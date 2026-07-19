"""Number platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import (
    KEY_RAMP,
    KEY_TIMER,
    MODE_CIRCADIAN,
    MODE_SCHEDULE,
    TIMER_BRIGHTNESS,
    TIMER_DEFAULT,
    TIMER_GRADUAL,
)
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium numbers."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GlowriumRampNumber(coordinator),
            GlowriumTimerGradual(coordinator),
            GlowriumTimerBrightness(coordinator),
        ]
    )


class GlowriumRampNumber(GlowriumEntity, NumberEntity):
    """Circadian sunrise/sunset ramp duration (key 0x2f; 0 = Sun Sync auto)."""

    _attr_translation_key = "ramp"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 90
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: Any) -> None:
        """Initialize the ramp-time number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_ramp"

    @property
    def available(self) -> bool:
        """The ramp only applies to the Circadian sunrise/sunset fade."""
        return super().available and self._coordinator.mode_allows(MODE_CIRCADIAN)

    @property
    def native_value(self) -> int | None:
        """Return the ramp time in minutes."""
        value = self._coordinator.state.get(KEY_RAMP)
        if isinstance(value, (bytes, bytearray)) and value:
            return int.from_bytes(value[:2], "big") // 60
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the ramp time in minutes."""
        await self._coordinator.async_set_ramp(int(value))


class _GlowriumTimerNumber(GlowriumEntity, NumberEntity):
    """Base for schedule numbers - only used in Schedule mode."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        """Schedule settings only apply in Schedule mode."""
        return super().available and self._coordinator.mode_allows(MODE_SCHEDULE)

    def _slot(self) -> bytes | None:
        value = self._coordinator.state.get(KEY_TIMER)
        if isinstance(value, (bytes, bytearray)) and len(value) >= len(TIMER_DEFAULT):
            return bytes(value)
        return None


class GlowriumTimerGradual(_GlowriumTimerNumber):
    """Schedule gradual on/off fade duration (minutes)."""

    _attr_translation_key = "schedule_gradual"
    _attr_native_min_value = 0
    _attr_native_max_value = 90
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: Any) -> None:
        """Initialize the schedule-gradual number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_gradual"

    @property
    def native_value(self) -> int | None:
        """Return the gradual fade in minutes."""
        slot = self._slot()
        if slot is None:
            return None
        return int.from_bytes(slot[TIMER_GRADUAL : TIMER_GRADUAL + 2], "big") // 60

    async def async_set_native_value(self, value: float) -> None:
        """Set the gradual fade in minutes."""
        await self._coordinator.async_set_timer_gradual(int(value))


class GlowriumTimerBrightness(_GlowriumTimerNumber):
    """Schedule brightness (%)."""

    _attr_translation_key = "schedule_brightness"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: Any) -> None:
        """Initialize the schedule-brightness number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_brightness"

    @property
    def native_value(self) -> int | None:
        """Return the schedule brightness percentage."""
        slot = self._slot()
        return slot[TIMER_BRIGHTNESS] if slot is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Set the schedule brightness percentage."""
        await self._coordinator.async_set_timer_brightness(int(value))
