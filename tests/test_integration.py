"""Integration tests running inside Home Assistant."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.blind_control.const import DEFAULTS, DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

COVER = "cover.living_room"
FEATURES = 15  # open | close | set_position | stop


def _options(**kw):
    return {**DEFAULTS, "covers": [COVER], **kw}


@pytest.fixture
async def setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Set up HA at local noon with one cover."""
    await hass.config.async_set_time_zone("America/Chicago")
    hass.config.latitude, hass.config.longitude = 41.88, -87.63
    freezer.move_to("2026-06-17 12:00:00-05:00")
    hass.states.async_set(COVER, "open", {"current_position": 100, "supported_features": FEATURES})
    calls: list[ServiceCall] = []

    async def set_position(call: ServiceCall) -> None:
        """Record the call and move the fake cover, like a real device would."""
        calls.append(call)
        hass.states.async_set(
            COVER,
            "open" if call.data["position"] else "closed",
            {"current_position": call.data["position"], "supported_features": FEATURES},
            context=call.context,
        )

    hass.services.async_register("cover", "set_cover_position", set_position)

    async def _setup(options=None, subentries=()):
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Living room",
            options=options or _options(),
            subentries_data=list(subentries),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup, calls


def rule(name, position, *, start=None, end=None, **kw) -> ConfigSubentryData:
    return ConfigSubentryData(
        subentry_type="rule",
        title=name,
        unique_id=None,
        data={
            "name": name,
            "action": "position",
            "position": position,
            "start": start or {"kind": "none"},
            "end": end or {"kind": "none"},
            "weekdays": [],
            "order": 10,
            "priority": 0,
            "reset_override": False,
            **kw,
        },
    )


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Office", "covers": [COVER]}
    )
    assert result["step_id"] == "settings"
    sections = {
        "positions": {
            "default_position": 100,
            "min_position": 0,
            "max_position": 100,
            "min_delta": 5,
        },
        "sun": {
            "sun_mode": "maximize",
            "sun_position": 0,
            "azimuth": 200,
            "azimuth_left": 70,
            "azimuth_right": 70,
            "min_elevation": 5,
            "max_elevation": 90,
        },
        "window": {"window_top": 2.2, "window_bottom": 0.4, "floor_length": 0.8},
        "override": {
            "detect_manual": True,
            "override_duration": 60,
            "manual_priority": 1,
            "tolerance": 3,
            "update_interval": 5,
        },
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sections)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Office"
    assert result["options"]["sun_mode"] == "maximize"
    assert result["options"]["covers"] == [COVER]
    assert result["options"]["floor_length"] == 0.8


async def test_rule_subentry_flow(hass: HomeAssistant, setup) -> None:
    do_setup, _ = setup
    entry = await do_setup()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "rule"), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Afternoon",
            "action": "position",
            "position": 30,
            "start": {"kind": "time", "time": "11:00:00", "offset": 0},
            "end": {"kind": "sunset", "offset": -30},
            "conditions": [
                {"condition": "state", "entity_id": "input_boolean.home", "state": "on"}
            ],
            "order": 10,
            "priority": 0,
            "reset_override": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()  # entry reloads
    status = hass.states.get("sensor.living_room_status")
    rules = status.attributes["rules"]
    assert rules[0]["name"] == "Afternoon"
    assert rules[0]["detail"] == "conditions not met"
    hass.states.async_set("input_boolean.home", "on")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "rule"
    assert hass.states.get("sensor.living_room_target_position").state == "30"


async def test_rule_moves_cover_and_reports(hass: HomeAssistant, setup) -> None:
    do_setup, calls = setup
    await do_setup(
        subentries=[
            rule(
                "Midday",
                40,
                start={"kind": "time", "time": "11:00:00"},
                end={"kind": "time", "time": "15:00:00"},
            )
        ]
    )
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": COVER, "position": 40}
    status = hass.states.get("sensor.living_room_status")
    assert status.state == "rule"
    assert "Midday" in hass.states.get("sensor.living_room_reason").state
    nxt = hass.states.get("sensor.living_room_next_action")
    assert dt_util.parse_datetime(nxt.state) == dt_util.parse_datetime("2026-06-17T15:00:00-05:00")
    assert nxt.attributes["position"] == 100
    assert "100% at 15:00" in hass.states.get("sensor.living_room_next_action_summary").state


