"""Home Assistant runtime for one Blind Control config entry."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    COMMAND_GRACE,
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
    LOGGER,
    SUBENTRY_OVERSTEER,
    SUBENTRY_RULE,
)
from .engine import (
    WEEKDAY_NAMES,
    AstralSun,
    ControllerConfig,
    Decision,
    Inputs,
    Override,
    OverrideSource,
    Oversteer,
    Prediction,
    Rule,
    RuleAction,
    Source,
    SunMode,
    TimeKind,
    TimeSpec,
    evaluate,
    is_move,
    next_boundary,
    predict,
)

STORAGE_VERSION = 1

type BlindControlConfigEntry = ConfigEntry[BlindController]


def _parse_time(value: str | None) -> time | None:
    return dt_util.parse_time(value) if value else None


def _time_spec(data: Mapping[str, Any] | None) -> TimeSpec:
    if not data:
        return TimeSpec()
    return TimeSpec(
        kind=TimeKind(data.get(CONF_KIND, TimeKind.NONE)),
        at=_parse_time(data.get(CONF_TIME)),
        offset=timedelta(minutes=float(data.get(CONF_OFFSET) or 0)),
        not_before=_parse_time(data.get(CONF_NOT_BEFORE)),
        not_after=_parse_time(data.get(CONF_NOT_AFTER)),
    )


def build_config(entry: ConfigEntry) -> ControllerConfig:
    """Build the engine configuration from a config entry and its subentries."""
    opts = {**DEFAULTS, **entry.options}
    rules: list[Rule] = []
    oversteers: list[Oversteer] = []
    for sub_id, sub in entry.subentries.items():
        data = sub.data
        if sub.subentry_type == SUBENTRY_RULE:
            rules.append(
                Rule(
                    id=sub_id,
                    name=data.get(CONF_NAME, sub.title),
                    order=int(data.get(CONF_ORDER, 0)),
                    action=RuleAction(data.get(CONF_ACTION, RuleAction.POSITION)),
                    position=int(data.get(CONF_POSITION, 0)),
                    start=_time_spec(data.get(CONF_START)),
                    end=_time_spec(data.get(CONF_END)),
                    weekdays=frozenset(WEEKDAY_NAMES.index(d) for d in data.get(CONF_WEEKDAYS, [])),
                    has_conditions=bool(data.get(CONF_CONDITIONS)),
                    priority=int(data.get(CONF_PRIORITY, 0)),
                    reset_override=bool(data.get(CONF_RESET_OVERRIDE, False)),
                )
            )
        elif sub.subentry_type == SUBENTRY_OVERSTEER:
            oversteers.append(
                Oversteer(
                    id=sub_id,
                    name=data.get(CONF_NAME, sub.title),
                    order=int(data.get(CONF_ORDER, 0)),
                    position=int(data.get(CONF_POSITION, 100)),
                    only_when_sun_in_window=bool(data.get(CONF_ONLY_IN_SUN, True)),
                    has_conditions=bool(data.get(CONF_CONDITIONS)),
                )
            )
    return ControllerConfig(
        default_position=int(opts[CONF_DEFAULT_POSITION]),
        min_position=int(opts[CONF_MIN_POSITION]),
        max_position=int(opts[CONF_MAX_POSITION]),
        min_delta=int(opts[CONF_MIN_DELTA]),
        sun_mode=SunMode(opts[CONF_SUN_MODE]),
        sun_position=int(opts[CONF_SUN_POSITION]),
        azimuth=float(opts[CONF_AZIMUTH]),
        azimuth_left=float(opts[CONF_AZIMUTH_LEFT]),
        azimuth_right=float(opts[CONF_AZIMUTH_RIGHT]),
        min_elevation=float(opts[CONF_MIN_ELEVATION]),
        max_elevation=float(opts[CONF_MAX_ELEVATION]),
        window_top=float(opts[CONF_WINDOW_TOP]),
        window_bottom=float(opts[CONF_WINDOW_BOTTOM]),
        floor_length=float(opts[CONF_FLOOR_LENGTH]),
        rules=tuple(rules),
        oversteers=tuple(oversteers),
    )


def cover_position(state: State | None) -> int | None:
    """Return a cover's position (0 closed - 100 open) from its state."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if (pos := state.attributes.get(ATTR_CURRENT_POSITION)) is not None:
        return int(pos)
    if state.state == STATE_OPEN:
        return 100
    if state.state == STATE_CLOSED:
        return 0
    return None


