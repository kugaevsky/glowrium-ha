"""What keeps a coordinator in Home Assistant: one config entry.

The device half asks its keeper for what it needs of whatever runs it
(``coordinator.Keeper``) and knows nothing of Home Assistant. This is the
adapter for Home Assistant: it is built from the instance and the entry,
neither of which can be absent here, and everything that is Home Assistant's
to do for a lamp is done in this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .link import _RECONNECT_INTERVAL


class EntryKeeper:
    """Keep a coordinator for one config entry (``coordinator.Keeper``)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Keep for ``entry``, which is there before anything can be asked.

        Home Assistant replays the last advertisement from inside the
        registration of the watcher, and the reconnect that sets off has to
        be put on the entry: the entry is taken here, where the keeper is
        built, so that there is no moment at which work could be asked for
        and the entry not yet known.
        """
        self._hass = hass
        self._entry = entry
        self._address: str = entry.data[CONF_ADDRESS]
        # How to end each thing that watches, in the order they were begun.
        self._watchers: list[Callable[[], None]] = []

    def spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Run ``work`` on the entry, so that an unload ends it.

        A task on hass is only awaited at shutdown: one that outlived its
        coordinator would finish connecting and claim the lamp's single slot
        for an owner that no longer exists.
        """
        self._entry.async_create_background_task(self._hass, work, name)

    def run_lasting(
        self, work: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Run ``work`` on hass, where neither a deadline nor an unload ends it.

        A hang-up has to outlive the entry: cancelled by an unload, it leaves
        the lamp's slot taken and the client's bus open.
        """
        return self._hass.async_create_task(work, name)

    def watch(
        self, advertising: Callable[[bool], None], tick: Callable[[], None]
    ) -> bool:
        """Watch through Home Assistant's Bluetooth and its timer.

        Three watchers. Without the advertisements the lamp is never noticed
        coming back. Without the tracking of a lamp gone unavailable its
        entities never say that it was unplugged: availability follows the
        lamp on the air, not the link. And the advertisements are throttled,
        so the clock is watched as well: a link that dropped is looked at
        within ``_RECONNECT_INTERVAL``, whenever the lamp is next heard.

        Each is kept as it is begun, so a start that fails part-way leaves
        nothing ``unwatch`` cannot end.
        """
        hass, address = self._hass, self._address

        @callback
        def _heard(
            _service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange,
        ) -> None:
            advertising(True)

        @callback
        def _gone(_service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
            advertising(False)

        @callback
        def _on_the_clock(_now: datetime) -> None:
            tick()

        self._watchers.append(
            bluetooth.async_register_callback(
                hass,
                _heard,
                bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self._watchers.append(
            bluetooth.async_track_unavailable(hass, _gone, address, connectable=True)
        )
        self._watchers.append(
            async_track_time_interval(hass, _on_the_clock, _RECONNECT_INTERVAL)
        )
        return bluetooth.async_address_present(hass, address, connectable=True)

    def unwatch(self) -> None:
        """End every watcher that was begun, once."""
        while self._watchers:
            self._watchers.pop()()
