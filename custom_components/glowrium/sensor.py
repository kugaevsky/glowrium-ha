"""Sensor platform for the Glowrium integration (diagnostic coordinates)."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .coordinator import GlowriumCoordinator
from .entity import GlowriumEntity
from .protocol import Unusable, known

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
            GlowriumCoordinateSensor(
                coordinator, "latitude", lambda: coordinator.latitude
            ),
            GlowriumCoordinateSensor(
                coordinator, "longitude", lambda: coordinator.longitude
            ),
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
        self,
        coordinator: GlowriumCoordinator,
        translation_key: str,
        read: Callable[[], float | Unusable | None],
    ) -> None:
        """Initialize a coordinate sensor on the coordinate that ``read`` gives."""
        super().__init__(coordinator)
        self._read = read
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{coordinator.address}_{translation_key}"

    @property
    def native_value(self) -> float | None:
        """Return the stored coordinate in degrees."""
        degrees = known(self._read())
        return None if degrees is None else round(degrees, 5)
