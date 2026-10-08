"""The Glowrium integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_MODEL_ID,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .coordinator import GlowriumCoordinator
from .link import Unclosed

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

# For each lamp, by its address, what could be neither hung up nor closed.
# Kept here, where a reload does not reach, and not on the coordinator: the
# one a reload makes must not dial over a client its predecessor could not
# let go of.
_UNCLOSED: HassKey[dict[str, Unclosed]] = HassKey(f"{DOMAIN}_unclosed")


async def async_setup_entry(hass: HomeAssistant, entry: GlowriumConfigEntry) -> bool:
    """Set up Glowrium from a config entry."""
    address = entry.data[CONF_ADDRESS]
    coordinator = GlowriumCoordinator(
        hass,
        address,
        entry.title,
        # What an earlier session read off the lamp. The entities are built
        # before this one has read anything, and the presets depend on it.
        model_id=entry.data.get(CONF_MODEL_ID),
        # What a coordinator for this lamp before this one could not let go
        # of. By the lamp's address and not by the entry: a lamp removed and
        # set up again is the same lamp, under another entry.
        unclosed=hass.data.setdefault(_UNCLOSED, {}).setdefault(address, Unclosed()),
    )
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
    # Home Assistant stopping is not an unload: it does not run the callback
    # above, so the lamp's link would be left to whatever the process manages
    # on its way out. Listened for explicitly, and the listener is dropped
    # with the entry.
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_shutdown)
    )
    await coordinator.async_start(entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GlowriumConfigEntry) -> bool:
    """Unload a config entry."""
    # The coordinator is stopped by the async_on_unload callback registered in
    # async_setup_entry, which covers a failed setup as well as this path.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
