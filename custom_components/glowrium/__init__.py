"""The Glowrium integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .coordinator import GlowriumCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

type GlowriumConfigEntry = ConfigEntry[GlowriumCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: GlowriumConfigEntry) -> bool:
    """Set up Glowrium from a config entry."""
    coordinator = GlowriumCoordinator(hass, entry.data[CONF_ADDRESS], entry.title)
    # Published before it is started, not after: async_start registers the
    # bluetooth callbacks and the reconnect poll, so a setup cancelled part-way
    # through it would otherwise leave a live coordinator that async_unload_entry
    # cannot reach - one more of them competing for the single BLE connection on
    # every cancelled attempt.
    entry.runtime_data = coordinator
    # Registered before starting, and relied on for BOTH teardown paths: Home
    # Assistant does not call async_unload_entry for an entry that never
    # reached LOADED, so a setup that fails after this point would otherwise
    # leave the coordinator's bluetooth callbacks and reconnect poll running
    # with no owner - one more of them on every retry.
    entry.async_on_unload(coordinator.async_stop)
    await coordinator.async_start(entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GlowriumConfigEntry) -> bool:
    """Unload a config entry."""
    # The coordinator is stopped by the async_on_unload callback registered in
    # async_setup_entry, which covers a failed setup as well as this path.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
