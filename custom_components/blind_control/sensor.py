"""Sensors explaining what Blind Control is doing and why."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .controller import BlindControlConfigEntry, BlindController
from .engine import Source
from .entity import BlindControlEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BlindSensorDescription(SensorEntityDescription):
    """Sensor description with value / attribute accessors."""

    value_fn: Callable[[BlindController], Any]
    attrs_fn: Callable[[BlindController], dict[str, Any]] | None = None


def _status(c: BlindController) -> str | None:
    return c.decision.source.value if c.decision else None


def _override_expires(c: BlindController) -> datetime | None:
    return c.override.expires if c.override else None


SENSORS: tuple[BlindSensorDescription, ...] = (
    BlindSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in Source],
        value_fn=_status,
        attrs_fn=lambda c: c.status_attributes(),
    ),
    BlindSensorDescription(
        key="reason",
        value_fn=lambda c: c.decision.reason[:255] if c.decision else None,
    ),
    BlindSensorDescription(
        key="target_position",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda c: c.decision.position if c.decision else None,
        attrs_fn=lambda c: {"cover_positions": c.cover_positions()},
    ),
    BlindSensorDescription(
        key="next_action",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.next_action.time if c.next_action else None,
        attrs_fn=lambda c: c.next_action_attributes(),
    ),
    BlindSensorDescription(
        key="next_action_summary",
        value_fn=lambda c: c.next_action_text(),
        attrs_fn=lambda c: c.next_action_attributes(),
    ),
    BlindSensorDescription(
        key="override_expires",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_override_expires,
    ),
    BlindSensorDescription(
        key="sun_shade_position",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.decision.sun.shade_position if c.decision else None,
        attrs_fn=lambda c: c.decision.sun.as_dict() if c.decision else {},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlindControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_entities(BlindSensor(entry.runtime_data, d) for d in SENSORS)


class BlindSensor(BlindControlEntity, SensorEntity):
    """Blind Control sensor."""

    entity_description: BlindSensorDescription

    def __init__(self, controller: BlindController, description: BlindSensorDescription) -> None:
        """Initialise."""
        super().__init__(controller, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the value."""
        return self.entity_description.value_fn(self.controller)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return explanatory attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.controller)
