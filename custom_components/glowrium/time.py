"""Time platform (schedule start/end) for the Glowrium integration."""

from __future__ import annotations

import datetime

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import MODE_SCHEDULE
from .coordinator import GlowriumCoordinator
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium schedule times."""
    coordinator = entry.runtime_data
    async_add_entities([GlowriumTimerStart(coordinator), GlowriumTimerEnd(coordinator)])


class _GlowriumTimerTime(GlowriumEntity, TimeEntity):
    """Base for the schedule start/end times - only used in Schedule mode."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        """Schedule times only apply in Schedule mode."""
        return super().available and self._coordinator.mode_allows(MODE_SCHEDULE)


class GlowriumTimerStart(_GlowriumTimerTime):
    """Schedule start (on) time."""

    _attr_translation_key = "schedule_start"

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the schedule-start time."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_start"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the schedule start time."""
        return self._coordinator.schedule_start

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the schedule start time."""
        await self._coordinator.async_set_timer_start(value.hour, value.minute)


class GlowriumTimerEnd(_GlowriumTimerTime):
    """Schedule end (off) time."""

    _attr_translation_key = "schedule_end"

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the schedule-end time."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_end"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the schedule end time."""
        return self._coordinator.schedule_end

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the schedule end time."""
        await self._coordinator.async_set_timer_end(value.hour, value.minute)
