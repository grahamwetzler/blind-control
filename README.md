# Blind Control for Home Assistant

Rule-, override-, oversteer- and sun-based control for roller shades and other
covers. It is a native Home Assistant re-imagining of the *blind-control* node
from [node-red-contrib-sun-position](https://github.com/rdmtc/node-red-contrib-sun-position):
everything is configured in the UI, and it always tells you:

- **what** the shade is doing (`sensor.<name>_status`, `sensor.<name>_target_position`)
- **why** (`sensor.<name>_reason`, plus a full evaluation trace in the status attributes)
- **what happens next** (`sensor.<name>_next_action`: when, to which position, and why)

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → add this repository as type **Integration**.
2. Install **Blind Control** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → Blind Control*.

Create one entry per group of covers that should move together (e.g. "Living
room south windows").

## Concepts

| node-red-contrib-sun-position | Blind Control | Where |
|---|---|---|
| Blind level (0 = closed … 1 = open) | Position 0–100 % (HA convention: 0 closed, 100 open) | — |
| Rules (time, conditions, absolute / min / max level) | **Rule** subentries | Integration page → *Add rule* |
| Rule conditions on flow/global/msg values | Native **Home Assistant conditions** (state, numeric state, template, zone, …) | Rule form |
| Time limits "from / until", sun times with offset, earliest/latest | Start / end: fixed time or dawn / sunrise / noon / sunset / dusk, offset, *not before*, *not after* | Rule form |
| Day-of-week limits | Weekdays | Rule form |
| Manual override via message, expiry, priority/importance | **Overrides**: auto-detected manual changes, the `blind_control.set_override` action, expiry, priority | Options + actions |
| "Reset override" by rule | *Clear overrides when the rule starts* | Rule form |
| Sun control: restrict / maximize, window azimuth & geometry, floor length | **Sun control**: *restrict* / *maximize*, window direction & field of view, elevation limits, window heights, allowed sun distance | Options |
| Oversteer 1–3 (value/threshold → level, "only if sun in window") | **Oversteer** subentries: HA conditions → position, *only while the sun is in the window*; any number, ordered | Integration page → *Add oversteer* |
| Min delta / blind increment | Minimum change | Options |
| `msg.blindCtrl.reason` | `sensor.<name>_reason` + `rules` attribute | Entities |
| Auto trigger time | `sensor.<name>_next_action` (+ summary) | Entities |

### Decision order

Every evaluation walks this list from the top; the first step that produces a
position wins:

1. **Automation switched off** → nothing is sent.
2. **Override** → its position. A *Position* rule only wins if its **priority is
   higher** than the override's (e.g. a storm or fire rule with priority 50).
3. **Position rule** → the first active one, by *order*.
4. **Sun control** (if a sun mode is set and the *Sun control* switch is on):
   1. **Oversteer** → first matching one, by order.
   2. **Sun in window** → *restrict*: the fixed shade position; *maximize*: as
      open as possible while keeping direct sun within the allowed floor distance.
   3. Sun not in window → default position.
5. **Default position**.

Active *Minimum* / *Maximum* rules then limit the result of steps 4–5 (not
overrides or Position rules), and the global min/max limits clamp everything
except overrides.

A rule is **active** while *all* of these hold: inside its time window (windows
may cross midnight, e.g. sunset → sunrise; the weekday is checked for the day the
window **starts**), on a selected weekday, and its conditions are met. A rule with
no start or end applies all day.

### Overrides

- **Manual detection** (on by default): if a cover moves and Blind Control didn't
  command it (wall switch, remote, vendor app, dashboard), the new position
  becomes a manual override. Blind Control tells its own movements apart using
  the HA context of its commands, the position tolerance, and intermediate
  positions while a commanded move is still travelling.
- **Groups**: an override pauses the whole controller. A manual move of one
  cover does **not** move the other covers in the group. They stay where they
  are until the override ends, so adjusting one shade by hand never makes the
  others jump. An override set with `blind_control.set_override` moves every
  cover.
- **Delivery**: a cover that was unavailable, didn't respond, or was just added
  to the group gets the current target when it comes back (up to 3 attempts).
- **Expiry**: after *Override duration* (0 = until cleared). The *Override
  expires* sensor shows when.
- **Reset by rule**: a rule with *Clear overrides when the rule starts* clears
  any override when it becomes active. Example: close at sunset even if someone
  opened the shade in the afternoon.
- **Priority** (Node-RED *importance*): manual overrides get the *Manual
  override priority* (default 1). Rules have priority 0 by default, so
  overrides win unless you raise a rule's priority.
  - A new override with a **lower** priority than the active one is
    **ignored** (equal or higher replaces it), unless `force: true` is set.
  - A manual move during a higher-priority override is undone: the covers go
    back to the override's position. Example: an *Away* override at priority 50
    can't be knocked out by a wall switch. A new manual move always replaces an
    earlier manual override.
  - `blind_control.clear_override` with a `priority` only clears an override of
    that priority or lower. Without one it always clears, as do the *Clear
    override* button and rules with *Clear overrides when the rule starts*.
  - Ignored overrides and clears are logged and shown in the *Override active*
    sensor's `last_event` attribute.
- Overrides, switch states and the last commanded position survive restarts.

## Entities

| Entity | Description |
|---|---|
| `sensor.*_status` | `override`, `rule`, `oversteer`, `sun`, `default`, `disabled`. Attributes: reason, target and actual cover positions, active rule/oversteer/override, limits applied, sun details, **every rule with why it is or isn't active**, trigger of the last evaluation |
| `sensor.*_reason` | Human-readable explanation, e.g. *Rule 'Night' (sunset +30 min → sunrise) sets 0%* |
| `sensor.*_target_position` | Position Blind Control wants (%) |
| `sensor.*_next_action` | Timestamp of the next movement; attributes: position, source, reason |
| `sensor.*_next_action_summary` | e.g. *0% at 18:42 — Rule 'Night' (sunset → sunrise) sets 0%* |
| `sensor.*_override_expires` | When the current override ends |
| `sensor.*_sun_shade_position` | Position sun control would pick right now (diagnostic) |
| `binary_sensor.*_sun_in_window` | Sun shines into the window |
| `binary_sensor.*_override_active` | An override exists (attribute `in_effect` says whether it currently wins) |
| `switch.*_automation` | Master switch (pause everything) |
| `switch.*_sun_control` | Enable/disable sun control and oversteer |
| `button.*_clear_override` / `button.*_recalculate` | Return to automatic / re-evaluate and re-send |

The next action is **simulated** over the coming days: time windows, weekdays,
sun events, sun movement, override expiry and override resets are exact. Home
Assistant conditions are assumed to keep their current value. When a condition
changes, everything is re-evaluated immediately.

## Actions

```yaml
action: blind_control.set_override
data:
  config_entry_id: <pick the controller in the UI>
  position: 30
  duration: { hours: 2 }   # optional; default = override duration, 0 = until cleared
  priority: 5              # optional; default = manual override priority
  note: Movie night        # optional; shown in the reason
  force: false             # optional; replace even a higher-priority override
```

```yaml
action: blind_control.clear_override
data:
  config_entry_id: <pick the controller in the UI>
  priority: 5              # optional; only clear an override of priority <= 5
```

`blind_control.recalculate` takes only `config_entry_id`.

| Node-RED (sun-position blind-control) | Blind Control |
|---|---|
| `msg.payload` position with `msg.importance` | `set_override` with `priority` |
| `msg.expire` / override expiry | `set_override` `duration` (or *Override duration*) |
| lower importance than the active override → ignored | same; `force: true` to replace anyway |
| `resetOverwrite` with importance | `clear_override` with `priority` |
| rule importance beating an override | Position rule with a higher *priority* |
| rule *reset overwrite* | rule option *Clear overrides when the rule starts* |

## Example setup

**Living room, south-facing, 2.2 m tall windows starting 0.4 m above the floor:**

- Options: sun mode *maximize*, window direction 190°, 75° left/right, window
  top 2.2 m, bottom 0.4 m, allowed sun distance 0.8 m, default 100 %.
- Rule **Night**: sunset +30 min → sunrise (*not before* 07:00), position 0 %,
  *clear overrides when the rule starts*.
- Rule **Privacy cap**: no time window, *Maximum* 60 %, condition: `person.*` not home.
- Rule **Storm**: no time window, position 100 %, priority 50, condition: wind speed above 50 km/h.
- Oversteer **Cloudy**: condition `weather.home` cloud coverage above 80 %, position 100 %.
- Oversteer **Heat**: condition outdoor temperature above 30 °C, position 0 %, *only while the sun is in the window*.

## Development

```sh
uv venv --python 3.14 && uv pip install pytest-homeassistant-custom-component ruff
.venv/bin/pytest
```

The decision logic lives in `custom_components/blind_control/engine.py` and has
no Home Assistant dependencies. `controller.py` connects it to Home Assistant.
