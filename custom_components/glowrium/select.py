"""Select platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import (
    KEY_LIGHTING_MODE,
    MODE_CIRCADIAN,
    OPERATING_MODES,
)
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium selects."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GlowriumOperatingModeSelect(coordinator),
            GlowriumLightingModeSelect(coordinator),
        ]
    )


class GlowriumOperatingModeSelect(GlowriumEntity, SelectEntity):
    """Manual / Circadian / Schedule - the mutually exclusive auto modes."""

    _attr_translation_key = "operating_mode"
    _attr_options = list(OPERATING_MODES)

    def __init__(self, coordinator: Any) -> None:
        """Initialize the operating-mode selector."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_operating_mode"

    @property
    def current_option(self) -> str:
        """Return the active operating mode."""
        return self._coordinator.operating_mode

    async def async_select_option(self, option: str) -> None:
        """Switch operating mode."""
        await self._coordinator.async_set_operating_mode(option)


class GlowriumLightingModeSelect(GlowriumEntity, SelectEntity):
    """Circadian lighting mode (Sun SYNC, Sunrise Sync, ...)."""

    _attr_translation_key = "lighting_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any) -> None:
        """Initialize the lighting-mode selector for the device's model."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_lighting_mode"
        self._modes = coordinator.model.lighting_modes
        self._attr_options = list(self._modes)
        self._by_index = {index: label for label, index in self._modes.items()}

    @property
    def available(self) -> bool:
        """Lighting modes only apply while in Circadian mode."""
        return super().available and self._coordinator.mode_allows(MODE_CIRCADIAN)

    @property
    def current_option(self) -> str | None:
        """Return the selected lighting mode, if known."""
        return self._by_index.get(self._coordinator.state.get(KEY_LIGHTING_MODE))

    async def async_select_option(self, option: str) -> None:
        """Apply the chosen lighting mode."""
        await self._coordinator.async_set_lighting_mode(self._modes[option])