class BlindController:
    """Evaluates the decision engine and drives the covers of one entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise."""
        self.hass = hass
        self.entry = entry
        opts = {**DEFAULTS, **entry.options}
        self.covers: list[str] = list(opts.get(CONF_COVERS, []))
        self.detect_manual = bool(opts[CONF_DETECT_MANUAL])
        self.override_duration = timedelta(minutes=float(opts[CONF_OVERRIDE_DURATION]))
        self.manual_priority = int(opts[CONF_MANUAL_PRIORITY])
        self.update_interval = timedelta(minutes=max(1.0, float(opts[CONF_UPDATE_INTERVAL])))
        self.tolerance = int(opts[CONF_TOLERANCE])
        self.config = build_config(entry)
        self.tz = dt_util.get_default_time_zone()
        self.sun = AstralSun(
            hass.config.latitude, hass.config.longitude, hass.config.elevation, self.tz
        )

        # Persistent state
        self.enabled = True
        self.sun_enabled = True
        self.override: Override | None = None
        self.last_commanded: int | None = None
        self.last_command_time: datetime | None = None
        self._prev_active: frozenset[str] | None = None

        # Derived state
        self.decision: Decision | None = None
        self.next_action: Prediction | None = None
        self.next_evaluation: datetime | None = None
        self.last_trigger: str | None = None
        self.last_override_event: str | None = None

        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._checkers: dict[str, Any] = {}
        self._our_contexts: deque[str] = deque(maxlen=64)
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unsub_timer: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------ setup

    async def async_setup(self) -> None:
        """Load state, compile conditions and start listening."""
        if stored := await self._store.async_load():
            self.enabled = stored.get("enabled", True)
            self.sun_enabled = stored.get("sun_enabled", True)
            self.last_commanded = stored.get("last_commanded")
            if prev := stored.get("previously_active"):
                self._prev_active = frozenset(prev)
            if ov := stored.get("override"):
                try:
                    self.override = Override.from_dict(ov)
                except (KeyError, ValueError):
                    LOGGER.warning("Discarding invalid stored override: %s", ov)

        tracked: set[str] = set()
        for sub_id, sub in self.entry.subentries.items():
            if not (conds := sub.data.get(CONF_CONDITIONS)):
                continue
            try:
                validated = await condition.async_validate_conditions_config(self.hass, list(conds))
                self._checkers[sub_id] = await condition.async_conditions_from_config(
                    self.hass, validated, LOGGER, f"{self.entry.title}: {sub.title}"
                )
            except HomeAssistantError as err:
                LOGGER.error(
                    "Invalid conditions in '%s' of '%s': %s; treating as not met",
                    sub.title,
                    self.entry.title,
                    err,
                )
                continue
            for cond in validated:
                tracked |= condition.async_extract_entities(cond)

        tracked.discard("")
        if tracked:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sorted(tracked), self._condition_entity_changed
                )
            )
        if self.covers:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.covers, self._cover_changed)
            )

        if self.hass.is_running:
            self.evaluate("setup")
        else:
            self.evaluate("setup", apply=False)
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    lambda _: self.evaluate("home_assistant_started"),
                )
            )

    async def async_unload(self) -> None:
        """Stop listening and persist state."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        for checker in self._checkers.values():
            if unload := getattr(checker, "async_unload", None):
                unload()
        self._checkers.clear()
        await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sun_enabled": self.sun_enabled,
            "last_commanded": self.last_commanded,
            "override": self.override.as_dict() if self.override else None,
            "previously_active": sorted(self._prev_active)
            if self._prev_active is not None
            else None,
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 2)

    # ------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register an entity update callback."""
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ------------------------------------------------------------- evaluate

    @callback
    def _check_conditions(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for sub_id, checker in self._checkers.items():
            try:
                results[sub_id] = bool(checker(None))
            except Exception:
                LOGGER.exception("Error evaluating conditions of %s", sub_id)
                results[sub_id] = False
        return results

    def _inputs(self) -> Inputs:
        return Inputs(
            enabled=self.enabled,
            sun_enabled=self.sun_enabled,
            override=self.override,
            conditions=self._check_conditions(),
            previously_active=self._prev_active,
        )

    @callback
    def evaluate(self, trigger: str, *, apply: bool = True, force: bool = False) -> None:
        """Re-evaluate, drive the covers and schedule the next evaluation."""
        now = dt_util.now()
        inputs = self._inputs()
        decision = evaluate(self.config, inputs, now, self.sun, self.tz)
        self.last_trigger = trigger

        if decision.override_expired and self.override is not None:
            LOGGER.debug("%s: override expired", self.entry.title)
            self.last_override_event = f"Override expired at {now.isoformat()}"
            self.override = None
        if decision.reset_override_by is not None and self.override is not None:
            LOGGER.debug(
                "%s: override reset by rule %s",
                self.entry.title,
                decision.reset_override_by.name,
            )
            self.last_override_event = (
                f"Override cleared by rule '{decision.reset_override_by.name}' at {now.isoformat()}"
            )
            self.override = None
        self._prev_active = decision.active_rule_ids
        self.decision = decision

        if apply:
            self._apply(decision, force=force)

        baseline = (
            decision.position
            if decision.source is Source.OVERRIDE or self.last_commanded is None
            else self.last_commanded
        )
        self.next_action = predict(
            self.config,
            replace(inputs, override=self.override),
            now,
            self.sun,
            self.tz,
            decision,
            baseline,
        )
        self._schedule(now)
        self._save()
        self._notify()

    @callback
    def _schedule(self, now: datetime) -> None:
        if self._unsub_timer:
            self._unsub_timer()
        candidates = [now + self.update_interval]
        if self.next_action is not None:
            candidates.append(self.next_action.time)
        if self.override is not None and self.override.expires is not None:
            candidates.append(self.override.expires)
        if (boundary := next_boundary(self.config, now, self.sun, self.tz)) is not None:
            candidates.append(boundary)
        when = max(min(candidates), now + timedelta(seconds=1)) + timedelta(seconds=1)
        self.next_evaluation = when
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass, self._timer_fired, dt_util.as_utc(when)
        )

    @callback
    def _timer_fired(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.evaluate("schedule")

    @callback
    def _condition_entity_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if (
            old is not None
            and new is not None
            and old.state == new.state
            and old.attributes == new.attributes
        ):
            return
        self.evaluate(f"state change: {event.data['entity_id']}")

    # ----------------------------------------------------------------- apply

    @callback
    def _apply(self, decision: Decision, *, force: bool) -> None:
        target = decision.position
        if target is None:
            return
        if (
            decision.source is Source.OVERRIDE
            and decision.override is not None
            and decision.override.source is OverrideSource.MANUAL
        ):
            # The covers are already where the user put them.
            self.last_commanded = target
            return
        if not force and not is_move(self.config, self.last_commanded, target):
            return

        self.last_commanded = target
        self.last_command_time = dt_util.utcnow()
        for entity_id in self.covers:
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                LOGGER.debug("%s unavailable, not moving it", entity_id)
                continue
            current = cover_position(state)
            if current is not None and abs(current - target) <= 1:
                continue
            context = Context()
            self._our_contexts.append(context.id)
            features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
            if features & CoverEntityFeature.SET_POSITION:
                service, data = (
                    "set_cover_position",
                    {
                        ATTR_ENTITY_ID: entity_id,
                        ATTR_POSITION: target,
                    },
                )
            else:
                service = "open_cover" if target >= 50 else "close_cover"
                data = {ATTR_ENTITY_ID: entity_id}
            LOGGER.debug(
                "%s: %s %s -> %s (%s)",
                self.entry.title,
                service,
                entity_id,
                target,
                decision.reason,
            )
            self.hass.async_create_task(
                self._async_call(service, data, context),
                f"{DOMAIN} move {entity_id}",
            )

    async def _async_call(self, service: str, data: dict[str, Any], context: Context) -> None:
        try:
            await self.hass.services.async_call(
                "cover", service, data, blocking=True, context=context
            )
        except HomeAssistantError as err:
            LOGGER.warning("Failed to move %s: %s", data[ATTR_ENTITY_ID], err)

    # ------------------------------------------------------- manual detection

    def _is_ours(self, old: State, new: State, position: int, old_position: int | None) -> bool:
        ctx = new.context
        if ctx.id in self._our_contexts or (
            ctx.parent_id is not None and ctx.parent_id in self._our_contexts
        ):
            return True
        target = self.last_commanded
        if self.override is not None and self.override.source is OverrideSource.SERVICE:
            target = self.override.position
        if target is None:
            return False
        if abs(position - target) <= self.tolerance:
            return True
        # Intermediate positions reported while travelling toward the target.
        return (
            self.last_command_time is not None
            and dt_util.utcnow() - self.last_command_time < timedelta(seconds=COMMAND_GRACE)
            and old_position is not None
            and abs(position - target) < abs(old_position - target)
        )

    @callback
    def _cover_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        self._notify()  # cover positions are exposed as attributes
        if not (self.detect_manual and self.enabled) or old is None or new is None:
            return
        if old.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if new.state in (STATE_OPENING, STATE_CLOSING):
            return
        position, old_position = cover_position(new), cover_position(old)
        if position is None or position == old_position:
            return
        if self._is_ours(old, new, position, old_position):
            return

        user = f" by user {new.context.user_id}" if new.context.user_id else ""
        LOGGER.info(
            "%s: manual change of %s to %s%%%s detected",
            self.entry.title,
            event.data["entity_id"],
            position,
            user,
        )
        self.set_override(
            position,
            source=OverrideSource.MANUAL,
            note=f"{event.data['entity_id']} moved{user}",
        )

    # ------------------------------------------------------------- overrides

    @callback
    def set_override(
        self,
        position: int,
        *,
        source: OverrideSource = OverrideSource.SERVICE,
        duration: timedelta | None = None,
        priority: int | None = None,
        note: str | None = None,
    ) -> None:
        """Set an override and re-evaluate."""
        now = dt_util.now()
        duration = self.override_duration if duration is None else duration
        self.override = Override(
            position=max(0, min(100, int(position))),
            source=source,
            priority=self.manual_priority if priority is None else int(priority),
            started=now,
            expires=now + duration if duration.total_seconds() > 0 else None,
            note=note,
        )
        self.last_override_event = f"Override set at {now.isoformat()}"
        self.evaluate(f"override ({source.value})", force=source is OverrideSource.SERVICE)

    @callback
    def clear_override(self) -> None:
        """Clear the override and return to automatic control."""
        if self.override is None:
            return
        self.override = None
        self.last_override_event = f"Override cleared at {dt_util.now().isoformat()}"
        self.evaluate("override cleared", force=True)

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable automatic control."""
        self.enabled = enabled
        self.evaluate("automation enabled" if enabled else "automation disabled", force=enabled)

    @callback
    def set_sun_enabled(self, enabled: bool) -> None:
        """Enable or disable sun control."""
        self.sun_enabled = enabled
        self.evaluate("sun control enabled" if enabled else "sun control disabled")

    # ------------------------------------------------------------ reporting

    def cover_positions(self) -> dict[str, int | None]:
        """Current position of every controlled cover."""
        return {c: cover_position(self.hass.states.get(c)) for c in self.covers}

    def status_attributes(self) -> dict[str, Any]:
        """Full explanation of the current state."""
        d = self.decision
        if d is None:
            return {}
        return {
            "reason": d.reason,
            "target_position": d.position,
            "last_commanded_position": self.last_commanded,
            "cover_positions": self.cover_positions(),
            "rule": d.rule.name if d.rule else None,
            "oversteer": d.oversteer.name if d.oversteer else None,
            "override": d.override.as_dict() if d.override else None,
            "limits": list(d.limits),
            "sun": d.sun.as_dict(),
            "sun_mode": self.config.sun_mode.value,
            "rules": [s.as_dict() for s in d.rules],
            "evaluated_at": d.time.isoformat(),
            "trigger": self.last_trigger,
            "last_override_event": self.last_override_event,
            "next_evaluation": self.next_evaluation.isoformat() if self.next_evaluation else None,
        }

    def next_action_attributes(self) -> dict[str, Any]:
        """Details of the predicted next movement."""
        if (p := self.next_action) is None:
            return {"position": None, "reason": "No movement is scheduled"}
        return {
            "position": p.decision.position,
            "source": p.decision.source.value,
            "reason": p.decision.reason,
            "rule": p.decision.rule.name if p.decision.rule else None,
            "assumption": "conditions keep their current state",
        }

    def next_action_text(self) -> str:
        """One line summary of the next action."""
        if (p := self.next_action) is None:
            return "No movement scheduled"
        when = p.time.astimezone(self.tz)
        day = "" if when.date() == dt_util.now().date() else when.strftime("%a ")
        return f"{p.decision.position}% at {day}{when:%H:%M} — {p.decision.reason}"[:255]
