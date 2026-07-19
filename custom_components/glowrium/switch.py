"""Switch platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import KEY_DST, KEY_INDICATOR
from .entity import GlowriumEntity


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


class GlowriumIndicatorSwitch(GlowriumEntity, SwitchEntity):
    """The device's status indicator LED (key 0x17)."""

    _attr_translation_key = "indicator"

    def __init__(self, coordinator: Any) -> None:
        """Initialize the indicator switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_indicator"

    @property
    def is_on(self) -> bool | None:
        """Return whether the indicator LED is on."""
        value = self._coordinator.state.get(KEY_INDICATOR)
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the indicator LED on."""
        await self._coordinator.async_set_indicator(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the indicator LED off."""
        await self._coordinator.async_set_indicator(False)


class GlowriumDstSwitch(GlowriumEntity, SwitchEntity):
    """Daylight-saving-time handling (key 0x35, byte 0)."""

    _attr_translation_key = "dst"

    def __init__(self, coordinator: Any) -> None:
        """Initialize the DST switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_dst"

    @property
    def is_on(self) -> bool | None:
        """Return whether DST is enabled."""
        value = self._coordinator.state.get(KEY_DST)
        if isinstance(value, (bytes, bytearray)) and len(value) >= 1:
            return value[0] == 1
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable DST."""
        await self._coordinator.async_set_dst(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable DST."""
        await self._coordinator.async_set_dst(False)
