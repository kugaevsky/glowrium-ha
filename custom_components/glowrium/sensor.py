"""Sensor platform for the Glowrium integration (diagnostic coordinates)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import KEY_LATITUDE, KEY_LONGITUDE
from .coordinator import GlowriumCoordinator
from .entity import GlowriumEntity

# Nothing here is polled and nothing takes a command: state is pushed by the
# coordinator.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GlowriumCoordinateSensor(coordinator, KEY_LATITUDE, "latitude"),
            GlowriumCoordinateSensor(coordinator, KEY_LONGITUDE, "longitude"),
        ]
    )


class GlowriumCoordinateSensor(GlowriumEntity, SensorEntity):
    """The latitude/longitude the device uses to compute its circadian curve.

    Disabled until somebody asks for it. After Sync location this is where
    the home is, and enabled it sits in a state and goes into the recorder's
    history on every installation, for the few who ever look. Home Assistant
    takes this default only when it first registers the entity, so one that
    an installation already has stays as it is.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: GlowriumCoordinator, key: int, translation_key: str
    ) -> None:
        """Initialize a coordinate sensor."""
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{coordinator.address}_{translation_key}"

    @property
    def native_value(self) -> float | None:
        """Return the stored coordinate in degrees."""
        value = self._coordinator.state.get(self._key)
        return round(value, 5) if isinstance(value, float) else None
