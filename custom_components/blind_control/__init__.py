"""Blind Control: rule, override, oversteer and sun based cover control."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION,
    ATTR_FORCE,
    ATTR_NOTE,
    CONF_POSITION,
    CONF_PRIORITY,
    DOMAIN,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_RECALCULATE,
    SERVICE_SET_OVERRIDE,
)
from .controller import BlindControlConfigEntry, BlindController
from .engine import OverrideSource

try:
    import probatio as vol
except ImportError:  # Home Assistant < 2026.x
    import voluptuous as vol  # type: ignore[no-redef]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTRY_SCHEMA = {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
SET_OVERRIDE_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(CONF_POSITION): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional(ATTR_DURATION): cv.positive_time_period,
        vol.Optional(CONF_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional(ATTR_NOTE): cv.string,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)
CLEAR_OVERRIDE_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Optional(CONF_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)
ENTRY_SCHEMA = vol.Schema(_ENTRY_SCHEMA)


def _controller(hass: HomeAssistant, call: ServiceCall) -> BlindController:
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry: BlindControlConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_entry",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry.runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions."""

    @callback
    def set_override(call: ServiceCall) -> None:
        duration: timedelta | None = call.data.get(ATTR_DURATION)
        _controller(hass, call).set_override(
            call.data[CONF_POSITION],
            source=OverrideSource.SERVICE,
            duration=duration,
            priority=call.data.get(CONF_PRIORITY),
            note=call.data.get(ATTR_NOTE),
            force=call.data[ATTR_FORCE],
        )

    @callback
    def clear_override(call: ServiceCall) -> None:
        _controller(hass, call).clear_override(call.data.get(CONF_PRIORITY))

    @callback
    def recalculate(call: ServiceCall) -> None:
        _controller(hass, call).evaluate("recalculate action", force=True)

    hass.services.async_register(DOMAIN, SERVICE_SET_OVERRIDE, set_override, SET_OVERRIDE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_OVERRIDE, clear_override, CLEAR_OVERRIDE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_RECALCULATE, recalculate, ENTRY_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BlindControlConfigEntry) -> bool:
    """Set up a controller from a config entry."""
    controller = BlindController(hass, entry)
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_setup()
    # Options and rule/oversteer subentry changes both fire update listeners.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: BlindControlConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BlindControlConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded
