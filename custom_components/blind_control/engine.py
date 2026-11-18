"""Decision engine for Blind Control.

This module is deliberately free of Home Assistant imports so the complete
decision pipeline (overrides, rules, oversteer, sun control) and the
next-action prediction can be unit tested in isolation.

Decision pipeline, highest precedence first:

1. Automation disabled        -> no action.
2. Override (manual/service)  -> override position, unless an active
                                 ``position`` rule has a *higher* priority.
3. ``position`` rule          -> first active rule (by order) wins.
4. Sun control (if enabled):
     a. oversteer             -> first matching oversteer (by order) wins.
     b. sun in window         -> shading position (restrict / maximize).
     c. sun not in window     -> default position.
5. Default position.

Active ``min`` / ``max`` rules then limit results from steps 4-5, and the
global min/max limits clamp every non-override result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
import math
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Source(StrEnum):
    """What determined the current target position."""

    DISABLED = "disabled"
    OVERRIDE = "override"
    RULE = "rule"
    OVERSTEER = "oversteer"
    SUN = "sun"
    DEFAULT = "default"


class RuleAction(StrEnum):
    """What a rule does while it is active."""

    POSITION = "position"  # absolute position, stops evaluation
    MIN = "min"  # lower limit for automatic positions
    MAX = "max"  # upper limit for automatic positions


class SunMode(StrEnum):
    """Sun control mode."""

    OFF = "off"
    RESTRICT = "restrict"  # fixed shade position while sun is in the window
    MAXIMIZE = "maximize"  # as open as possible while keeping sun off the floor


class TimeKind(StrEnum):
    """Anchor for a rule time boundary."""

    NONE = "none"
    TIME = "time"
    DAWN = "dawn"
    SUNRISE = "sunrise"
    NOON = "noon"
    SUNSET = "sunset"
    DUSK = "dusk"


class OverrideSource(StrEnum):
    """Where an override came from."""

    MANUAL = "manual"  # cover moved outside of Blind Control
    SERVICE = "service"  # blind_control.set_override action


# ---------------------------------------------------------------------------
# Sun provider
# ---------------------------------------------------------------------------


class SunProvider(Protocol):
    """Sun position / event source."""

    def position(self, when: datetime) -> tuple[float, float]:
        """Return (azimuth, elevation) in degrees."""

    def event(self, kind: TimeKind, day: date) -> datetime | None:
        """Return the time of a sun event on a local day, or None."""


class AstralSun:
    """SunProvider backed by astral (bundled with Home Assistant)."""

    def __init__(self, latitude: float, longitude: float, elevation: float, tz: tzinfo) -> None:
        """Initialise."""
        from astral import Observer, sun as astral_sun

        self._sun = astral_sun
        self._observer = Observer(latitude, longitude, elevation)
        self._tz = tz
        self._events: dict[tuple[TimeKind, date], datetime | None] = {}
        self._funcs: dict[TimeKind, Callable[..., datetime]] = {
            TimeKind.DAWN: astral_sun.dawn,
            TimeKind.SUNRISE: astral_sun.sunrise,
            TimeKind.NOON: astral_sun.noon,
            TimeKind.SUNSET: astral_sun.sunset,
            TimeKind.DUSK: astral_sun.dusk,
        }

    def position(self, when: datetime) -> tuple[float, float]:
        """Return (azimuth, elevation)."""
        return (
            self._sun.azimuth(self._observer, when),
            self._sun.elevation(self._observer, when),
        )

    def event(self, kind: TimeKind, day: date) -> datetime | None:
        """Return a (cached) sun event time."""
        key = (kind, day)
        if key not in self._events:
            try:
                value: datetime | None = self._funcs[kind](self._observer, day, tzinfo=self._tz)
            except ValueError:  # sun never reaches the angle (polar regions)
                value = None
            if len(self._events) > 512:
                self._events.clear()
            self._events[key] = value
        return self._events[key]


# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeSpec:
    """A time-of-day boundary: fixed time or sun event, with offset and clamps."""

    kind: TimeKind = TimeKind.NONE
    at: time | None = None
    offset: timedelta = timedelta()
    not_before: time | None = None
    not_after: time | None = None

    @property
    def is_set(self) -> bool:
        """Return True if the boundary is configured."""
        return self.kind is not TimeKind.NONE and (
            self.kind is not TimeKind.TIME or self.at is not None
        )

    def resolve(self, day: date, sun: SunProvider, tz: tzinfo) -> datetime | None:
        """Resolve the boundary for a local day."""
        if not self.is_set:
            return None
        if self.kind is TimeKind.TIME:
            assert self.at is not None
            base = datetime.combine(day, self.at, tz)
        else:
            event = sun.event(self.kind, day)
            if event is None:
                return None
            base = event.astimezone(tz)
        base += self.offset
        if self.not_before is not None:
            base = max(base, datetime.combine(day, self.not_before, tz))
        if self.not_after is not None:
            base = min(base, datetime.combine(day, self.not_after, tz))
        return base

    def describe(self) -> str:
        """Human readable description."""
        if not self.is_set:
            return "—"
        if self.kind is TimeKind.TIME:
            assert self.at is not None
            text = self.at.strftime("%H:%M")
        else:
            text = self.kind.value
        minutes = int(self.offset.total_seconds() // 60)
        if minutes:
            text += f" {'+' if minutes > 0 else '-'}{abs(minutes)} min"
        if self.not_before is not None:
            text += f" (not before {self.not_before.strftime('%H:%M')})"
        if self.not_after is not None:
            text += f" (not after {self.not_after.strftime('%H:%M')})"
        return text


WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class Rule:
    """A time / condition based rule."""

    id: str
    name: str
    order: int = 0
    action: RuleAction = RuleAction.POSITION
    position: int = 0
    start: TimeSpec = TimeSpec()
    end: TimeSpec = TimeSpec()
    weekdays: frozenset[int] = frozenset()  # 0 = Monday; empty = every day
    has_conditions: bool = False
    priority: int = 0
    reset_override: bool = False

    @property
    def has_window(self) -> bool:
        """Return True if the rule is limited to a time window."""
        return self.start.is_set or self.end.is_set

    def describe_window(self) -> str:
        """Human readable time window."""
        if not self.has_window:
            return "all day"
        start = self.start.describe() if self.start.is_set else "midnight"
        end = self.end.describe() if self.end.is_set else "midnight"
        return f"{start} → {end}"


@dataclass(frozen=True, slots=True)
class Oversteer:
    """Condition based override of sun control (e.g. clouds, heat, wind)."""

    id: str
    name: str
    order: int = 0
    position: int = 100
    only_when_sun_in_window: bool = True
    has_conditions: bool = True


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    """Static configuration of one controller (group of covers)."""

    default_position: int = 100
    min_position: int = 0
    max_position: int = 100
    min_delta: int = 5
    sun_mode: SunMode = SunMode.OFF
    sun_position: int = 0
    azimuth: float = 180.0
    azimuth_left: float = 90.0
    azimuth_right: float = 90.0
    min_elevation: float = 0.0
    max_elevation: float = 90.0
    window_top: float = 2.0
    window_bottom: float = 0.0
    floor_length: float = 0.5
    rules: tuple[Rule, ...] = ()
    oversteers: tuple[Oversteer, ...] = ()

    def clamp(self, position: int) -> int:
        """Clamp to the global limits."""
        return max(self.min_position, min(self.max_position, position))


@dataclass(frozen=True, slots=True)
class Override:
    """A temporary override of automatic control."""

    position: int
    source: OverrideSource
    priority: int
    started: datetime
    expires: datetime | None = None
    note: str | None = None

    def is_active(self, now: datetime) -> bool:
        """Return True if the override has not expired."""
        return self.expires is None or now < self.expires

    def as_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            "position": self.position,
            "source": self.source.value,
            "priority": self.priority,
            "started": self.started.isoformat(),
            "expires": self.expires.isoformat() if self.expires else None,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Override:
        """Deserialise."""
        return cls(
            position=int(data["position"]),
            source=OverrideSource(data["source"]),
            priority=int(data["priority"]),
            started=datetime.fromisoformat(data["started"]),
            expires=datetime.fromisoformat(data["expires"]) if data.get("expires") else None,
            note=data.get("note"),
        )


@dataclass(frozen=True, slots=True)
class Inputs:
    """Dynamic inputs for one evaluation."""

    enabled: bool = True
    sun_enabled: bool = True
    override: Override | None = None
    # Result of the Home Assistant conditions for each rule / oversteer id.
    conditions: Mapping[str, bool] = field(default_factory=dict)
    # Rules active at the previous evaluation; None disables reset detection.
    previously_active: frozenset[str] | None = None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SunState:
    """Sun position relative to the window."""

    azimuth: float
    elevation: float
    in_window: bool
    detail: str
    profile_angle: float | None = None
    shade_position: int | None = None  # position sun control would pick

    def as_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            "azimuth": round(self.azimuth, 1),
            "elevation": round(self.elevation, 1),
            "in_window": self.in_window,
            "detail": self.detail,
            "profile_angle": None if self.profile_angle is None else round(self.profile_angle, 1),
            "shade_position": self.shade_position,
        }


@dataclass(frozen=True, slots=True)
class RuleStatus:
    """Evaluation result of one rule."""

    rule: Rule
    active: bool
    detail: str
    window: tuple[datetime, datetime] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            "name": self.rule.name,
            "action": self.rule.action.value,
            "position": self.rule.position,
            "active": self.active,
            "detail": self.detail,
            "window": self.rule.describe_window(),
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of an evaluation."""

    time: datetime
    position: int | None
    source: Source
    reason: str
    sun: SunState
    rules: tuple[RuleStatus, ...] = ()
    active_rule_ids: frozenset[str] = frozenset()
    rule: Rule | None = None
    oversteer: Oversteer | None = None
    override: Override | None = None
    limits: tuple[str, ...] = ()
    reset_override_by: Rule | None = None
    override_expired: bool = False