async def test_manual_change_creates_override(
    hass: HomeAssistant, setup, freezer: FrozenDateTimeFactory
) -> None:
    do_setup, calls = setup
    await do_setup(subentries=[rule("Midday", 40)])
    assert len(calls) == 1
    # Our own movement arriving at the target (new device context): not an override.
    hass.states.async_set(COVER, "open", {"current_position": 41, "supported_features": FEATURES})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.living_room_override_active").state == "off"

    # Someone moves it with the wall switch.
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(
        COVER, "open", {"current_position": 80, "supported_features": FEATURES}, context=Context()
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.living_room_override_active").state == "on"
    assert hass.states.get("sensor.living_room_status").state == "override"
    assert "Manual override to 80%" in hass.states.get("sensor.living_room_reason").state
    assert len(calls) == 1  # no fighting the user

    # Expires after the default duration and returns to the rule.
    freezer.tick(timedelta(minutes=121))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "rule"
    assert len(calls) == 2
    assert calls[-1].data["position"] == 40


async def test_set_and_clear_override_actions(hass: HomeAssistant, setup) -> None:
    do_setup, calls = setup
    entry = await do_setup()
    assert not calls  # default 100 and the cover already is at 100
    await hass.services.async_call(
        DOMAIN,
        "set_override",
        {
            "config_entry_id": entry.entry_id,
            "position": 25,
            "duration": {"minutes": 30},
            "note": "movie",
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert calls[-1].data["position"] == 25
    assert "movie" in hass.states.get("sensor.living_room_reason").state
    expires = hass.states.get("sensor.living_room_override_expires").state
    assert dt_util.parse_datetime(expires) == dt_util.parse_datetime("2026-06-17T12:30:00-05:00")

    hass.states.async_set(COVER, "open", {"current_position": 25, "supported_features": FEATURES})
    await hass.services.async_call(
        DOMAIN, "clear_override", {"config_entry_id": entry.entry_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "default"
    assert calls[-1].data["position"] == 100


async def test_automation_switch(hass: HomeAssistant, setup) -> None:
    do_setup, _ = setup
    await do_setup()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.living_room_automation"}, blocking=True
    )
    assert hass.states.get("sensor.living_room_status").state == "disabled"
    assert hass.states.get("sensor.living_room_next_action").state == "unknown"


async def test_sun_maximize(hass: HomeAssistant, setup) -> None:
    do_setup, calls = setup
    await do_setup(
        options=_options(
            sun_mode="maximize",
            azimuth=180,
            azimuth_left=89,
            azimuth_right=89,
            window_top=2.0,
            window_bottom=0.0,
            floor_length=0.3,
        )
    )
    status = hass.states.get("sensor.living_room_status")
    assert status.state == "sun"
    assert hass.states.get("binary_sensor.living_room_sun_in_window").state == "on"
    target = int(hass.states.get("sensor.living_room_target_position").state)
    assert 0 < target < 100
    assert calls[-1].data["position"] == target
    assert hass.states.get("sensor.living_room_next_action").state != "unknown"


async def test_override_survives_restart(hass: HomeAssistant, setup) -> None:
    do_setup, _ = setup
    entry = await do_setup()
    await hass.services.async_call(
        DOMAIN, "set_override", {"config_entry_id": entry.entry_id, "position": 10}, blocking=True
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "override"


async def test_options_flow(hass: HomeAssistant, setup) -> None:
    do_setup, _ = setup
    entry = await do_setup()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "covers": [COVER],
            "positions": {
                "default_position": 100,
                "min_position": 50,
                "max_position": 40,
                "min_delta": 5,
            },
            "sun": {
                "sun_mode": "off",
                "sun_position": 0,
                "azimuth": 180,
                "azimuth_left": 80,
                "azimuth_right": 80,
                "min_elevation": 0,
                "max_elevation": 90,
            },
            "window": {"window_top": 2.1, "window_bottom": 0.0, "floor_length": 0.5},
            "override": {
                "detect_manual": True,
                "override_duration": 120,
                "manual_priority": 1,
                "tolerance": 3,
                "update_interval": 5,
            },
        },
    )
    assert result["errors"] == {"base": "min_above_max"}


async def test_oversteer_subentry(hass: HomeAssistant, setup) -> None:
    do_setup, calls = setup
    entry = await do_setup(
        options=_options(
            sun_mode="restrict", sun_position=20, azimuth=180, azimuth_left=89, azimuth_right=89
        )
    )
    assert hass.states.get("sensor.living_room_status").state == "sun"
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "oversteer"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Cloudy",
            "conditions": [
                {
                    "condition": "numeric_state",
                    "entity_id": "sensor.cloud_cover",
                    "above": 80,
                }
            ],
            "position": 100,
            "only_when_sun_in_window": True,
            "order": 10,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    hass.states.async_set("sensor.cloud_cover", "95")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "oversteer"
    assert calls[-1].data["position"] == 100
    hass.states.async_set("sensor.cloud_cover", "10")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_status").state == "sun"
    assert calls[-1].data["position"] == 20


async def test_reset_override_rule(
    hass: HomeAssistant, setup, freezer: FrozenDateTimeFactory
) -> None:
    do_setup, calls = setup
    entry = await do_setup(
        subentries=[
            rule(
                "Evening",
                0,
                start={"kind": "time", "time": "18:00:00"},
                end={"kind": "time", "time": "23:00:00"},
                reset_override=True,
            )
        ]
    )
    await hass.services.async_call(
        DOMAIN,
        "set_override",
        {"config_entry_id": entry.entry_id, "position": 60, "duration": {"hours": 0}},
        blocking=True,
    )
    assert hass.states.get("sensor.living_room_override_expires").state == "unknown"
    nxt = hass.states.get("sensor.living_room_next_action")
    assert dt_util.parse_datetime(nxt.state) == dt_util.parse_datetime("2026-06-17T18:00:00-05:00")
    freezer.move_to("2026-06-17 18:00:05-05:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.living_room_override_active").state == "off"
    assert hass.states.get("sensor.living_room_status").state == "rule"
    assert calls[-1].data["position"] == 0
