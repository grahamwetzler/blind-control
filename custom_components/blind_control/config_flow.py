"""Config flow, options flow and rule/oversteer subentry flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition, selector

from .const import (
    CONF_ACTION,
    CONF_AZIMUTH,
    CONF_AZIMUTH_LEFT,
    CONF_AZIMUTH_RIGHT,
    CONF_CONDITIONS,
    CONF_COVERS,
    CONF_DEFAULT_POSITION,
    CONF_DETECT_MANUAL,
    CONF_END,
    CONF_FLOOR_LENGTH,
    CONF_KIND,
    CONF_MANUAL_PRIORITY,
    CONF_MAX_ELEVATION,
    CONF_MAX_POSITION,
    CONF_MIN_DELTA,
    CONF_MIN_ELEVATION,
    CONF_MIN_POSITION,
    CONF_NAME,
    CONF_NOT_AFTER,
    CONF_NOT_BEFORE,
    CONF_OFFSET,
    CONF_ONLY_IN_SUN,
    CONF_ORDER,
    CONF_OVERRIDE_DURATION,
    CONF_POSITION,
    CONF_PRIORITY,
    CONF_RESET_OVERRIDE,
    CONF_START,
    CONF_SUN_MODE,
    CONF_SUN_POSITION,
    CONF_TIME,
    CONF_TOLERANCE,
    CONF_UPDATE_INTERVAL,
    CONF_WEEKDAYS,
    CONF_WINDOW_BOTTOM,
    CONF_WINDOW_TOP,
    DEFAULTS,
    DOMAIN,
    SECTION_OVERRIDE,
    SECTION_POSITIONS,
    SECTION_SUN,
    SECTION_WINDOW,
    SUBENTRY_OVERSTEER,
    SUBENTRY_RULE,
)
from .engine import WEEKDAY_NAMES, RuleAction, SunMode, TimeKind

try:
    import probatio as vol
except ImportError:  # Home Assistant < 2026.x
    import voluptuous as vol  # type: ignore[no-redef]


def _number(
    lo: float, hi: float, step: float = 1, unit: str | None = None, slider: bool = False
) -> vol.All:
    config = selector.NumberSelectorConfig(
        min=lo,
        max=hi,
        step=step,
        mode=selector.NumberSelectorMode.SLIDER if slider else selector.NumberSelectorMode.BOX,
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return vol.All(selector.NumberSelector(config), vol.Coerce(float if step < 1 else int))


def _percent() -> vol.All:
    return _number(0, 100, 1, "%", slider=True)


def _select(options: list[str], key: str, multiple: bool = False) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key=key,
            multiple=multiple,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


COVERS_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=COVER_DOMAIN, multiple=True)
)

SECTIONS: dict[str, dict[str, Any]] = {
    SECTION_POSITIONS: {
        CONF_DEFAULT_POSITION: _percent(),
        CONF_MIN_POSITION: _percent(),
        CONF_MAX_POSITION: _percent(),
        CONF_MIN_DELTA: _number(1, 50, 1, "%"),
    },
    SECTION_SUN: {
        CONF_SUN_MODE: _select([m.value for m in SunMode], CONF_SUN_MODE),
        CONF_SUN_POSITION: _percent(),
        CONF_AZIMUTH: _number(0, 359, 1, "°"),
        CONF_AZIMUTH_LEFT: _number(0, 90, 1, "°"),
        CONF_AZIMUTH_RIGHT: _number(0, 90, 1, "°"),
        CONF_MIN_ELEVATION: _number(0, 90, 1, "°"),
        CONF_MAX_ELEVATION: _number(0, 90, 1, "°"),
    },
    SECTION_WINDOW: {
        CONF_WINDOW_TOP: _number(0, 20, 0.01, "m"),
        CONF_WINDOW_BOTTOM: _number(0, 20, 0.01, "m"),
        CONF_FLOOR_LENGTH: _number(0, 20, 0.01, "m"),
    },
    SECTION_OVERRIDE: {
        CONF_DETECT_MANUAL: selector.BooleanSelector(),
        CONF_OVERRIDE_DURATION: _number(0, 10080, 1, "min"),
        CONF_MANUAL_PRIORITY: _number(0, 100, 1),
        CONF_TOLERANCE: _number(0, 20, 1, "%"),
        CONF_UPDATE_INTERVAL: _number(1, 60, 1, "min"),
    },
}


def _settings_schema(with_covers: bool) -> vol.Schema:
    schema: dict[Any, Any] = {}
    if with_covers:
        schema[vol.Required(CONF_COVERS)] = COVERS_SELECTOR
    for name, fields in SECTIONS.items():
        schema[vol.Required(name)] = section(
            vol.Schema({vol.Required(k): v for k, v in fields.items()}),
            {"collapsed": name != SECTION_SUN},
        )
    return vol.Schema(schema)


def _nest(options: Mapping[str, Any]) -> dict[str, Any]:
    """Flat options -> nested suggested values for the sectioned form."""
    values = {**DEFAULTS, **options}
    nested: dict[str, Any] = {CONF_COVERS: values.get(CONF_COVERS, [])}
    for name, fields in SECTIONS.items():
        nested[name] = {k: values[k] for k in fields}
    return nested


def _flatten(user_input: Mapping[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if key in SECTIONS:
            flat.update(value)
        else:
            flat[key] = value
    return flat


def _validate_settings(flat: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if flat[CONF_MIN_POSITION] > flat[CONF_MAX_POSITION]:
        errors["base"] = "min_above_max"
    if flat[CONF_MIN_ELEVATION] >= flat[CONF_MAX_ELEVATION]:
        errors["base"] = "min_elevation_above_max"
    if flat[CONF_WINDOW_BOTTOM] >= flat[CONF_WINDOW_TOP]:
        errors["base"] = "window_bottom_above_top"
    return errors


class BlindControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a controller for a group of covers."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._name = ""
        self._covers: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BlindControlOptionsFlow:
        """Return the options flow."""
        return BlindControlOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Rules and oversteers are subentries."""
        return {SUBENTRY_RULE: RuleSubentryFlow, SUBENTRY_OVERSTEER: OversteerSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name the controller and pick its covers."""
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            self._covers = user_input[CONF_COVERS]
            return await self.async_step_settings()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): selector.TextSelector(),
                    vol.Required(CONF_COVERS): COVERS_SELECTOR,
                }
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Positions, sun, window geometry and override behaviour."""
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = _flatten(user_input)
            if not (errors := _validate_settings(flat)):
                return self.async_create_entry(
                    title=self._name, data={}, options={CONF_COVERS: self._covers, **flat}
                )
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                _settings_schema(with_covers=False), _nest({})
            ),
            errors=errors,
        )


