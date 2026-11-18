"""Diagnostics for Blind Control."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .controller import BlindControlConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BlindControlConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    c = entry.runtime_data
    return {
        "options": dict(entry.options),
        "subentries": [
            {"type": s.subentry_type, "title": s.title, "data": dict(s.data)}
            for s in entry.subentries.values()
        ],
        "enabled": c.enabled,
        "sun_enabled": c.sun_enabled,
        "status": c.status_attributes(),
        "next_action": {
            "time": c.next_action.time.isoformat() if c.next_action else None,
            **c.next_action_attributes(),
        },
    }
