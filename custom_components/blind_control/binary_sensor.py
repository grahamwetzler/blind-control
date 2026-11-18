"""Binary sensors for Blind Control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .controller import BlindControlConfigEntry, BlindController
from .entity import BlindControlEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlindControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    controller = entry.runtime_data
    async_add_entities([SunInWindowSensor(controller), OverrideActiveSensor(controller)])


class SunInWindowSensor(BlindControlEntity, BinarySensorEntity):
    """On while the sun shines into the window."""

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "sun_in_window")

    @property
    def is_on(self) -> bool | None:
        """Return True if the sun is in the window."""
        d = self.controller.decision
        return d.sun.in_window if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return sun details."""
        d = self.controller.decision
        return d.sun.as_dict() if d else None


class OverrideActiveSensor(BlindControlEntity, BinarySensorEntity):
    """On while an override (manual or via action) is in effect."""

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "override_active")

    @property
    def is_on(self) -> bool:
        """Return True if an override exists."""
        return self.controller.override is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return override details."""
        ov = self.controller.override
        d = self.controller.decision
        return {
            **(ov.as_dict() if ov else {}),
            "in_effect": bool(d and d.source.value == "override"),
            "last_event": self.controller.last_override_event,
        }