class BlindControlOptionsFlow(OptionsFlow):
    """Edit controller settings (rules and oversteers are edited as subentries)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = _flatten(user_input)
            if not (errors := _validate_settings(flat)):
                return self.async_create_entry(data=flat)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _settings_schema(with_covers=True), _nest(self.config_entry.options)
            ),
            errors=errors,
        )


# ---------------------------------------------------------------- subentries


def _time_section() -> section:
    return section(
        vol.Schema(
            {
                vol.Required(CONF_KIND, default=TimeKind.NONE.value): _select(
                    [k.value for k in TimeKind], CONF_KIND
                ),
                vol.Optional(CONF_TIME): selector.TimeSelector(),
                vol.Optional(CONF_OFFSET, default=0): _number(-720, 720, 1, "min"),
                vol.Optional(CONF_NOT_BEFORE): selector.TimeSelector(),
                vol.Optional(CONF_NOT_AFTER): selector.TimeSelector(),
            }
        ),
        {"collapsed": False},
    )


RULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_ACTION, default=RuleAction.POSITION.value): _select(
            [a.value for a in RuleAction], CONF_ACTION
        ),
        vol.Required(CONF_POSITION, default=0): _percent(),
        vol.Required(CONF_START): _time_section(),
        vol.Required(CONF_END): _time_section(),
        vol.Optional(CONF_WEEKDAYS, default=[]): _select(
            list(WEEKDAY_NAMES), CONF_WEEKDAYS, multiple=True
        ),
        vol.Optional(CONF_CONDITIONS): selector.ConditionSelector(),
        vol.Required(CONF_ORDER, default=10): _number(0, 1000, 1),
        vol.Required(CONF_PRIORITY, default=0): _number(0, 100, 1),
        vol.Required(CONF_RESET_OVERRIDE, default=False): selector.BooleanSelector(),
    }
)

OVERSTEER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_CONDITIONS): selector.ConditionSelector(),
        vol.Required(CONF_POSITION, default=100): _percent(),
        vol.Required(CONF_ONLY_IN_SUN, default=True): selector.BooleanSelector(),
        vol.Required(CONF_ORDER, default=10): _number(0, 1000, 1),
    }
)


async def _validate_conditions(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, str]:
    if not (conds := data.get(CONF_CONDITIONS)):
        return {}
    try:
        await condition.async_validate_conditions_config(hass, list(conds))
    except (HomeAssistantError, vol.Invalid):
        return {CONF_CONDITIONS: "invalid_conditions"}
    return {}


def _validate_rule(data: Mapping[str, Any]) -> dict[str, str]:
    for key in (CONF_START, CONF_END):
        spec = data.get(key) or {}
        if spec.get(CONF_KIND) == TimeKind.TIME and not spec.get(CONF_TIME):
            return {"base": "time_required"}
    return {}


class _BaseSubentryFlow(ConfigSubentryFlow):
    schema: vol.Schema
    subentry_type: str

    async def _validate(self, data: dict[str, Any]) -> dict[str, str]:
        return await _validate_conditions(self.hass, data)

    def _default_order(self) -> int:
        entry = self._get_entry()
        return 10 * (len(entry.get_subentries_of_type(self.subentry_type)) + 1)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a new item."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (errors := await self._validate(user_input)):
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        suggested = user_input or {CONF_ORDER: self._default_order()}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(self.schema, suggested),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing item."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (errors := await self._validate(user_input)):
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self.schema, user_input or dict(subentry.data)
            ),
            errors=errors,
        )


class RuleSubentryFlow(_BaseSubentryFlow):
    """Add or edit a rule."""

    schema = RULE_SCHEMA
    subentry_type = SUBENTRY_RULE

    async def _validate(self, data: dict[str, Any]) -> dict[str, str]:
        return _validate_rule(data) or await super()._validate(data)


class OversteerSubentryFlow(_BaseSubentryFlow):
    """Add or edit an oversteer."""

    schema = OVERSTEER_SCHEMA
    subentry_type = SUBENTRY_OVERSTEER
