"""What keeps a coordinator in Home Assistant: one config entry.

The device half asks its keeper for what it needs of whatever runs it
(``coordinator.Keeper``) and knows nothing of Home Assistant. This is the
adapter for Home Assistant: it is built from the instance and the entry,
neither of which can be absent here, and everything that is Home Assistant's
to do for a lamp is done in this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


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
