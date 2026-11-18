"""Buttons for Blind Control."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up buttons."""
    controller = entry.runtime_data
    async_add_entities([ClearOverrideButton(controller), RecalculateButton(controller)])


class ClearOverrideButton(BlindControlEntity, ButtonEntity):
    """Return to automatic control."""

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "clear_override")

    async def async_press(self) -> None:
        """Clear the override."""
        self.controller.clear_override()


class RecalculateButton(BlindControlEntity, ButtonEntity):
    """Re-evaluate and re-apply the target position."""

    def __init__(self, controller: BlindController) -> None:
        """Initialise."""
        super().__init__(controller, "recalculate")

    async def async_press(self) -> None:
        """Recalculate."""
        self.controller.evaluate("recalculate button", force=True)
