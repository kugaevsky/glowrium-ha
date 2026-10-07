"""Select platform for the Glowrium integration."""

from __future__ import annotations

from typing import Final

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
    """Set up the Glowrium selects."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GlowriumOperatingModeSelect(coordinator),
            GlowriumLightingModeSelect(coordinator),
        ]
    )


class GlowriumOperatingModeSelect(GlowriumSettingEntity, SelectEntity):
    """Manual / Circadian / Schedule - the mutually exclusive auto modes."""

    _attr_translation_key = "operating_mode"
    _attr_options = list(OPERATING_MODES)

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the operating-mode selector."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_operating_mode"

    @property
    def current_option(self) -> str | None:
        """Return the active operating mode."""
        mode = self._coordinator.operating_mode
        if mode is None and self._restored in self.options:
            return self._restored
        return mode

    async def async_select_option(self, option: str) -> None:
        """Switch operating mode."""
        await self._coordinator.async_set_operating_mode(option)


# What the lighting modes were called while the option was the preset's
# English name, before 0.3.0 made it a key. Only for reading back a value
# remembered from then; nothing is offered or accepted under these.
_NAMES_BEFORE_KEYS: Final = {
    "Sun SYNC": "sun_sync",
    "Before Sunrise": "before_sunrise",
    "Sunrise Sync": "sunrise_sync",
    "Sunset Sync": "sunset_sync",
    "After Sunset": "after_sunset",
    "Two-Phase": "two_phase",
    "Balance": "balance",
    "Enhanced Two-Phase": "enhanced_two_phase",
}


class GlowriumLightingModeSelect(GlowriumSettingEntity, SelectEntity):
    """Circadian lighting mode (Sun SYNC, Sunrise Sync, ...).

    An option is a preset's key; its name is a translation of that key.
    """

    _attr_translation_key = "lighting_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Initialize the lighting-mode selector."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_lighting_mode"

    async def async_added_to_hass(self) -> None:
        """Recover the last known mode, whichever way it was written down."""
        await super().async_added_to_hass()
        if self._restored is not None:
            self._restored = _NAMES_BEFORE_KEYS.get(self._restored, self._restored)

    @property
    def _modes(self) -> dict[str, int]:
        """The presets of the lamp's model, as it is known right now.

        Looked up each time rather than kept: the entity is built before the
        lamp has said which model it is, and the answer changes what is
        offered.
        """
        return self._coordinator.model.lighting_modes

    @property
    def options(self) -> list[str]:
        """Return the presets this model has."""
        return list(self._modes)

    @property
    def available(self) -> bool:
        """Lighting modes only apply while in Circadian mode."""
        return super().available and self._coordinator.mode_allows(MODE_CIRCADIAN)

    @property
    def current_option(self) -> str | None:
        """Return the selected lighting mode, if known.

        The mode remembered from the last run stands in only while the lamp
        has named no index. Once it has, the answer is the preset that index
        belongs to - or none, when it belongs to no preset of this model. The
        remembered mode is not an answer then: it is what the select showed
        before the last restart, and the lamp has just said something else.
        """
        state = self._coordinator.state
        modes = self._modes
        if KEY_LIGHTING_MODE not in state:
            return self._restored if self._restored in modes else None
        index = state[KEY_LIGHTING_MODE]
        return next((key for key, value in modes.items() if value == index), None)

    async def async_select_option(self, option: str) -> None:
        """Apply the chosen lighting mode."""
        await self._coordinator.async_set_lighting_mode(self._modes[option])