@dataclass(frozen=True, slots=True)
class Prediction:
    """The next time the target position will change."""

    time: datetime
    decision: Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_time(value: datetime, tz: tzinfo) -> str:
    return value.astimezone(tz).strftime("%H:%M")


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tz)


def _window_for_day(
    rule: Rule, day: date, sun: SunProvider, tz: tzinfo
) -> tuple[datetime, datetime] | None:
    """Return the rule window that starts on a local day."""
    start = rule.start.resolve(day, sun, tz) if rule.start.is_set else _midnight(day, tz)
    end = (
        rule.end.resolve(day, sun, tz)
        if rule.end.is_set
        else _midnight(day + timedelta(days=1), tz)
    )
    if start is None or end is None:
        return None
    if end <= start:
        if not (rule.start.is_set and rule.end.is_set):
            return None
        # Window crosses midnight, e.g. sunset -> sunrise.
        end = rule.end.resolve(day + timedelta(days=1), sun, tz)
        if end is None or end <= start:
            return None
    return start, end


def _day_allowed(rule: Rule, day: date) -> bool:
    return not rule.weekdays or day.weekday() in rule.weekdays


def rule_window_at(
    rule: Rule, now: datetime, sun: SunProvider, tz: tzinfo
) -> tuple[datetime, datetime] | None:
    """Return the window containing ``now`` (day filter applied), if any."""
    today = now.astimezone(tz).date()
    for day in (today - timedelta(days=1), today):
        if not _day_allowed(rule, day):
            continue
        window = _window_for_day(rule, day, sun, tz)
        if window and window[0] <= now < window[1]:
            return window
    return None


