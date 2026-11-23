"""Constants for Blind Control."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "blind_control"
LOGGER = logging.getLogger(__package__)

SUBENTRY_RULE: Final = "rule"
SUBENTRY_OVERSTEER: Final = "oversteer"

# Options (stored flat on the config entry)
CONF_COVERS: Final = "covers"
CONF_DEFAULT_POSITION: Final = "default_position"
CONF_MIN_POSITION: Final = "min_position"
CONF_MAX_POSITION: Final = "max_position"
CONF_MIN_DELTA: Final = "min_delta"
CONF_SUN_MODE: Final = "sun_mode"
CONF_SUN_POSITION: Final = "sun_position"
CONF_AZIMUTH: Final = "azimuth"
CONF_AZIMUTH_LEFT: Final = "azimuth_left"
CONF_AZIMUTH_RIGHT: Final = "azimuth_right"
CONF_MIN_ELEVATION: Final = "min_elevation"
CONF_MAX_ELEVATION: Final = "max_elevation"
CONF_WINDOW_TOP: Final = "window_top"
CONF_WINDOW_BOTTOM: Final = "window_bottom"
CONF_FLOOR_LENGTH: Final = "floor_length"
CONF_DETECT_MANUAL: Final = "detect_manual"
CONF_OVERRIDE_DURATION: Final = "override_duration"
CONF_MANUAL_PRIORITY: Final = "manual_priority"
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_TOLERANCE: Final = "tolerance"

# Option form sections
SECTION_POSITIONS: Final = "positions"
SECTION_SUN: Final = "sun"
SECTION_WINDOW: Final = "window"
SECTION_OVERRIDE: Final = "override"

# Subentry fields
CONF_NAME: Final = "name"
CONF_ORDER: Final = "order"
CONF_ACTION: Final = "action"
CONF_POSITION: Final = "position"
CONF_WEEKDAYS: Final = "weekdays"
CONF_PRIORITY: Final = "priority"
CONF_RESET_OVERRIDE: Final = "reset_override"
CONF_CONDITIONS: Final = "conditions"
CONF_START: Final = "start"
CONF_END: Final = "end"
CONF_KIND: Final = "kind"
CONF_TIME: Final = "time"
CONF_OFFSET: Final = "offset"
CONF_NOT_BEFORE: Final = "not_before"
CONF_NOT_AFTER: Final = "not_after"
CONF_ONLY_IN_SUN: Final = "only_when_sun_in_window"

DEFAULTS: Final[dict[str, object]] = {
    CONF_DEFAULT_POSITION: 100,
    CONF_MIN_POSITION: 0,
    CONF_MAX_POSITION: 100,
    CONF_MIN_DELTA: 5,
    CONF_SUN_MODE: "off",
    CONF_SUN_POSITION: 0,
    CONF_AZIMUTH: 180,
    CONF_AZIMUTH_LEFT: 80,
    CONF_AZIMUTH_RIGHT: 80,
    CONF_MIN_ELEVATION: 0,
    CONF_MAX_ELEVATION: 90,
    CONF_WINDOW_TOP: 2.1,
    CONF_WINDOW_BOTTOM: 0.0,
    CONF_FLOOR_LENGTH: 0.5,
    CONF_DETECT_MANUAL: True,
    CONF_OVERRIDE_DURATION: 120,
    CONF_MANUAL_PRIORITY: 1,
    CONF_UPDATE_INTERVAL: 5,
    CONF_TOLERANCE: 3,
}

# Seconds after a command during which position reports that move toward the
# commanded target are treated as our own movement.
COMMAND_GRACE: Final = 180

SERVICE_SET_OVERRIDE: Final = "set_override"
SERVICE_CLEAR_OVERRIDE: Final = "clear_override"
SERVICE_RECALCULATE: Final = "recalculate"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_DURATION: Final = "duration"
ATTR_NOTE: Final = "note"
ATTR_FORCE: Final = "force"
