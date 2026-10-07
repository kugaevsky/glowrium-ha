"""Switch platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import KEY_INDICATOR
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
    """Set up the Glowrium switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        [GlowriumIndicatorSwitch(coordinator), GlowriumDstSwitch(coordinator)]
    )


class GlowriumIndicatorSwitch(GlowriumSettingEntity, SwitchEntity):
    """The device's status indicator LED (key 0x17).

    A setting of the lamp, not what the lamp is for: as a `config` entity it
    is passed over by an action aimed at a room, which means the light.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "indicator"

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the indicator switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_indicator"

    @property
    def is_on(self) -> bool | None:
        """Return whether the indicator LED is on."""
        value = self._coordinator.state.get(KEY_INDICATOR)
        if value is None:
            return self._restored_bool()
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the indicator LED on."""
        await self._coordinator.async_set_indicator(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the indicator LED off."""
        await self._coordinator.async_set_indicator(False)


class GlowriumDstSwitch(GlowriumSettingEntity, SwitchEntity):
    """Daylight-saving-time handling (key 0x35, byte 0).

    A `config` entity, and it matters that it is: on the one lamp it was
    watched on, the flag put the lamp's own program an hour out, and "turn on
    every switch in this room" used to set it with nothing to say why.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "dst"

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the DST switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_dst"

    @property
    def is_on(self) -> bool | None:
        """Return whether DST is enabled."""
        enabled = self._coordinator.dst_enabled
        return enabled if enabled is not None else self._restored_bool()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable DST."""
        await self._coordinator.async_set_dst(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable DST."""
        await self._coordinator.async_set_dst(False)