def rule_boundaries(
    rules: Iterable[Rule],
    start: datetime,
    end: datetime,
    sun: SunProvider,
    tz: tzinfo,
) -> list[datetime]:
    """Return every rule window boundary in (start, end]."""
    result: set[datetime] = set()
    first = start.astimezone(tz).date() - timedelta(days=1)
    days = (end.astimezone(tz).date() - first).days + 1
    for rule in rules:
        if not rule.has_window and not rule.weekdays:
            continue
        for offset in range(days):
            day = first + timedelta(days=offset)
            if rule.has_window:
                window = _window_for_day(rule, day, sun, tz)
                points: tuple[datetime, ...] = window or ()
            else:  # weekday-only rule: boundaries at midnight
                points = (_midnight(day, tz),)
            result.update(p for p in points if start < p <= end)
    return sorted(result)


def _angle_diff(a: float, b: float) -> float:
    """Signed difference a - b in (-180, 180]."""
    return (a - b + 540.0) % 360.0 - 180.0


def compute_sun(cfg: ControllerConfig, azimuth: float, elevation: float) -> SunState:
    """Determine whether the sun shines into the window and the shade position."""
    diff = _angle_diff(azimuth, cfg.azimuth)
    profile: float | None = None
    if elevation > 0 and abs(diff) < 90:
        profile = math.degrees(
            math.atan(math.tan(math.radians(elevation)) / math.cos(math.radians(diff)))
        )

    if elevation <= 0:
        return SunState(azimuth, elevation, False, "sun is below the horizon", profile)
    if not -cfg.azimuth_left <= diff <= cfg.azimuth_right:
        side = "left" if diff < 0 else "right"
        return SunState(
            azimuth,
            elevation,
            False,
            f"sun azimuth {azimuth:.0f}° is outside the window ({side})",
            profile,
        )
    if elevation <= cfg.min_elevation:
        return SunState(
            azimuth,
            elevation,
            False,
            f"sun elevation {elevation:.0f}° is below the minimum {cfg.min_elevation:.0f}°",
            profile,
        )
    if elevation > cfg.max_elevation:
        return SunState(
            azimuth,
            elevation,
            False,
            f"sun elevation {elevation:.0f}° is above the maximum {cfg.max_elevation:.0f}°",
            profile,
        )

    if cfg.sun_mode is SunMode.MAXIMIZE:
        span = cfg.window_top - cfg.window_bottom
        if profile is None or span <= 0:
            shade = 100
        else:
            # Height of the shade's bottom edge at which the sun reaches exactly
            # ``floor_length`` into the room.
            edge = cfg.floor_length * math.tan(math.radians(profile))
            shade = round(max(0.0, min(1.0, (edge - cfg.window_bottom) / span)) * 100)
    else:
        shade = cfg.sun_position
    return SunState(
        azimuth,
        elevation,
        True,
        f"sun is in the window (azimuth {azimuth:.0f}°, elevation {elevation:.0f}°)",
        profile,
        shade,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _rule_statuses(
    cfg: ControllerConfig,
    inputs: Inputs,
    now: datetime,
    sun: SunProvider,
    tz: tzinfo,
) -> list[RuleStatus]:
    statuses: list[RuleStatus] = []
    today = now.astimezone(tz).date()
    for rule in sorted(cfg.rules, key=lambda r: (r.order, r.name)):
        window = rule_window_at(rule, now, sun, tz)
        if rule.has_window and window is None:
            statuses.append(RuleStatus(rule, False, "outside time window"))
            continue
        if not rule.has_window and not _day_allowed(rule, today):
            statuses.append(RuleStatus(rule, False, "not scheduled today"))
            continue
        if rule.has_conditions and not inputs.conditions.get(rule.id, False):
            statuses.append(RuleStatus(rule, False, "conditions not met", window))
            continue
        detail = "active"
        if window is not None:
            detail += f" ({_fmt_time(window[0], tz)}–{_fmt_time(window[1], tz)})"
        statuses.append(RuleStatus(rule, True, detail, window))
    return statuses


def _describe_override(override: Override, tz: tzinfo) -> str:
    kind = "Manual override" if override.source is OverrideSource.MANUAL else "Override"
    text = f"{kind} to {override.position}%"
    if override.note:
        text += f" ({override.note})"
    if override.expires is not None:
        text += f" until {_fmt_time(override.expires, tz)}"
    else:
        text += " until cleared"
    return text


def evaluate(
    cfg: ControllerConfig,
    inputs: Inputs,
    now: datetime,
    sun: SunProvider,
    tz: tzinfo,
) -> Decision:
    """Evaluate the decision pipeline at ``now``."""
    azimuth, elevation = sun.position(now)
    sun_state = compute_sun(cfg, azimuth, elevation)
    statuses = _rule_statuses(cfg, inputs, now, sun, tz)
    active = [s.rule for s in statuses if s.active]
    active_ids = frozenset(r.id for r in active)

    override = inputs.override
    override_expired = False
    if override is not None and not override.is_active(now):
        override, override_expired = None, True

    reset_by: Rule | None = None
    if override is not None and inputs.previously_active is not None:
        for rule in active:
            if rule.reset_override and rule.id not in inputs.previously_active:
                reset_by, override = rule, None
                break

    common: dict[str, Any] = {
        "time": now,
        "sun": sun_state,
        "rules": tuple(statuses),
        "active_rule_ids": active_ids,
        "reset_override_by": reset_by,
        "override_expired": override_expired,
    }

    if not inputs.enabled:
        return Decision(
            position=None,
            source=Source.DISABLED,
            reason="Automation is disabled",
            override=override,
            **common,
        )

    absolute = next((r for r in active if r.action is RuleAction.POSITION), None)

    if override is not None and (absolute is None or absolute.priority <= override.priority):
        return Decision(
            position=override.position,
            source=Source.OVERRIDE,
            reason=_describe_override(override, tz),
            override=override,
            **common,
        )

    limits: list[str] = []
    if absolute is not None:
        position = cfg.clamp(absolute.position)
        reason = f"Rule '{absolute.name}' ({absolute.describe_window()}) sets {absolute.position}%"
        if override is not None:
            reason += (
                f"; outranks the override (priority {absolute.priority} > {override.priority})"
            )
        if position != absolute.position:
            limits.append(f"global limits {cfg.min_position}–{cfg.max_position}%")
            reason += f", limited to {position}%"
        return Decision(
            position=position,
            source=Source.RULE,
            reason=reason,
            rule=absolute,
            override=override,
            limits=tuple(limits),
            **common,
        )

    source = Source.DEFAULT
    oversteer: Oversteer | None = None
    if cfg.sun_mode is not SunMode.OFF and inputs.sun_enabled:
        for item in sorted(cfg.oversteers, key=lambda o: (o.order, o.name)):
            if item.only_when_sun_in_window and not sun_state.in_window:
                continue
            if item.has_conditions and not inputs.conditions.get(item.id, False):
                continue
            oversteer = item
            break
        if oversteer is not None:
            source, position = Source.OVERSTEER, oversteer.position
            reason = f"Oversteer '{oversteer.name}' sets {oversteer.position}%"
            if oversteer.only_when_sun_in_window:
                reason += " while the sun is in the window"
        elif sun_state.in_window:
            assert sun_state.shade_position is not None
            source, position = Source.SUN, sun_state.shade_position
            if cfg.sun_mode is SunMode.MAXIMIZE:
                reason = (
                    f"Sun control: {position}% keeps the sun within "
                    f"{cfg.floor_length:g} m of the window — {sun_state.detail}"
                )
            else:
                reason = f"Sun control: shading at {position}% — {sun_state.detail}"
        else:
            position = cfg.default_position
            reason = f"Default position {position}% — {sun_state.detail}"
    else:
        position = cfg.default_position
        reason = f"Default position {position}% — no rule is active"
        if cfg.sun_mode is not SunMode.OFF:
            reason += ", sun control is switched off"

    # Limit rules.
    for rule in active:
        if rule.action is RuleAction.MIN and position < rule.position:
            position = rule.position
            limits.append(f"rule '{rule.name}' min {rule.position}%")
        elif rule.action is RuleAction.MAX and position > rule.position:
            position = rule.position
            limits.append(f"rule '{rule.name}' max {rule.position}%")
    clamped = cfg.clamp(position)
    if clamped != position:
        limits.append(f"global limits {cfg.min_position}–{cfg.max_position}%")
        position = clamped
    if limits:
        reason += f"; limited to {position}% by " + ", ".join(limits)

    return Decision(
        position=position,
        source=source,
        reason=reason,
        oversteer=oversteer,
        override=override,
        limits=tuple(limits),
        **common,
    )


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


def is_move(cfg: ControllerConfig, baseline: int | None, target: int | None) -> bool:
    """Return True if moving from ``baseline`` to ``target`` would be commanded."""
    if target is None:
        return False
    if baseline is None:
        return True
    if target == baseline:
        return False
    if abs(target - baseline) >= cfg.min_delta:
        return True
    # Always allow reaching the end stops even for small deltas.
    return target in (0, 100, cfg.min_position, cfg.max_position)


def next_boundary(
    cfg: ControllerConfig,
    now: datetime,
    sun: SunProvider,
    tz: tzinfo,
    horizon: timedelta = timedelta(days=8),
) -> datetime | None:
    """Return the next rule window boundary after ``now``."""
    points = rule_boundaries(cfg.rules, now, now + horizon, sun, tz)
    return points[0] if points else None


def predict(
    cfg: ControllerConfig,
    inputs: Inputs,
    now: datetime,
    sun: SunProvider,
    tz: tzinfo,
    current: Decision,
    baseline: int | None,
    *,
    horizon: timedelta = timedelta(days=8),
    sun_horizon: timedelta = timedelta(hours=48),
    step: timedelta = timedelta(minutes=5),
    resolution: timedelta = timedelta(seconds=30),
) -> Prediction | None:
    """Predict the next commanded movement.

    Home Assistant conditions are assumed to keep their current value; time
    windows, weekdays, override expiry, override resets and the sun are
    simulated exactly.
    """
    if not inputs.enabled:
        return None

    candidates: set[datetime] = set(rule_boundaries(cfg.rules, now, now + horizon, sun, tz))
    expires = inputs.override.expires if inputs.override is not None else None
    if expires is not None and now < expires <= now + horizon:
        candidates.add(expires)
    if cfg.sun_mode is not SunMode.OFF and inputs.sun_enabled:
        epoch = datetime(2000, 1, 1, tzinfo=now.tzinfo)
        t = epoch + ((now - epoch) // step + 1) * step
        while t <= now + sun_horizon:
            candidates.add(t)
            t += step

    sim = replace(inputs, previously_active=current.active_rule_ids)
    if current.reset_override_by is not None or current.override_expired:
        sim = replace(sim, override=None)
    prev_t = now
    for t in sorted(candidates):
        decision = evaluate(cfg, sim, t, sun, tz)
        if is_move(cfg, baseline, decision.position):
            lo, hi = prev_t, t
            while hi - lo > resolution:
                mid = lo + (hi - lo) / 2
                probe = evaluate(cfg, sim, mid, sun, tz)
                if is_move(cfg, baseline, probe.position):
                    hi, decision = mid, probe
                else:
                    lo = mid
            return Prediction(hi, decision)
        if decision.reset_override_by is not None or decision.override_expired:
            sim = replace(sim, override=None)
        sim = replace(sim, previously_active=decision.active_rule_ids)
        prev_t = t
    return None
