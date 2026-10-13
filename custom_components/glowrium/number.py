"""Number platform for the Glowrium integration."""

from __future__ import annotations

import math

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberExtraStoredData,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import MODE_CIRCADIAN, MODE_SCHEDULE
from .coordinator import GlowriumCoordinator
from .entity import GlowriumSettingEntity

# Every command goes through the coordinator's one lock, so there is nothing
# left for Home Assistant to queue here, and no entity is polled.
PARALLEL_UPDATES = 0


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


def _whole_minutes(minutes: float) -> int:
    """Return a length of time as the whole minutes the lamp is written.

    The nearest minute, half a minute going up. An action can set a part of
    a minute, and in a unit chosen for the entity most values have one. Out
    of another unit a value also arrives a hair off - 65 minutes set as hours
    are a hair under 65 - and the hair is taken off before the half is looked
    for.
    """
    return math.floor(round(minutes, 6) + 0.5)


class _GlowriumNumber(GlowriumSettingEntity, RestoreNumber):
    """A number among the lamp's settings, remembered in its own unit.

    The state Home Assistant keeps for a number is in the unit it is shown
    in, and a length of time can be shown in a unit chosen for the entity:
    45 minutes shown in seconds are kept as 2700. So a number keeps its value
    in its own unit beside that state, and that is what it remembers.

    When a remembered value stands in is the settings' one rule, and is the
    same here: a number whose state was not a value remembers nothing,
    whatever was kept beside it.
    """

    _restored_data: NumberExtraStoredData | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator and recover the last known value."""
        await super().async_added_to_hass()
        self._restored_data = await self.async_get_last_number_data()

    def _restored_number(self) -> float | None:
        """Return the remembered value in the number's own unit, if there is one."""
        if self._restored is None:
            return None
        if self._restored_data is None:
            # Stored by a version that had the state alone to store. No unit
            # could be chosen then, so the state is the number's own value.
            try:
                return float(self._restored)
            except ValueError:
                return None
        # Out of a file: held to being a number, whatever its type says.
        value = self._restored_data.native_value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value


class GlowriumRampNumber(_GlowriumNumber):
    """Circadian sunrise/sunset ramp duration (key 0x2f; 0 = Sun Sync auto).

    Whole minutes, and meant to be read in minutes: in a chosen unit Home
    Assistant shows as many decimals as the value has, so in hours, none.
    """

    _attr_translation_key = "ramp"
    _attr_entity_category = EntityCategory.CONFIG
    _only_in_mode = MODE_CIRCADIAN  # the ramp is the circadian fade's
    _attr_native_min_value = 0
    _attr_native_max_value = 90
    _attr_native_step = 1
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the ramp-time number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_ramp"

    @property
    def native_value(self) -> float | None:
        """Return the ramp time in minutes."""
        return self._or_remembered(
            self._coordinator.ramp_minutes, self._restored_number
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the ramp time in minutes, to the nearest whole one."""
        await self._coordinator.async_set_ramp(_whole_minutes(value))


class _GlowriumTimerNumber(_GlowriumNumber):
    """Base for schedule numbers - only used in Schedule mode."""

    _attr_entity_category = EntityCategory.CONFIG
    _only_in_mode = MODE_SCHEDULE  # schedule settings only apply in Schedule mode


class GlowriumTimerGradual(_GlowriumTimerNumber):
    """Schedule gradual on/off fade duration, in whole minutes as the ramp is."""

    _attr_translation_key = "schedule_gradual"
    _attr_native_min_value = 0
    _attr_native_max_value = 90
    _attr_native_step = 1
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the schedule-gradual number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_gradual"

    @property
    def native_value(self) -> float | None:
        """Return the gradual fade in minutes."""
        return self._or_remembered(
            self._coordinator.schedule_gradual_minutes, self._restored_number
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the gradual fade in minutes, to the nearest whole one."""
        await self._coordinator.async_set_timer_gradual(_whole_minutes(value))


class GlowriumTimerBrightness(_GlowriumTimerNumber):
    """Schedule brightness (%)."""

    _attr_translation_key = "schedule_brightness"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the schedule-brightness number."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_schedule_brightness"

    @property
    def native_value(self) -> float | None:
        """Return the schedule brightness percentage."""
        return self._or_remembered(
            self._coordinator.schedule_brightness, self._restored_number
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the schedule brightness percentage."""
        await self._coordinator.async_set_timer_brightness(int(value))
