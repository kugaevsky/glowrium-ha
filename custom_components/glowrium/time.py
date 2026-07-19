"""Time platform (schedule start/end) for the Glowrium integration."""

from __future__ import annotations

import datetime
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import (
    KEY_TIMER,
    MODE_SCHEDULE,
    TIMER_DEFAULT,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_START_H,
    TIMER_START_M,
)
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

    def _slot(self) -> bytes | None:
        value = self._coordinator.state.get(KEY_TIMER)
        if isinstance(value, (bytes, bytearray)) and len(value) >= len(TIMER_DEFAULT):
            return bytes(value)
        return None


class GlowriumTimerStart(_GlowriumTimerTime):
    """Schedule start (on) time."""

    _attr_translation_key = "schedule_start"

    def __init__(self, coordinator: Any) -> None:
        """Initialize the schedule-start time."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_start"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the schedule start time."""
        slot = self._slot()
        if slot is None:
            return None
        return datetime.time(slot[TIMER_START_H], slot[TIMER_START_M])

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the schedule start time."""
        await self._coordinator.async_set_timer_start(value.hour, value.minute)


class GlowriumTimerEnd(_GlowriumTimerTime):
    """Schedule end (off) time."""

    _attr_translation_key = "schedule_end"

    def __init__(self, coordinator: Any) -> None:
        """Initialize the schedule-end time."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_end"

    @property
    def native_value(self) -> datetime.time | None:
        """Return the schedule end time."""
        slot = self._slot()
        if slot is None:
            return None
        return datetime.time(slot[TIMER_END_H], slot[TIMER_END_M])

    async def async_set_value(self, value: datetime.time) -> None:
        """Set the schedule end time."""
        await self._coordinator.async_set_timer_end(value.hour, value.minute)
