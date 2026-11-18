"""Switches for Blind Control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
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
    """Set up switches."""
    controller = entry.runtime_data
    async_add_entities([AutomationSwitch(controller), SunControlSwitch(controller)])


class AutomationSwitch(BlindControlEntity, SwitchEntity):
    """Master switch for automatic control."""

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "automation")

    @property
    def is_on(self) -> bool:
        """Return True if automation is enabled."""
        return self.controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable automation."""
        self.controller.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable automation."""
        self.controller.set_enabled(False)


class SunControlSwitch(BlindControlEntity, SwitchEntity):
    """Enable or disable sun control (and oversteer)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "sun_control")

    @property
    def is_on(self) -> bool:
        """Return True if sun control is enabled."""
        return self.controller.sun_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable sun control."""
        self.controller.set_sun_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable sun control."""
        self.controller.set_sun_enabled(False)
