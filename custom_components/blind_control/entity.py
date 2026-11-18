"""Base entity for Blind Control."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import BlindController


class BlindControlEntity(Entity):
    """Entity bound to a controller; pushed, never polled."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: BlindController, key: str) -> None:
        """Initialise."""
        self.controller = controller
        entry = controller.entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Blind Control",
            model=f"{len(controller.covers)} cover(s)",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller updates."""
        self.async_on_remove(self.controller.async_add_listener(self.async_write_ha_state))
