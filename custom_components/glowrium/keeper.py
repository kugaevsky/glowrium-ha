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

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_MODEL_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import UNDEFINED

from . import identity
from .const import DOMAIN
from .link import _RECONNECT_INTERVAL, Dial, dial_by_bluetooth
from .models import has_profile, resolve_model

# Where the repair for a Bluetooth stack that will not hang up sends the
# reader for what to do.
_TROUBLESHOOTING_URL = "https://github.com/kugaevsky/glowrium-ha#troubleshooting"
# What to send of a lamp nobody here owns, and how: where the notice about
# unconfirmed presets points.
_PROTOCOL_DATA_URL = (
    "https://github.com/kugaevsky/glowrium-ha/blob/main/"
    "CONTRIBUTING.md#sending-protocol-data-for-a-device"
)


def presets_issue_id(entry_id: str) -> str:
    """Name the repair about unconfirmed presets by the config entry.

    Never by the lamp's address: Home Assistant lists the ids of an
    integration's open repairs in its diagnostics download, which is a file
    people post. A function of the module, and not of the keeper: the
    repair goes when the entry is removed, and no keeper is left then.
    """
    return f"presets_unconfirmed_{entry_id}"


def dial_for(hass: HomeAssistant, address: str, name: str) -> Dial:
    """Return the dial to the lamp as Home Assistant's Bluetooth finds it.

    Asked of the scanners at the moment of each dial, and not once: which
    of them hears the lamp, and whether any does, is not the same from one
    dial to the next. A lamp none of them has is "not in range", said by
    the dial before the library is asked.
    """

    def _found() -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(hass, address, connectable=True)

    return dial_by_bluetooth(_found, address, name)


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
        # Set where the watching ends. What is learnt of the lamp after that
        # is kept, and no longer said. An unload cancels the exchange on its
        # way; Home Assistant stopping does not, so the answer to the read of
        # the device info can come in to a keeper that nobody is left to
        # answer for (see _review_presets_notice).
        self._ceased = False

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

        And the watching begins with what the entry remembers of the lamp's
        model, said before anything is heard: the lamp may not be heard for
        hours.
        """
        hass, address = self._hass, self._address
        self._review_presets_notice()

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
        """End every watcher that was begun, once, and stand behind no repair.

        A keeper that has ceased to watch cannot say when the stack lets go,
        so it does not leave the claim standing that it has not. The repair
        filed under this entry from here on is the next keeper's.

        The notice about unconfirmed presets is another matter, and stands
        (see ``_review_presets_notice``).
        """
        self._ceased = True
        while self._watchers:
            self._watchers.pop()()
        self.stack_fault(None)

    def lamp_described(
        self, model_id: str | None, firmware: str | None, serial: str | None
    ) -> None:
        """Hand what the lamp said about itself on to Home Assistant.

        The entities described the device when they were built, which is
        before anything was read, and Home Assistant takes that description
        once. So what is learned afterwards is put into the device registry
        here, and the model is kept with the config entry so that the next
        start knows it before the first entity exists.

        Both are done for a keeper that has ceased to watch as well: what
        was read is true of the lamp whoever is left to hear it, and the
        next start begins from it. Only the notice is withheld then.
        """
        entry = self._entry
        registry = dr.async_get(self._hass)
        # The device is found through the config entry it belongs to, not by
        # its address: since Home Assistant 2026.10 a connection no longer
        # names one device across config entries, and looking one up that way
        # is deprecated. There is one, once the first entity has been added.
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            # Only what the lamp actually said. The registry takes None as a
            # value; UNDEFINED is how a field is left as it is.
            registry.async_update_device(
                device.id,
                model=resolve_model(model_id).name if model_id else UNDEFINED,
                model_id=model_id or UNDEFINED,
                sw_version=firmware or UNDEFINED,
                serial_number=serial or UNDEFINED,
            )
        if model_id and entry.data.get(CONF_MODEL_ID) != model_id:
            self._hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_MODEL_ID: model_id}
            )
        # The model is known from here on, in this session and not the next.
        self._review_presets_notice()

    def coordinates(self) -> tuple[float, float]:
        """Say where Home Assistant's home is.

        It holds a latitude and a longitude always, as numbers, and both are
        zero when it was given no position.
        """
        return self._hass.config.latitude, self._hass.config.longitude

    def _review_presets_notice(self) -> None:
        """Say that the presets are unconfirmed for this lamp's model, or unsay it.

        A model without a profile is driven by the reference presets - the
        G7's - and the select shows what was chosen whether or not the lamp
        took it as that preset. Its owner is told, in a repair that asks for
        nothing and points at what would settle it.

        Only while the model is known - as the entry remembers it, which is
        as the lamp last named it: a lamp that has not named itself yet is
        driven by the same presets, and may well be a G7.

        Raised at every start and over itself, and not taken down where the
        watching stops: Home Assistant keeps "ignored" with the repair, and one
        that is deleted and raised afresh comes back as new - on every reload,
        and on every restart. It is taken down in two places only: here, once
        the model has a profile, and where the entry is removed
        (``async_remove_entry``). Not kept across a restart of Home Assistant,
        so an entry that is not loaded then shows nothing.
        """
        model_id = self._entry.data.get(CONF_MODEL_ID)
        if self._ceased or not model_id:
            return
        issue_id = presets_issue_id(self._entry.entry_id)
        if has_profile(model_id):
            # Its presets are its own: by an update that brought the profile,
            # or because the lamp names another model than was remembered.
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        # As text: the description is rendered as Markdown, and the name comes
        # off the air.
        said = {"name": self._name_as_text}
        key = "presets_unconfirmed_unnamed_model"
        # The model id only when it is one from end to end: what the parser
        # made of a device-info string can have the serial number inside it.
        # A placeholder is not translated, so nothing can stand in for it:
        # the text that does without the model id is a text of its own.
        if (model := identity.as_model_id(model_id)) is not None:
            said["model"] = model
            key = "presets_unconfirmed"
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=_PROTOCOL_DATA_URL,
            translation_key=key,
            translation_placeholders=said,
        )

    @property
    def _name_as_text(self) -> str:
        """The lamp's name for a repair, which is rendered as Markdown.

        The entry is titled with the name the lamp advertised: it comes off
        the air. Without the address a lamp picked from the list is titled
        with: a repair leaves the host in the diagnostics.
        """
        return identity.as_text(
            identity.without_address(self._entry.title, self._address)
        )

    @property
    def _stack_issue_id(self) -> str:
        """Name the repair by the config entry, never by the lamp's address.

        Home Assistant lists the ids of an integration's open repairs in its
        diagnostics download, which is a file people post. The entry's id
        tells two lamps apart as well as the address would.
        """
        return f"bluetooth_stack_stuck_{self._entry.entry_id}"

    def stack_fault(self, count: int | None) -> None:
        """Put a stack that will not hang up in front of the user, as a repair.

        The log line is for whoever goes looking. This is for everyone else:
        it names the lamp, says that it is the host's stack and not the lamp,
        and carries the one thing that ends it. Not kept across a restart -
        the next start finds out for itself whether the stack still holds on.
        Taken down (``None``) once the lamp has answered.
        """
        if count is None:
            ir.async_delete_issue(self._hass, DOMAIN, self._stack_issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._stack_issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=_TROUBLESHOOTING_URL,
            translation_key="bluetooth_stack_stuck",
            translation_placeholders={
                "name": self._name_as_text,
                "count": str(count),
            },
        )
