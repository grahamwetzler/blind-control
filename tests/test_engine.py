"""Unit tests for the pure decision engine."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.blind_control.engine import (
    ControllerConfig,
    Inputs,
    Override,
    OverrideSource,
    Oversteer,
    Rule,
    RuleAction,
    Source,
    SunMode,
    TimeKind,
    TimeSpec,
    compute_sun,
    evaluate,
    predict,
)

TZ = ZoneInfo("America/Chicago")


class FakeSun:
    """Sun rising at 06:00 due east, south at noon (60°), setting at 18:00 west."""

    def position(self, when: datetime) -> tuple[float, float]:
        local = when.astimezone(TZ)
        hours = local.hour + local.minute / 60 + local.second / 3600
        azimuth = 90 + (hours - 6) * 15
        elevation = 60 * (1 - ((hours - 12) / 6) ** 2)
        return azimuth % 360, elevation

    def event(self, kind: TimeKind, day: date) -> datetime | None:
        hour = {
            TimeKind.DAWN: 5.5,
            TimeKind.SUNRISE: 6,
            TimeKind.NOON: 12,
            TimeKind.SUNSET: 18,
            TimeKind.DUSK: 18.5,
        }[kind]
        return datetime.combine(day, time(0), TZ) + timedelta(hours=hour)


SUN = FakeSun()


def at(hour: int, minute: int = 0, day: int = 18) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)  # 18th is a Wednesday


NIGHT = Rule(
    id="night",
    name="Night",
    order=10,
    position=0,
    start=TimeSpec(TimeKind.SUNSET, offset=timedelta(minutes=30)),
    end=TimeSpec(TimeKind.SUNRISE),
    reset_override=True,
)


def run(cfg: ControllerConfig, now: datetime, **kw) -> object:
    return evaluate(cfg, Inputs(**kw), now, SUN, TZ)


def test_default_when_nothing_active() -> None:
    d = run(ControllerConfig(rules=(NIGHT,)), at(10))
    assert d.source is Source.DEFAULT
    assert d.position == 100


@pytest.mark.parametrize("hour", [19, 23, 2, 5])
def test_rule_window_crosses_midnight(hour: int) -> None:
    d = run(ControllerConfig(rules=(NIGHT,)), at(hour))
    assert d.source is Source.RULE
    assert d.position == 0
    assert "Night" in d.reason


def test_rule_window_boundaries() -> None:
    cfg = ControllerConfig(rules=(NIGHT,))
    assert run(cfg, at(18, 29)).source is Source.DEFAULT
    assert run(cfg, at(18, 30)).source is Source.RULE
    assert run(cfg, at(6, 0)).source is Source.DEFAULT


def test_not_before_clamp() -> None:
    rule = Rule(
        id="r",
        name="Wake",
        position=100,
        start=TimeSpec(TimeKind.SUNRISE, not_before=time(7, 30)),
        end=TimeSpec(TimeKind.TIME, at=time(9)),
    )
    cfg = ControllerConfig(default_position=0, rules=(rule,))
    assert run(cfg, at(7)).source is Source.DEFAULT
    assert run(cfg, at(7, 30)).source is Source.RULE


def test_weekdays_use_start_day() -> None:
    weekend_night = Rule(
        id="r",
        name="Fri/Sat late",
        position=0,
        start=TimeSpec(TimeKind.TIME, at=time(23)),
        end=TimeSpec(TimeKind.TIME, at=time(9)),
        weekdays=frozenset({4, 5}),
    )
    cfg = ControllerConfig(rules=(weekend_night,))
    assert run(cfg, at(8, day=21)).source is Source.RULE  # Sat morning, started Fri
    assert run(cfg, at(8, day=19)).source is Source.DEFAULT  # Thu morning


def test_conditions_gate_rules() -> None:
    rule = Rule(id="away", name="Away", position=0, has_conditions=True)
    cfg = ControllerConfig(rules=(rule,))
    assert run(cfg, at(10)).source is Source.DEFAULT
    assert run(cfg, at(10), conditions={"away": True}).source is Source.RULE
    d = run(cfg, at(10))
    assert d.rules[0].detail == "conditions not met"


def test_first_position_rule_by_order_wins() -> None:
    a = Rule(id="a", name="A", order=20, position=30)
    b = Rule(id="b", name="B", order=10, position=60)
    assert run(ControllerConfig(rules=(a, b)), at(10)).position == 60


def test_limits_apply_to_automatic_positions_only() -> None:
    cap = Rule(id="cap", name="Privacy", action=RuleAction.MAX, position=40)
    d = run(ControllerConfig(rules=(cap,)), at(10))
    assert d.source is Source.DEFAULT
    assert d.position == 40
    assert "Privacy" in d.reason
    absolute = Rule(id="abs", name="Open", position=100)
    d = run(ControllerConfig(rules=(cap, absolute)), at(10))
    assert d.position == 100


def test_global_limits_clamp_rules() -> None:
    cfg = ControllerConfig(min_position=10, rules=(NIGHT,))
    assert run(cfg, at(22)).position == 10


def override(position: int, priority: int = 1, expires: datetime | None = None) -> Override:
    return Override(position, OverrideSource.MANUAL, priority, at(12), expires)


def test_override_beats_rule_with_equal_priority() -> None:
    cfg = ControllerConfig(rules=(NIGHT,))
    d = run(cfg, at(22), override=override(70))
    assert d.source is Source.OVERRIDE
    assert d.position == 70


def test_higher_priority_rule_beats_override() -> None:
    storm = Rule(id="s", name="Storm", position=100, priority=5, has_conditions=True)
    cfg = ControllerConfig(rules=(storm,))
    d = run(cfg, at(10), override=override(20), conditions={"s": True})
    assert d.source is Source.RULE
    assert "outranks" in d.reason
    assert d.override is not None  # override stays and resumes afterwards


def test_override_expiry() -> None:
    d = run(ControllerConfig(), at(14), override=override(20, expires=at(13)))
    assert d.override_expired
    assert d.source is Source.DEFAULT


def test_rule_start_resets_override() -> None:
    cfg = ControllerConfig(rules=(NIGHT,))
    d = run(cfg, at(19), override=override(70), previously_active=frozenset())
    assert d.reset_override_by is NIGHT
    assert d.source is Source.RULE
    # Already active before: no reset.
    d = run(cfg, at(19), override=override(70), previously_active=frozenset({"night"}))
    assert d.source is Source.OVERRIDE


def test_disabled() -> None:
    d = run(ControllerConfig(rules=(NIGHT,)), at(22), enabled=False)
    assert d.source is Source.DISABLED
    assert d.position is None


SOUTH = dict(azimuth=180.0, azimuth_left=60.0, azimuth_right=60.0)


def test_sun_restrict() -> None:
    cfg = ControllerConfig(sun_mode=SunMode.RESTRICT, sun_position=20, **SOUTH)
    assert run(cfg, at(12)).source is Source.SUN
    assert run(cfg, at(12)).position == 20
    d = run(cfg, at(7))  # azimuth 105 -> outside window on the left
    assert d.source is Source.DEFAULT
    assert "outside the window (left)" in d.reason
    assert run(cfg, at(12), sun_enabled=False).source is Source.DEFAULT


def test_sun_maximize_geometry() -> None:
    cfg = ControllerConfig(
        sun_mode=SunMode.MAXIMIZE, window_bottom=0.0, window_top=2.0, floor_length=1.0, **SOUTH
    )
    # Elevation 45° straight on: edge height = 1.0 m -> 50 %.
    state = compute_sun(cfg, 180, 45)
    assert state.shade_position == 50
    # Lower sun -> more closed.
    assert compute_sun(cfg, 180, 20).shade_position < 50
    # Oblique sun raises the profile angle -> more open.
    assert compute_sun(cfg, 230, 45).shade_position > 50


def test_azimuth_wraparound() -> None:
    cfg = ControllerConfig(
        azimuth=350, azimuth_left=30, azimuth_right=30, sun_mode=SunMode.RESTRICT
    )
    assert compute_sun(cfg, 10, 30).in_window
    assert not compute_sun(cfg, 30, 30).in_window


def test_oversteer() -> None:
    cloudy = Oversteer(id="c", name="Cloudy", position=100)
    hot = Oversteer(id="h", name="Hot", position=0, only_when_sun_in_window=False, order=20)
    cfg = ControllerConfig(
        sun_mode=SunMode.RESTRICT, sun_position=20, oversteers=(cloudy, hot), **SOUTH
    )
    d = run(cfg, at(12), conditions={"c": True})
    assert d.source is Source.OVERSTEER
    assert d.position == 100
    # Sun outside window: only the oversteer not bound to the sun applies.
    d = run(cfg, at(7), conditions={"c": True, "h": True})
    assert d.oversteer is not None and d.oversteer.name == "Hot"
    # Absolute rules beat oversteer.
    cfg2 = ControllerConfig(
        sun_mode=SunMode.RESTRICT,
        oversteers=(cloudy,),
        rules=(Rule(id="r", name="R", position=50),),
        **SOUTH,
    )
    assert run(cfg2, at(12), conditions={"c": True}).source is Source.RULE


def _predict(cfg: ControllerConfig, now: datetime, **kw):
    inputs = Inputs(**kw)
    current = evaluate(cfg, inputs, now, SUN, TZ)
    return predict(cfg, inputs, now, SUN, TZ, current, current.position)


def test_predict_rule_start() -> None:
    p = _predict(ControllerConfig(rules=(NIGHT,)), at(10))
    assert p is not None
    assert p.time == at(18, 30)
    assert p.decision.position == 0


def test_predict_override_expiry() -> None:
    p = _predict(ControllerConfig(), at(10), override=override(20, expires=at(11, 15)))
    assert p is not None
    assert p.time == at(11, 15)
    assert p.decision.position == 100


def test_predict_reset_by_rule() -> None:
    p = _predict(ControllerConfig(rules=(NIGHT,)), at(14), override=override(70))
    assert p is not None
    assert p.time == at(18, 30)
    assert p.decision.source is Source.RULE


def test_predict_sun_entering_window() -> None:
    cfg = ControllerConfig(sun_mode=SunMode.RESTRICT, sun_position=20, **SOUTH)
    p = _predict(cfg, at(6, 30))
    assert p is not None
    # Window starts at azimuth 120 -> 08:00 for the fake sun.
    assert abs((p.time - at(8)).total_seconds()) <= 30
    assert p.decision.source is Source.SUN


def test_predict_none_when_static() -> None:
    assert _predict(ControllerConfig(), at(10)) is None
