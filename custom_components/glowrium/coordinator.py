"""Active BLE coordinator for a single Glowrium device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, time
from enum import Enum, auto
import logging
from time import monotonic
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError, BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.util import dt as dt_util

from . import cbor, identity, protocol
from .const import (
    ACTIVATE_MISC_VALUE,
    DOMAIN,
    INFO_UUID,
    KEY_ACTIVATE_MISC,
    KEY_ACTIVATED,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
    KEY_INDICATOR,
    KEY_LATITUDE,
    KEY_LIGHTING_MODE,
    KEY_LONGITUDE,
    KEY_POWER,
    KEY_RAMP,
    KEY_SCHEDULE,
    KEY_TIME,
    KEY_TIMER,
    MODE_CIRCADIAN,
    MODE_MANUAL,
    MODE_PARAM_2C,
    MODE_PARAM_32,
    MODE_SCHEDULE,
    NOTIFY_UUID,
    RAMP_DEFAULT,
    STATE_KEYS,
    WRITE_UUID,
)
from .link import (
    _LINK_ERRORS,
    _RECONNECT_INTERVAL,
    Dial,
    Link,
    Unclosed,
    _gatt_call,
    _NoNewLinkError,
    _reason,
    dial_by_bluetooth,
)
from .models import GlowriumModel, resolve_model

_LOGGER = logging.getLogger(__name__)
_WRITE_ATTEMPTS = 2  # the initial write plus one reconnect-and-retry
# Ceiling on getting one user-facing command out, so a button reports a clear
# failure instead of appearing to hang while the retries stack up. It has to
# outlast a background connect (see _CONNECT_TIMEOUT). A failed command may
# then spend up to _CONFIRM_TIMEOUT more deciding whether it failed after all,
# so the worst a user waits is the sum of the two.
_COMMAND_TIMEOUT = 25.0
# Ceiling on a background connect: the wait for _lock, the dial, the
# subscription and the priming. Without it a connect to an unreachable device
# holds the lock indefinitely, and everything else that needs the lock waits
# behind it with no deadline of its own.
#
# It is deliberately SHORTER than _COMMAND_TIMEOUT, and the relationship is the
# point rather than the number: a background connect holds the lock while a
# command waits for it inside its own budget, so a holder allowed longer than
# the waiter means pressing a switch during a background connect reports
# failure on a reachable lamp, having attempted nothing. It is also shorter
# than _RECONNECT_INTERVAL, so the connect spawned by one poll tick is over
# before the next.
#
# And it is no shorter than what the library gives one try of its own
# (BLEAK_TIMEOUT, 20 s): a ceiling close to what a connect takes on a weak
# link turns a slow connect into a failed one, tick after tick, for as long as
# the radio stays marginal. Being no shorter than one try does not hand the
# library the whole dial - the ceiling also covers the wait for the lock and
# every try after the first, so a slow try can still be cut from outside. What
# was measured under 10 s and under 20 s, and why it is not raised further:
# ARCHITECTURE.md, "Reconnect".
# test_no_path_holds_the_lock_longer_than_a_command_will_wait pins all three.
_CONNECT_TIMEOUT = 20.0
# Ceiling on asking a link that is already held for its state - priming one a
# command made, probing one that has gone silent - including the wait for
# _lock. There is no dial in it, so it need not be as long as a connect, and a
# probe that is slow to give its verdict keeps a dead link held meanwhile.
_ASK_TIMEOUT = 10.0
# How long a failed command waits for the device to report the state it asked
# for before the failure is believed. A write-with-response on a marginal link
# can reach the lamp and be acted on while the acknowledgement is lost, which
# bleak reports as failure. Observed once on a G7 at RSSI -88: the confirming
# notification arrived 22-32 ms BEFORE the error was raised, so this is grace
# for a slower link rather than a wait anyone should routinely pay.
_CONFIRM_TIMEOUT = 2.0
# Where the repair for a Bluetooth stack that will not hang up sends the
# reader for what to do.
_TROUBLESHOOTING_URL = "https://github.com/kugaevsky/glowrium-ha#troubleshooting"
# Said wherever the log asks for a frame to be posted (see _for_the_log).
_BLANKED = (
    "What reads as the coordinates stored in the lamp, or as the sunrise and "
    "sunset times it works out from them, is shown as xx; look the frame over "
    "all the same before posting it"
)
# The id under which the lamp keeps those times (nothing here reads them), as
# it stands in a frame: an id above 23 takes a second byte.
_CURVE_KEY = bytes((0x18, 0x34))
# How many bytes follow the head of a CBOR float: double, single, half.
_FLOAT_BYTES = {b"\xfb": 8, b"\xfa": 4, b"\xf9": 2}
# The head of a CBOR byte string. Up to 23 bytes the length is in the head
# itself; 0x58 and 0x59 are followed by one and by two bytes of length.
_BYTES_SHORT = range(0x40, 0x58)
_BYTES_LONGER = {b"\x58": 1, b"\x59": 2}
# How long BlueZ gets to report a link dropped once it has called it "not
# connected". Normally two to three seconds (see _REFUSAL_MARKERS). When the
# report never comes, the client is held with is_connected True and nothing
# dials again: on the real host that lasted five hours, until a command.
_LOST_GRACE = 10.0
# How long a held link may stay silent before it is asked whether it is still
# there. The lamp only speaks when something changes, so silence is normal -
# and it is also all a link gives off that died without BlueZ noticing.
_PROBE_INTERVAL = 300.0
# How long the lamp gets to report once it has acknowledged the state request.
# On a G7 the report arrives inside the write call itself. The wait is for a
# model that splits its map across notifications, and with the dial before it
# has to fit inside _CONNECT_TIMEOUT.
_REPORT_TIMEOUT = 3.0
# How far the device clock may drift before it is worth a write. The lamp only
# ever had its clock set during first-time bring-up, so one set up months ago
# runs its schedule and its circadian curve off that date - a reporter's was six
# months out, with nothing to show it because the clock is not an entity. The
# lamp reports its clock with the rest of its state, which is what makes
# correcting it cheap: rather than on every connect, it is written only when it
# is actually wrong. A minute is far below anything the schedule resolves and
# far above normal drift between connects.
_CLOCK_TOLERANCE = 60.0

# The batched state request is muted after this many consecutive refusals. One
# failure means nothing on a weak link - a dropped connection surfaces as the
# same BleakError as an outright refusal - and giving up after one leaves a
# lamp that has to be read without every property a read does not carry.
_STATE_REQUEST_ATTEMPTS = 3
# ...and muted only for this long, not for the session. A model that genuinely
# refuses the request must not have its link torn down on every connect, but a
# lamp on a weak signal fails the same way and then recovers: observed on a G7 at
# RSSI -88, three consecutive failures accumulated about 70 s after start-up
# purely from a bad link, which permanently cost it four properties until Home
# Assistant was restarted. Muting expires so that heals itself.
_STATE_REQUEST_COOLDOWN = 600.0


# Only an error that looks like the device answering "no" counts as a refusal.
# Two cheaper tests were tried on real hardware and both were wrong: a
# successful read does not prove the device is still there (the link drops
# between the read and the write), and bleak's is_connected lags reality - on a
# G7 it still read True at the moment a write failed with "not connected", with
# the disconnect callback arriving two seconds later. So the test is inverted:
# recognise a refusal, treat everything else as the link. Muting a working lamp
# silently costs it every property a read of the state does not carry; asking
# an exotic device once too often costs a reconnect.
_REFUSAL_MARKERS = ("authorization", "authentication", "not permitted")
# The same, where the error is bleak's own and says which ATT error it was.
_REFUSAL_CODES = frozenset(
    {
        BleakGATTProtocolErrorCode.READ_NOT_PERMITTED,
        BleakGATTProtocolErrorCode.WRITE_NOT_PERMITTED,
        BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHENTICATION,
        BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHORIZATION,
    }
)


class _Asked(Enum):
    """What came of asking the lamp to report its state."""

    REPORTED = auto()  # it did
    SILENT = auto()  # it acknowledged the request and reported nothing
    REFUSED = auto()  # it answered the request with a refusal
    LOST = auto()  # the request met a link that is gone


def _looks_like_a_refusal(err: Exception) -> bool:
    """Return True if ``err`` reads as the device declining, not as a lost link.

    Deliberately narrow: an unrecognised error is treated as the link, because
    the cost of guessing wrong that way is one more request on the next connect,
    while guessing wrong the other way silences a working lamp for the session.

    bleak's own protocol error carries the ATT error code, and is judged by
    that alone: its wording is bleak's to change. Any other error - a
    Bluetooth proxy's, for one - has only its text to be told by.
    """
    if isinstance(err, BleakGATTProtocolError):
        return err.code in _REFUSAL_CODES
    text = str(err).lower()
    return any(marker in text for marker in _REFUSAL_MARKERS)


def _private_at(frame: bytes, at: int) -> tuple[int, int]:
    """Tell whether a value that says where the lamp is starts at ``at``.

    ``(head, value)``: how many bytes name it - the id and the head of the
    value - and how many the value itself takes. ``(1, 0)`` when nothing of
    the kind starts here.
    """
    if frame[at] in (KEY_LATITUDE, KEY_LONGITUDE):
        value = _FLOAT_BYTES.get(frame[at + 1 : at + 2])
        return (2, value) if value else (1, 0)
    if frame[at : at + 2] == _CURVE_KEY:
        head = frame[at + 2 : at + 3]
        if head and head[0] in _BYTES_SHORT:
            return 3, head[0] - _BYTES_SHORT.start
        more = _BYTES_LONGER.get(head, 0)
        length = frame[at + 3 : at + 3 + more]
        if more and len(length) == more:
            return 3 + more, int.from_bytes(length, "big")
    return 1, 0


def _for_the_log(frame: bytes) -> str:
    """Return ``frame`` as hex, with what says where the lamp is put down as xx.

    A frame goes into the log so that it can be posted, and a frame can hold
    the coordinates the lamp was given and the sunrise and sunset times it
    works out from them, which give the place away as well. Asking whoever
    posts it to blank hex by hand is asking for it to be forgotten.

    The frame being logged is one that could not be read to its end, so the
    values are not found by decoding it: they are looked for by their bytes -
    an id and the head of its value. A match that was something else costs a
    few bytes of the dump. Everything else stays as it is and where it is,
    which is what makes the dump worth having.

    Every offset is looked at, whatever was found before it. Skipping past a
    value once it is found would be the natural way to walk a frame, and a
    false match would then carry the search over the id of a real one - whose
    value would be printed whole. Looked at one by one, a false match can
    only blank more.
    """
    hidden = bytearray(len(frame))
    for at in range(len(frame)):
        head, value = _private_at(frame, at)
        start = at + head
        hidden[start : start + value] = b"\x01" * len(hidden[start : start + value])
    return "".join(
        "xx" if blank else f"{byte:02x}"
        for byte, blank in zip(frame, hidden, strict=True)
    )


def _named(listener: Callable[[], None]) -> str:
    """Return what to call ``listener`` in the log: its entity, if it has one."""
    entity_id = getattr(getattr(listener, "__self__", None), "entity_id", None)
    if isinstance(entity_id, str):
        return entity_id
    return getattr(listener, "__qualname__", "a listener")


def _parse_device_info(raw: bytes) -> dict[str, str]:
    """Parse the facebd80 device-info string: 'key:value;key:value;...'."""
    info: dict[str, str] = {}
    for part in raw.decode("utf-8", "replace").split(";"):
        key, sep, value = part.partition(":")
        if sep and key.strip():
            info[key.strip()] = value.strip()
    return info


class GlowriumCoordinator:
    """Maintain a BLE connection and mirror the device's CBOR property state.

    Commands are CBOR maps written to ``WRITE_UUID`` (facebd01); the device
    reports its state as CBOR maps notified on ``NOTIFY_UUID`` (facebd02).
    """

    def __init__(  # noqa: PLR0913 - what a coordinator is built from, by name
        self,
        hass: HomeAssistant | None,
        address: str,
        name: str,
        model_id: str | None = None,
        *,
        dial: Dial | None = None,
        unclosed: Unclosed | None = None,
    ) -> None:
        """Initialize the coordinator for the device at ``address``.

        ``hass`` is None when there is no Home Assistant behind it: that is
        how tools/bench.py drives the real coordinator against a real lamp.
        It then connects, primes and commands as it does anywhere, keeps its
        own background tasks, and cannot be started (see ``async_start``).

        ``model_id`` is the model an earlier session read off the lamp, if
        one did. The entities are built before this session has read
        anything, and which presets a lamp has depends on its model.

        ``dial`` is what a link is made through (see ``Dial``). Left out, it
        is the lamp as Home Assistant's Bluetooth finds it.

        ``unclosed`` is what coordinators for this lamp before this one could
        neither hang up nor close (see ``Unclosed``). The integration keeps one
        for each lamp and hands it to every coordinator it makes for it.
        """
        self.hass = hass
        self.address = address
        self.name = name
        self.state: dict[int, Any] = {}
        # The host's clock at the moment the lamp's (0x05) last came into the
        # mirror. A clock is right or wrong only against the moment it was
        # read at, and the mirror can hold one for hours (see _mirror).
        self._clock_heard_at: datetime | None = None
        # What the lamp said about itself in this session; empty until read.
        self.device_info: dict[str, str] = {}
        self._remembered_model_id = model_id
        # The device resets its ramp to a default when circadian is re-enabled,
        # so remember the user's chosen ramp and re-apply it on mode switch.
        self._desired_ramp: bytes | None = None
        self._listeners: set[Callable[[], None]] = set()
        # Listeners whose last telling raised: each is named in the log once,
        # not on every report (see _async_notify_listeners).
        self._listeners_failing: set[Callable[[], None]] = set()
        self._cancel_bluetooth: Callable[[], None] | None = None
        self._cancel_unavailable: Callable[[], None] | None = None
        self._cancel_poll: Callable[[], None] | None = None
        # Set by async_start. Background work is created on the entry rather
        # than on hass so it is cancelled when the entry unloads: a task on
        # hass is only awaited at shutdown, and one that outlives its
        # coordinator finishes connecting and claims the lamp's single slot
        # for an owner that no longer exists. The one exception is a hang-up,
        # which has to outlive the entry - see _hang_up.
        self._entry: ConfigEntry | None = None
        # Tasks in flight when there is no hass to keep them: hang-ups, and
        # whatever the poll spawned (see _run_without_hass).
        self._kept_tasks: set[asyncio.Task[None]] = set()
        # The link to the lamp: the client, the lock around it, and how it is
        # let go of (link.py). It is handed its dial; left out, that is the
        # lamp as Home Assistant's Bluetooth finds it.
        self._link = Link(
            address,
            dial or dial_by_bluetooth(self._ble_device, address, name),
            notify_uuid=NOTIFY_UUID,
            heard=self._heard,
            reach_changed=self._async_reach_changed,
            stack_fault=self._async_on_stack_fault,
            run_lasting=self._run_lasting,
            unclosed=unclosed,
        )
        # The keys the lamp has reported since it was last asked for its state.
        self._carried: set[int] = set()
        self._reconnecting = False
        self._activation_checked = False
        # The batched state request is muted until this time after a run of
        # failures, rather than for the session - see _request_state.
        self._state_request_muted_until = 0.0
        self._state_request_failures = 0
        # Set once a cooldown has already been served and the model refused
        # again: that is a refusal rather than a run of bad luck.
        self._state_request_given_up = False
        # The client whose state has been primed. A command connects without
        # priming (see _connect_locked), so this is how the poll notices there
        # is a connection whose properties were never fetched.
        self._primed_client: BleakClientWithServiceCache | None = None
        # Set once a frame has been rejected for trailing bytes, so the warning
        # is raised once per session instead of on every notification.
        self._trailing_warned = False
        # The same for a frame that was read only in part.
        self._unreadable_warned = False
        # Set whenever the device reports state, so a command awaiting
        # confirmation wakes on the report instead of polling for it.
        self._state_reported = asyncio.Event()
        # Monotonic counters, not values: confirmation needs to know that a
        # report is NEWER than the write it is vouching for, and that a write
        # actually reached the characteristic. Comparing the mirror alone
        # cannot tell a fresh report from an hours-old one.
        self._reports = 0
        self._writes_sent = 0
        # What _reports stood at when the lamp last reported each id. A report
        # vouches for what it carried, not for everything in the mirror.
        self._reported_at: dict[int, int] = {}

    @property
    def _state_request_muted(self) -> bool:
        """True while the batched state request is paused, or given up on."""
        return (
            self._state_request_given_up
            or monotonic() < self._state_request_muted_until
        )

    @property
    def activated(self) -> bool | None:
        """Return the device's activation flag (False = needs pairing/bring-up)."""
        return self.state.get(KEY_ACTIVATED)

    @property
    def model(self) -> GlowriumModel:
        """Return the per-model profile for the lamp's model id."""
        return resolve_model(self.model_id)

    @property
    def model_id(self) -> str | None:
        """Device model code (e.g. Glowrium-C051), read now or remembered.

        From the device-info string once this session has read it, and until
        then from what an earlier session read.
        """
        return self.device_info.get("pkey") or self._remembered_model_id

    @property
    def sw_version(self) -> str | None:
        """Firmware version from the device-info string."""
        return self.device_info.get("version")

    @property
    def serial_number(self) -> str | None:
        """Device id (serial) from the device-info string."""
        return self.device_info.get("devid")

    def _model_and_firmware(self) -> str:
        """Return the model id and the firmware as a warning may say them.

        A warning that asks to be reported gets posted. So each is said only
        when it is what it claims to be (``identity.described``): a lamp that
        glues the fields of its device-info string together would otherwise
        put its serial number here.
        """
        return identity.described(self.model_id, self.sw_version)

    @property
    def _plain_name(self) -> str:
        """The lamp's name without its address after it.

        A lamp picked from the list of discovered devices is titled with its
        name and its address in brackets. Where the address is said anyway, or
        where brackets and colons do not survive (``identity.as_text``), the name
        alone reads better.
        """
        return self.name.removesuffix(f" ({self.address})")

    # --- The link's own, under the names they had here -----------------------
    #
    # Stage 1 of #21: the link holds these now (link.py). The exchanges below
    # still say self._client and self._lock, and so do the tests that have not
    # moved yet. Each of these goes when nothing reaches through it any more.

    @property
    def _client(self) -> BleakClientWithServiceCache | None:
        return self._link.client

    @_client.setter
    def _client(self, client: BleakClientWithServiceCache | None) -> None:
        self._link.client = client

    @property
    def _lock(self) -> asyncio.Lock:
        return self._link.lock

    @property
    def _backends(self) -> dict[BleakClientWithServiceCache, Any]:
        return self._link.backends

    @property
    def _unreleased(self) -> set[BleakClientWithServiceCache]:
        return self._link.unclosed

    @property
    def _stuck_hang_ups(self) -> int:
        return self._link.stuck_hang_ups

    @_stuck_hang_ups.setter
    def _stuck_hang_ups(self, count: int) -> None:
        self._link.stuck_hang_ups = count

    @property
    def _dial_not_before(self) -> float:
        return self._link.dial_not_before

    @_dial_not_before.setter
    def _dial_not_before(self, moment: float) -> None:
        self._link.dial_not_before = moment

    @property
    def _lost(self) -> tuple[BleakClientWithServiceCache, float] | None:
        return self._link.lost

    @_lost.setter
    def _lost(self, lost: tuple[BleakClientWithServiceCache, float] | None) -> None:
        self._link.lost = lost

    @property
    def _last_answer(self) -> float:
        return self._link.last_answer

    @_last_answer.setter
    def _last_answer(self, moment: float) -> None:
        self._link.last_answer = moment

    @property
    def _present(self) -> bool:
        return self._link.present

    @_present.setter
    def _present(self, present: bool) -> None:
        self._link.present = present

    @property
    def _is_connected(self) -> bool:
        """Return True while a live GATT connection is held."""
        return self._link.connected

    def _hang_up(self, client: BleakClientWithServiceCache) -> asyncio.Task[None]:
        return self._link.hang_up(client)

    def _note_answer(self) -> None:
        self._link.note_answer()

    def _note_stuck_hang_up(self) -> None:
        self._link.note_stuck_hang_up()

    @callback
    def _async_on_disconnect(self, client: BleakClientWithServiceCache) -> None:
        self._link.on_lost(client)

    @callback
    def _async_log_reach(self) -> None:
        self._link.log_reach(self._plain_name)

    # --- What the link is handed ----------------------------------------------

    @callback
    def _heard(self, characteristic: Any, data: bytearray) -> None:
        """Take a frame the lamp sent - through the name a test may replace."""
        self._on_notify(characteristic, data)

    @callback
    def _async_reach_changed(self) -> None:
        """Tell the entities: the link has let go of a client that was lost."""
        self._async_notify_listeners()

    @callback
    def _async_on_stack_fault(self, count: int | None) -> None:
        """Raise the repair for a stack that will not hang up, or take it down."""
        if count is None:
            self._async_clear_stack_issue()
        else:
            self._async_raise_stack_issue(count)

    def _run_lasting(
        self, coro: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Run a hang-up where neither a deadline nor an unload can end it.

        On hass, not on the entry like the rest of the background work (see
        ``_spawn``): a hang-up that outlives its coordinator gives the lamp's
        slot back, and one cut short leaves the client's bus open.
        """
        if self.hass is not None:
            return self.hass.async_create_task(coro, name)
        return self._run_without_hass(coro, name)

    @property
    def available(self) -> bool:
        """Entity availability: the device is advertising, or there is a link.

        A GATT link to this lamp does not last, and tying availability to it
        makes every entity flap to ``unavailable`` on each reconnect. The
        device advertises continuously, so "present, or currently connected"
        is treated as available and the link is rebuilt silently underneath.
        """
        return self._link.in_reach

    @property
    def operating_mode(self) -> str | None:
        """Return the active mode, or None if the state has not been read yet.

        None ("unknown") is distinct from Manual, which is only reported once
        both mode flags have actually been read as off - so a device we cannot
        reach yet does not look like it is in Manual.
        """
        circadian = self.state.get(KEY_CIRCADIAN)
        schedule = self.state.get(KEY_SCHEDULE)
        if circadian:
            return MODE_CIRCADIAN
        if schedule:
            return MODE_SCHEDULE
        if circadian is None or schedule is None:
            return None
        return MODE_MANUAL

    def mode_allows(self, mode: str) -> bool:
        """Return True if the device is in ``mode``, or its mode is unknown.

        Keeps mode-specific entities available while the state is unknown,
        instead of collapsing them to unavailable during a disconnect.
        """
        current = self.operating_mode
        return current is None or current == mode

    # Decoded read accessors - the byte layouts they wrap live in protocol.py,
    # so entities read meaningful values instead of the raw state dict.

    @property
    def brightness_percent(self) -> float | None:
        """Brightness as a level from 0 to 100, or None if not read or no level."""
        return protocol.brightness_percent(self.state)

    @property
    def ramp_minutes(self) -> int | None:
        """Circadian ramp duration in minutes (0x2f), or None if not yet read."""
        return protocol.ramp_minutes(self.state)

    @property
    def schedule_start(self) -> time | None:
        """Schedule on-time from the 0x11 slot, or None if not yet read."""
        return protocol.schedule_start(self.state)

    @property
    def schedule_end(self) -> time | None:
        """Schedule off-time from the 0x11 slot, or None if not yet read."""
        return protocol.schedule_end(self.state)

    @property
    def schedule_brightness(self) -> int | None:
        """Schedule target brightness (%) from the 0x11 slot, or None."""
        return protocol.schedule_brightness(self.state)

    @property
    def schedule_gradual_minutes(self) -> int | None:
        """Schedule gradual-fade duration in minutes, or None if not yet read."""
        return protocol.schedule_gradual_minutes(self.state)

    @property
    def dst_enabled(self) -> bool | None:
        """Whether daylight saving is on, or None if not yet read."""
        return protocol.dst_enabled(self.state)

    def diagnostics(self) -> dict[str, Any]:
        """Describe the lamp and the link, for a diagnostics download.

        What the lamp said about itself, everything it has reported, and where
        the connection stands - as it is, private or not. What of this may
        leave the host is for the caller to choose, which is the one that
        knows it is writing a file to be shared (see diagnostics.py).
        """
        client = self._client
        backend = self._backends.get(client) if client is not None else None
        return {
            "device": {
                "model": self.model.name,
                "model_id": self.model_id,
                "firmware": self.sw_version,
                "info": dict(self.device_info),
            },
            "link": {
                "available": self.available,
                "advertising": self._present,
                "connected": self._is_connected,
                "primed": client is not None and client is self._primed_client,
                "client": type(backend).__name__ if backend is not None else None,
                "reports": self._reports,
                "writes_sent": self._writes_sent,
                "seconds_since_last_answer": round(monotonic() - self._last_answer),
                "state_request_refusals": self._state_request_failures,
                "state_request_paused": self._state_request_muted,
                "unanswered_hang_ups": self._stuck_hang_ups,
                "dials_held_back": monotonic() < self._dial_not_before,
                "clients_that_would_not_close": len(self._unreleased),
            },
            "state": dict(self.state),
            "clock_heard_at": self._clock_heard_at,
        }

    @callback
    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register an update listener; return a callable that removes it."""
        self._listeners.add(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.discard(update_callback)
            self._listeners_failing.discard(update_callback)

        return _remove

    @callback
    def _async_notify_listeners(self) -> None:
        """Tell every listener that something changed, each on its own.

        A listener is an entity writing its state. One that raises must not
        keep the news from the rest, and its exception is not the business of
        whoever brought the news - a notification, or a command that in fact
        went through. It is named with its trace once: the lamp reports all
        day, and what failed an entity once fails it on every report, until
        the value changes. Having managed a round, it is news again.
        """
        self._async_log_reach()
        for update_callback in list(self._listeners):
            try:
                update_callback()
            except Exception:  # whatever it raised, the rest are still told
                again = update_callback in self._listeners_failing
                # The round is told from a copy: one that left during it is
                # still called, and is not to be remembered.
                if update_callback in self._listeners:
                    self._listeners_failing.add(update_callback)
                if again:
                    # With its trace: it may not be the failure that was said.
                    _LOGGER.debug(
                        "%s: %s failed again to take in a change",
                        self.address,
                        _named(update_callback),
                        exc_info=True,
                    )
                    continue
                _LOGGER.exception(
                    "%s: %s failed to take in a change and shows what it showed "
                    "before; the others were told. Said once, until it has "
                    "managed again. Please report this with the trace below. "
                    "Look the trace over before posting it: its wording is not "
                    "this integration's, and it may quote a value",
                    self.address,
                    _named(update_callback),
                )
            else:
                self._listeners_failing.discard(update_callback)

    async def async_start(self, entry: ConfigEntry) -> None:
        """Watch for the device and keep it connected.

        Returns as soon as the watchers are in place. The first connect runs as
        a background task rather than being awaited: setup awaits this method,
        and a lamp that is out of range would otherwise hold the entry open for
        the whole connect budget - long enough for a reload to land inside it,
        cancel the setup and leave the entry in ``setup_error``. Waiting buys
        nothing, because entity availability follows advertisement presence
        rather than the GATT link. Tying the task to ``entry`` means it is
        cancelled on unload, so a half-finished connect cannot outlive us.
        """
        hass = self.hass
        if hass is None:
            # Watching is done through Home Assistant's Bluetooth: its
            # advertisement callbacks, its presence tracking, its timer.
            raise RuntimeError(
                f"{self.address}: async_start needs Home Assistant to watch the "
                "lamp through; without one, connect directly (tools/bench.py)"
            )
        # Before anything is registered. Home Assistant replays the last
        # advertisement from inside async_register_callback when it already
        # knows the device - every reload, for a lamp that advertises all the
        # time - and the reconnect that callback starts needs the entry to be
        # put on. Without it the task lands on hass and outlives the unload.
        self._entry = entry
        self._cancel_bluetooth = bluetooth.async_register_callback(
            hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Track presence so entity availability follows the device, not the link.
        self._cancel_unavailable = bluetooth.async_track_unavailable(
            hass, self._async_on_unavailable, self.address, connectable=True
        )
        self._present = bluetooth.async_address_present(
            hass, self.address, connectable=True
        )
        self._async_log_reach()  # absent from the start is worth saying too
        self._spawn(self._async_initial_connect(), "initial connect")
        # Advertisement callbacks are throttled, so also poll: reconnect within
        # _RECONNECT_INTERVAL after any drop, regardless of advertisement timing.
        self._cancel_poll = async_track_time_interval(
            hass, self._async_poll_reconnect, _RECONNECT_INTERVAL
        )

    @callback
    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        """Run ``coro`` in the background, for no longer than the entry lives.

        Without Home Assistant there is no entry to end it and nothing else to
        hold the task: the coordinator keeps it itself (``_run_without_hass``).
        """
        name = f"glowrium {what} {self.address}"
        if self.hass is None:
            self._run_without_hass(coro, name)
        elif self._entry is None:  # not started on an entry: nothing to end it with
            self.hass.async_create_task(coro, name)
        else:
            self._entry.async_create_background_task(self.hass, coro, name)

    @callback
    def _run_without_hass(
        self, coro: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Run ``coro`` on the loop and keep it until it is done.

        Standalone, with no Home Assistant behind it (tools/bench.py). Nothing
        keeps the task for us there, and the loop itself holds it only weakly.
        """
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._kept_tasks.add(task)
        task.add_done_callback(self._kept_tasks.discard)
        return task

    async def _async_initial_connect(self) -> None:
        """Connect once at start-up, off the setup path."""
        try:
            await self._async_ensure_connected()
        except _LINK_ERRORS as err:
            self._log_connect_ended("Initial connect", err)

    @callback
    def _async_stop_watching(self) -> None:
        """Stop reacting to the lamp, and take no new link from here on."""
        # A command may be in flight and outlast this; from here on it is
        # refused a new link, so that what the caller lets go of next is the
        # last one this coordinator will ever hold.
        self._link.stopped = True
        if self._cancel_bluetooth is not None:
            self._cancel_bluetooth()
            self._cancel_bluetooth = None
        if self._cancel_unavailable is not None:
            self._cancel_unavailable()
            self._cancel_unavailable = None
        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None
        # A coordinator that has stopped watching cannot say when the stack
        # lets go, so it does not leave the claim standing that it has not -
        # and has no episode left to call over. The repair filed under this
        # entry from here on is its successor's.
        self._async_clear_stack_issue()
        self._link.fault_announced = False

    @callback
    def async_shutdown(self, _event: Event | None = None) -> None:
        """Hang up as Home Assistant stops: at once, and without waiting.

        Home Assistant does not unload its config entries when it stops, so
        async_stop never runs then. The watching ends here; the hanging up,
        and why it must not wait, is the link's (``Link.shut_down``).
        """
        self._async_stop_watching()
        self._link.shut_down()

    async def async_stop(self) -> None:
        """Cancel watching and disconnect."""
        # Before anything is awaited.
        self._async_stop_watching()
        await self._link.let_go()

    @property
    def _stack_issue_id(self) -> str:
        """Name the repair by the config entry, never by the lamp's address.

        Home Assistant lists the ids of an integration's open repairs in its
        diagnostics download, which is a file people post. The entry's id
        tells two lamps apart as well as the address would.
        """
        if self._entry is None:
            return "bluetooth_stack_stuck"
        return f"bluetooth_stack_stuck_{self._entry.entry_id}"

    @callback
    def _async_raise_stack_issue(self, count: int) -> None:
        """Put the wedged stack in front of the user, as a repair.

        The log line is for whoever goes looking. This is for everyone else:
        it names the lamp, says that it is the host's stack and not the lamp,
        and carries the one thing that ends it. Not kept across a restart -
        the next start finds out for itself whether the stack still holds on.
        """
        if self.hass is None:  # the bench has no dashboard to raise it on
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._stack_issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=_TROUBLESHOOTING_URL,
            translation_key="bluetooth_stack_stuck",
            translation_placeholders={
                # As text: the description is rendered as Markdown.
                "name": identity.as_text(self._plain_name),
                "count": str(count),
            },
        )

    @callback
    def _async_clear_stack_issue(self) -> None:
        """Take the repair down: the lamp answered, or nobody is watching."""
        if self.hass is None:
            return
        ir.async_delete_issue(self.hass, DOMAIN, self._stack_issue_id)

    @callback
    def _async_on_advertisement(
        self,
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        was_present = self._present
        self._present = True
        # Reconnect when the device reappears, but only one attempt at a time
        # (advertisements arrive ~every second; don't spawn a connect storm).
        if (
            not self._is_connected
            and not self._reconnecting
            and monotonic() >= self._dial_not_before
        ):
            self._reconnecting = True
            self._spawn(self._async_reconnect(), "reconnect")
        if not was_present:
            self._async_notify_listeners()

    @callback
    def _async_on_unavailable(
        self, _service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        # The device stopped advertising (powered off / out of range).
        self._present = False
        self._async_notify_listeners()

    @callback
    def _async_poll_reconnect(self, _now: Any) -> None:
        for client in tuple(self._unreleased):
            # Still holding a bus nothing could close. The stack may have
            # come round since, and until this goes through nothing dials.
            self._hang_up(client)
        if not self._is_connected:
            # Held back only while BlueZ will not hang up (_note_stuck_hang_up).
            if not self._reconnecting and monotonic() >= self._dial_not_before:
                self._reconnecting = True
                self._spawn(self._async_reconnect(), "reconnect")
            return
        lost, self._lost = self._lost, None
        if lost is not None and lost[0] is self._client:
            if monotonic() - lost[1] < _LOST_GRACE:
                self._lost = lost
            else:
                # BlueZ said "not connected" and then never reported the link
                # dropped. Waiting any longer is waiting for ever.
                _LOGGER.debug(
                    "%s: the state request failed on this link and it was "
                    "never reported dropped; dropping it",
                    self.address,
                )
                self._hang_up(lost[0])
                self._async_notify_listeners()
                return
        # Connected, but by a command, which skips priming to stay fast. Fetch
        # the properties now, off the command's critical path.
        if self._client is not self._primed_client:
            self._spawn(self._async_prime(), "prime")
        elif monotonic() - self._last_answer >= _PROBE_INTERVAL:
            self._spawn(self._async_probe(), "probe")

    async def _async_prime(self) -> None:
        """Fetch device properties for a link that was established by a command."""
        try:
            async with asyncio.timeout(_ASK_TIMEOUT), self._lock:
                client = self._client
                if client is None or client is self._primed_client:
                    return
                if not await self._request_state(client):
                    # The link answers nothing, however connected it claims to
                    # be. Drop it so the poll rebuilds one: _is_connected would
                    # otherwise stay True and nothing would reconnect or
                    # re-prime, leaving the entities frozen for the whole life
                    # of a link that never worked.
                    _LOGGER.debug("%s: link answers nothing, dropping", self.address)
                    self._hang_up(client)
                    # Said here: the return below skips the else at the end.
                    self._async_notify_listeners()
                    return
                await self._async_activate_if_needed()
                await self._async_sync_clock_if_needed()
                self._primed_client = client
                _LOGGER.debug("%s: primed a link a command made", self.address)
                await self._async_read_device_info(client)
        except _LINK_ERRORS as err:
            _LOGGER.debug("Priming state of %s failed: %s", self.address, _reason(err))
        else:
            self._async_notify_listeners()

    async def _async_probe(self) -> None:
        """Ask a link that has been silent whether it is still there.

        By priming it again: the lamp is asked for its state, which costs one
        write, proves the link if it answers, and refreshes the mirror for free.
        A link that does not answer - or keeps the question waiting until the
        deadline - is dropped, and the poll rebuilds it.
        """
        client: BleakClientWithServiceCache | None = None
        alive = False
        try:
            async with asyncio.timeout(_ASK_TIMEOUT), self._lock:
                held = self._client
                if held is None or (monotonic() - self._last_answer < _PROBE_INTERVAL):
                    return
                client = held
                alive = await self._request_state(client)
        except _LINK_ERRORS as err:
            _LOGGER.debug(
                "Probing the link to %s failed: %s", self.address, _reason(err)
            )
        if client is None:
            return  # never got as far as asking; nothing was learnt
        if alive:
            _LOGGER.debug("%s: the held link answers", self.address)
            return
        _LOGGER.debug("%s: the held link no longer answers, dropping", self.address)
        if client is self._client:
            self._hang_up(client)
            self._async_notify_listeners()

    async def _async_reconnect(self) -> None:
        try:
            await self._async_ensure_connected()
        except _LINK_ERRORS as err:
            self._log_connect_ended("Reconnect", err)
        finally:
            self._reconnecting = False

    def _log_connect_ended(self, what: str, err: Exception) -> None:
        """Say how a background connect ended, when it did not end as meant.

        Time can run out with the link already taken: during its first
        exchange, during the device-info read after it, or because a command
        got the lock first and connected. That link is held, and what becomes
        of it is the poll's business - it primes one that is not primed, drops
        one that answers nothing, probes one that is. Calling that a failed
        connect sent whoever read the log looking for a lamp out of range: on
        the G7's host (2026-10-06) the line was written of a link that was held
        for ten seconds more, until the radio lost it.
        """
        if isinstance(err, TimeoutError) and self._is_connected:
            _LOGGER.debug(
                "%s to %s ran out of time, but the link is held (%s); "
                "it is left to the poll",
                what,
                self.address,
                "primed" if self._client is self._primed_client else "not primed yet",
            )
            return
        _LOGGER.debug("%s to %s failed: %s", what, self.address, _reason(err))

    def _ble_device(self) -> BLEDevice | None:
        if self.hass is None:  # the bench scans for itself and hands in a dial
            return None
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_ensure_connected(self) -> None:
        """Connect if not already connected, under a bounded wait for the lock.

        Every background connect - setup, the reconnect poll and the
        advertisement callback - funnels through here, so the ceiling applies to
        all of them. It has to cover the wait for ``_lock`` too: the starvation
        that hung setup was one holder grinding through connect attempts to an
        unreachable lamp while another waited on the lock with no deadline.
        """
        if self._is_connected:
            return
        async with asyncio.timeout(_CONNECT_TIMEOUT), self._lock:
            await self._connect_locked()

    async def _connect_locked(self, *, prime: bool = True) -> None:
        """Establish the GATT link, and unless told otherwise prime the state.

        The caller must hold ``_lock``; ``_async_ensure_connected`` and the
        write path both funnel through here so a command can never race a
        background connect.

        A command passes ``prime=False``. It needs the link and its own write,
        nothing else - and priming is expensive: the state request and the
        wait for its answer, up to 3 s waiting for the activation flag and,
        the first time, the device-info read, all before the write is even
        attempted and all inside the command budget. On a lamp where the
        connect alone is marginal, that is what turns a working command into a
        reported failure. The poll picks the priming up afterwards (see
        ``_async_poll_reconnect``).
        """
        if self._is_connected:
            return
        client = await self._link.open()
        if not prime:
            return
        if not await self._request_state(client):
            # Established, but it answers nothing - see _request_state. Drop it
            # here rather than holding a link that serves nothing until the
            # poll comes round: the poll rebuilds it either way, and in the
            # meantime _is_connected would claim a connection a command would
            # write into before failing.
            _LOGGER.debug("%s: link answers nothing, dropping", self.address)
            self._hang_up(client)
            self._async_notify_listeners()
            return
        await self._async_activate_if_needed()
        await self._async_sync_clock_if_needed()
        self._primed_client = client
        _LOGGER.debug("%s: connected and primed", self.address)
        await self._async_read_device_info(client)
        self._async_notify_listeners()

    def _require_read(self, value: Any, translation_key: str) -> Any:
        """Return ``value``, or raise if the device has not reported it yet.

        The schedule slot and the lighting-mode frame are read-modify-write: one
        write carries several fields at once. Substituting a default to change a
        single field silently overwrites the rest with values the user never
        chose, so refuse instead and say why.

        ``translation_key`` names the field in the user's own language. It is a
        key per field rather than one message with the field name substituted in,
        because a field name is a translatable noun, not a value.
        """
        if value is None or value == b"":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={"name": self.name},
            )
        return value

    async def _async_read_device_info(
        self, client: BleakClientWithServiceCache
    ) -> None:
        """Read the device-info string - once, and after everything else.

        It names the model, the firmware and the serial, and it is the one thing
        here that only a GATT read gives. On BlueZ a read of this lamp ends the
        link about two seconds later (see ``_request_state``), so it is left
        until the state has arrived and whatever had to be written has been:
        that link is spent, and the next one has nothing left to read.
        """
        if self.device_info:
            return
        try:
            with _gatt_call(client):
                raw = await client.read_gatt_char(INFO_UUID)
        except _LINK_ERRORS as err:
            _LOGGER.debug(
                "Device-info read from %s failed: %s", self.address, _reason(err)
            )
            return
        self.device_info = _parse_device_info(bytes(raw))
        _LOGGER.debug(
            "%s: device info read; on BlueZ this link does not outlive a read",
            self.address,
        )
        self._async_publish_device_info()

    @callback
    def _async_publish_device_info(self) -> None:
        """Hand what the lamp said about itself on to Home Assistant.

        The entities described the device when they were built, which is
        before anything was read, and Home Assistant takes that description
        once. So what is learned afterwards is put into the device registry
        here, and the model is kept with the config entry so that the next
        start knows it before the first entity exists.
        """
        entry = self._entry
        if self.hass is None or entry is None:  # the bench has neither
            return
        registry = dr.async_get(self.hass)
        # The device is found through the config entry it belongs to, not by
        # its address: since Home Assistant 2026.10 a connection no longer
        # names one device across config entries, and looking one up that way
        # is deprecated. There is one, once the first entity has been added.
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            # Only what the lamp actually said. The registry takes None as a
            # value; UNDEFINED is how a field is left as it is.
            registry.async_update_device(
                device.id,
                model=self.model.name if self.model_id else UNDEFINED,
                model_id=self.model_id or UNDEFINED,
                sw_version=self.sw_version or UNDEFINED,
                serial_number=self.serial_number or UNDEFINED,
            )
        model_id = self.device_info.get("pkey")
        if model_id and entry.data.get(CONF_MODEL_ID) != model_id:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_MODEL_ID: model_id}
            )

    async def _request_state(self, client: BleakClientWithServiceCache) -> bool:
        """Prime the state mirror: ask the lamp to report, read only if it will not.

        Writing the ids in ``STATE_KEYS`` to ``NOTIFY_UUID`` makes the lamp
        report them in a notification. ``NOTIFY_UUID`` is readable too, but on
        BlueZ a GATT read of this lamp ends the link two seconds later: the
        lamp answers a read twice - the value, then an error response to the
        same request - and BlueZ closes the channel on the stray one, where
        macOS ignores it. Reading first, on every connect, is what 0.2.0 and
        0.2.1 did, and it cost a link on every poll tick. The measurement and
        the trace: ARCHITECTURE.md, "Priming state on connect".

        So the lamp is asked first, and a link whose lamp reports is never
        read. The report is waited for, briefly: on a G7 it arrives inside the
        write, and a model that splits its map across notifications needs a
        moment more.

        Reading remains the way out for a lamp that will not report - one that
        refuses the request, as a G8 (``Glowrium-C064``) does with ATT
        ``Insufficient authorization``, or acknowledges it and stays silent. A
        lamp that has refused before is read first, as it always was, and the
        request is repeated until ``_STATE_REQUEST_ATTEMPTS`` refusals in a row
        silence it: a dropped link raises the same kind of error as a refusal,
        and one failure proves nothing. The read does not carry everything - a
        complete twenty-pair map that stops at 0x15, so no indicator, lighting
        mode, ramp or DST - which is why the request is not simply given up.

        Returns whether the link answered at all: the lamp reported, or
        acknowledged or refused the request, or could be read. bleak can hand
        back a client that reports itself connected while every call on it
        answers "not connected", and such a link is not a working one.
        """
        if self._state_request_muted or self._state_request_failures:
            read_ok, carried = await self._async_read_state(client)
            if not carried.issuperset(STATE_KEYS) and not self._state_request_muted:
                # Judged on what this read carried rather than on the mirror:
                # the mirror accumulates, so a key seen once would look
                # covered for the rest of the session.
                await self._async_ask_state(client)
            return read_ok
        asked = await self._async_ask_state(client)
        if asked is _Asked.REPORTED:
            return True
        read_ok, _ = await self._async_read_state(client)
        return read_ok or asked in (_Asked.SILENT, _Asked.REFUSED)

    async def _async_read_state(
        self, client: BleakClientWithServiceCache
    ) -> tuple[bool, frozenset[int]]:
        """Read the state map; return whether it answered and what it carried."""
        try:
            with _gatt_call(client):
                raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
        except _LINK_ERRORS as err:
            _LOGGER.debug("%s state read failed: %s", self.address, _reason(err))
            return False, frozenset()
        return True, self._ingest(raw)

    async def _async_ask_state(self, client: BleakClientWithServiceCache) -> _Asked:
        """Write the state request and wait for the lamp to report."""
        self._carried.clear()
        before = self._reports
        try:
            with _gatt_call(client):
                await client.write_gatt_char(
                    NOTIFY_UUID, bytes(STATE_KEYS), response=True
                )
        except _LINK_ERRORS as err:
            if not _looks_like_a_refusal(err):
                _LOGGER.debug(
                    "%s state request failed, but not by refusing: %s",
                    self.address,
                    _reason(err),
                )
                # The link is going, and BlueZ normally says so within seconds.
                # Noted in case it never does (see _LOST_GRACE).
                self._lost = (client, monotonic())
                return _Asked.LOST
            self._state_request_failures += 1
            if self._state_request_failures < _STATE_REQUEST_ATTEMPTS:
                _LOGGER.debug(
                    "%s state request failed (%d/%d): %s",
                    self.address,
                    self._state_request_failures,
                    _STATE_REQUEST_ATTEMPTS,
                    err,
                )
                return _Asked.REFUSED
            self._state_request_failures = 0
            served_a_cooldown = self._state_request_muted_until > 0.0
            self._state_request_given_up = served_a_cooldown
            self._state_request_muted_until = monotonic() + _STATE_REQUEST_COOLDOWN
            _LOGGER.warning(
                "%s (%s) refused the batched state request "
                "%d times in a row, most recently: %s. %s "
                "Commands still work; properties a read of the state does not "
                "carry stay unknown. Please report this model",
                self.address,
                self._model_and_firmware(),
                _STATE_REQUEST_ATTEMPTS,
                err,
                "Not asking again this session."
                if served_a_cooldown
                else f"Pausing it for {int(_STATE_REQUEST_COOLDOWN // 60)} minutes.",
            )
            return _Asked.REFUSED
        self._state_request_failures = 0
        self._note_answer()  # the write was acknowledged
        try:
            async with asyncio.timeout(_REPORT_TIMEOUT):
                while True:
                    # Cleared before checking, as in _async_device_confirms.
                    self._state_reported.clear()
                    if self._reports > before and self._carried.issuperset(STATE_KEYS):
                        return _Asked.REPORTED
                    await self._state_reported.wait()
        except TimeoutError:
            if self._reports > before:
                return _Asked.REPORTED  # in part; a read would add nothing asked for
        _LOGGER.debug(
            "%s acknowledged the state request and reported nothing", self.address
        )
        return _Asked.SILENT

    def _log_trailing_bytes(self, data: bytes, count: int) -> None:
        """Report a frame rejected for trailing bytes: once loudly, then quietly.

        Notifications arrive continuously, so an unconditional warning would
        flood the log; one per session is enough to surface the problem while
        the hex dump below gives whoever reports it everything needed to decode
        the frame by hand.
        """
        if self._trailing_warned:
            _LOGGER.debug(
                "%s: frame %s again carries %d trailing bytes",
                self.address,
                _for_the_log(data),
                count,
            )
            return
        self._trailing_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with %d trailing bytes "
            "and it was dropped: %s. The frame declared less than it carried, "
            "so accepting the remainder could mean acting on a corrupt state. "
            "Please report this frame - it is exactly the hex dump needed. %s",
            self.address,
            self._model_and_firmware(),
            count,
            _for_the_log(data),
            _BLANKED,
        )

    def _log_unreadable_item(self, data: bytes, err: cbor.UnreadableItemError) -> None:
        """Report a frame that was read only in part: once loudly, then quietly.

        As with trailing bytes: a lamp that sends one such frame sends them
        all day, and the first is the one that has to be seen - with the bytes
        it takes to give the item a reading.
        """
        if self._unreadable_warned:
            _LOGGER.debug(
                "%s: frame %s again carries an item that cannot be read (%s); "
                "kept the %d properties ahead of it",
                self.address,
                _for_the_log(data),
                err,
                len(err.ahead),
            )
            return
        self._unreadable_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with an item this "
            "integration cannot read (%s): %s. The %d properties ahead of it "
            "were kept; whatever follows it could not be found. Please report "
            "this frame - it is exactly the hex dump needed. %s",
            self.address,
            self._model_and_firmware(),
            err,
            _for_the_log(data),
            len(err.ahead),
            _BLANKED,
        )

    def _ingest(self, data: bytes) -> frozenset[int]:
        """Merge a CBOR property map from the device into the state mirror.

        Returns the keys this frame carried. The connect path needs that to
        judge whether the read covered everything, which it cannot do from the
        state mirror: the mirror accumulates across a session, so once a key has
        been seen it looks covered for ever.

        Shared by the notify callback and the read of the state, so that both
        handle a split map, the remembered ramp and the listeners identically.
        """
        self._note_answer()  # whatever it says, the lamp said it
        short = said = False
        try:
            decoded, short = cbor.decode_frame(data)
        except cbor.UnreadableItemError as err:
            # Kept, as far as it was read. Dropping the frame would cost more
            # than its properties: a state request answered only by this would
            # count as unanswered, the connect would fall back on reading, and
            # on BlueZ a read ends the link (see _request_state). A frame of
            # which nothing was read is still no report, and takes that way.
            self._log_unreadable_item(data, err)
            decoded, said = err.ahead, True
        except cbor.TrailingBytesError as err:
            # Reported apart from a merely malformed frame, and loudly the first
            # time: rejecting these is what changed in #5, and on a model whose
            # frames were always fully consumed before, this is the regression
            # that change risks. Buried in "Undecodable frame" at debug level it
            # would never be noticed.
            self._log_trailing_bytes(data, err.count)
            return frozenset()
        except ValueError as err:  # the decoder raises nothing else
            _LOGGER.debug("Undecodable frame %s: %s", _for_the_log(data), err)
            return frozenset()
        if not isinstance(decoded, dict) or not decoded:
            if not said:
                # Decoded without a fault, and still of no use: every frame
                # that is dropped is named, or nobody can ask what it was.
                _LOGGER.debug(
                    "%s: frame %s decodes to nothing that can be used: it is "
                    "not a map of properties, or is one with nothing in it",
                    self.address,
                    _for_the_log(data),
                )
            return frozenset()
        if short:
            _LOGGER.debug(
                "%s: property map split across frames; kept %d of them",
                self.address,
                len(decoded),
            )
        self._mirror(decoded)
        self._reports += 1
        self._reported_at.update(dict.fromkeys(decoded, self._reports))
        self._state_reported.set()
        # Seed the remembered ramp from the device the first time we see it, so
        # it survives an HA restart (the device persists its own ramp). Guard on
        # truthiness, not "is not None": an empty ramp would otherwise latch and
        # block re-seeding forever, as protocol.ramp_minutes already assumes.
        if self._desired_ramp is None:
            ramp = self.state.get(KEY_RAMP)
            if ramp and isinstance(ramp, (bytes, bytearray)):
                self._desired_ramp = bytes(ramp)
        self._async_notify_listeners()
        return frozenset(decoded)

    @callback
    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._carried |= self._ingest(bytes(data))

    def _mirror(self, values: dict[int, Any]) -> None:
        """Take ``values`` into the state mirror.

        And note the moment, if the lamp's clock is among them - whether the
        lamp reported it or it is the echo of a clock written to the lamp.
        The mirror is not emptied when a link drops, so the clock in it can
        be hours old by the time somebody asks how far off it is.
        """
        self.state.update(values)
        if KEY_TIME in values:
            self._clock_heard_at = dt_util.now()

    async def _write_raw(self, payload: dict[int, Any]) -> None:
        """Write one command frame to the connected device.

        The caller must hold ``_lock`` and have ensured a connection: the
        connect path uses this for the bring-up sequence, and the command path
        (``_async_write``) wraps it with the lock and a retry.
        """
        if self._client is None:
            raise BleakError("write attempted while disconnected")
        self._writes_sent += 1  # counted before, so a raising write still counts
        with _gatt_call(self._client):
            await self._client.write_gatt_char(
                WRITE_UUID, cbor.encode(payload), response=True
            )
        self._note_answer()
        # Optimistic local echo; the device also notifies its new state.
        self._mirror(payload)

    async def _async_write(self, payload: dict[int, Any]) -> None:
        """Send a command, and tell the listeners how things stand after it.

        Whichever way it went: a command whose write failed has let go of the
        link, and that may have been all that made the lamp reachable. A
        failure is a ``HomeAssistantError`` (see ``_async_deliver``).
        """
        try:
            await self._async_deliver(payload)
        finally:
            self._async_notify_listeners()

    async def _async_deliver(self, payload: dict[int, Any]) -> None:
        """Serialize a command under the connection lock, with one reconnect.

        The write runs inside ``_lock`` so it cannot race a background
        reconnect; if it still fails (the link dropped mid-command) the
        connection is rebuilt once and the write retried.

        The whole attempt - including the wait for ``_lock``, which a
        background reconnect may be holding - is capped by
        ``_COMMAND_TIMEOUT``, and every failure is reported as a
        ``HomeAssistantError`` so the user gets a readable message rather than
        a stack trace after a long hang.
        """
        reports_before, writes_before = self._reports, self._writes_sent
        # The client the last attempt failed on, if it got as far as having one.
        failed: BleakClientWithServiceCache | None = None
        # The client a write is being waited on, for as long as it is.
        writing_to: BleakClientWithServiceCache | None = None
        try:
            async with asyncio.timeout(_COMMAND_TIMEOUT), self._lock:
                for attempt in range(1, _WRITE_ATTEMPTS + 1):
                    try:
                        await self._connect_locked(prime=False)
                        writing_to = self._client
                        await self._write_raw(payload)
                        writing_to = None
                        break
                    except _LINK_ERRORS as err:
                        writing_to = None
                        client, self._client = self._client, None
                        if attempt == _WRITE_ATTEMPTS or isinstance(
                            err, _NoNewLinkError
                        ):
                            # The last attempt - or the coordinator's own
                            # "no", which a second attempt would only be
                            # given again.
                            failed = client
                            raise
                        _LOGGER.debug(
                            "Write to %s failed (%s); reconnecting and retrying",
                            self.address,
                            _reason(err),
                        )
                        if client is not None:
                            # Finished before the retry dials: the hang-up
                            # closes the link in BlueZ, and a connect made
                            # ahead of that either fails or is handed the very
                            # link being closed. Shielded, so the command's
                            # deadline ends the wait and not the hang-up.
                            await asyncio.shield(self._hang_up(client))
        except _LINK_ERRORS as err:
            if writing_to is not None and writing_to is self._client:
                # The deadline ran out inside the write. The handler above
                # never saw it - a deadline arrives as a cancellation - so the
                # link is still held, and a link that has kept a write waiting
                # this long is not one to hand the next command. Unless it has
                # been let go of meanwhile: then it is no longer ours to take.
                failed, self._client = writing_to, None
            try:
                if (
                    self._writes_sent > writes_before
                    and await self._async_device_confirms(payload, reports_before)
                ):
                    _LOGGER.debug(
                        "Command to %s reported %s, but the device reports the "
                        "state it asked for - treating it as delivered",
                        self.address,
                        _reason(err),
                    )
                else:
                    _LOGGER.debug(
                        "Command to %s failed: %s", self.address, _reason(err)
                    )
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        # What stopped it, when it was the coordinator itself:
                        # "out of range, try a proxy" is advice for the radio.
                        translation_key=(
                            err.translation_key
                            if isinstance(err, _NoNewLinkError)
                            else "cannot_connect"
                        ),
                        translation_placeholders={"name": self.name},
                    ) from err
            finally:
                # Only now. The coordinator let go of this client when the
                # write failed, but its notifications are the channel the
                # confirmation above listens on, so it had to stay up until
                # the device had its chance to answer.
                if failed is not None:
                    self._hang_up(failed)

    async def _async_device_confirms(
        self, payload: dict[int, Any], reports_before: int
    ) -> bool:
        """Return True if the device reports the state ``payload`` asked for.

        A write-with-response can reach the lamp, be acted on, and still fail:
        on a weak link the acknowledgement is what goes missing, so bleak raises
        while the lamp does exactly as it was told and notifies its new state.
        Reporting that as a failure told the user the command had not worked
        while they watched the light change.

        The notification path is an independent channel, so it settles the
        question the acknowledgement could not. Only keys in ``STATE_KEYS`` are
        compared - a mode command also carries fixed parameters (0x2c, 0x32) the
        device never reports back, and requiring those to match would mean no
        mode command could ever confirm.

        A match alone proves nothing, which is why ``reports_before`` exists.
        The mirror is never invalidated - a disconnect clears the client, not
        the state - so it can be hours old, and "turn it off" against a stale
        mirror that already says off would confirm instantly while the lamp
        stays on and the user loses the one signal that it is unreachable. So
        the report has to be newer than the write, and the caller only asks at
        all once a write actually reached the characteristic: a command that
        never got that far has nothing to be vouched for.

        And it has to be about the command. The lamp reports of its own accord
        all day - its brightness, as the circadian curve moves it - and such a
        report is as fresh as any, while saying nothing of a power flag the
        mirror got wrong hours ago. So at least one of the properties the
        command set has to have been reported since the command was taken up
        (``reports_before`` is noted before the wait for the lock, so a report
        that came while the command waited counts: it is as fresh). Not all of
        them: the lamp reports what changed, and a mode command carries a
        ramp that is usually what it already was. What this leaves is a
        coincidence: one property reported with the value asked for, while
        another matches a mirror that is stale.
        """
        tracked = {key: value for key, value in payload.items() if key in STATE_KEYS}
        if not tracked:
            return False
        try:
            async with asyncio.timeout(_CONFIRM_TIMEOUT):
                while True:
                    # Clear before checking. Nothing can interleave between the
                    # two here - both are synchronous and the reports come from
                    # this same event loop - so the order is not load-bearing
                    # today; it is the order that stays correct if a report ever
                    # arrives from anywhere else.
                    self._state_reported.clear()
                    if all(self.state.get(k) == v for k, v in tracked.items()) and any(
                        self._reported_at.get(k, 0) > reports_before for k in tracked
                    ):
                        return True
                    await self._state_reported.wait()
        except TimeoutError:
            return False

    async def _async_activate_if_needed(self) -> None:
        """Bring the device up once if it reports as not yet activated (0x14).

        Runs inside the connection lock, after the state request - which is
        either a background connect or the priming the poll performs on a link
        a command established. Never from a command itself: those connect with
        ``prime=False`` and return before reaching here.
        """
        if self._activation_checked:
            return
        if self._state_request_muted:
            # This device is not reporting its properties, so 0x14 can never
            # arrive. Waiting for it on every connect is pure latency - and the
            # wait happens while the lock is held, so it delays anything queued
            # behind it - while a device whose activation flag cannot be read
            # must never be activated blind.
            self._activation_checked = True
            return
        # Wait (briefly) for the initial state - including 0x14 - to arrive.
        for _ in range(12):
            if KEY_ACTIVATED in self.state or not self._is_connected:
                break
            await asyncio.sleep(0.25)
        if self.state.get(KEY_ACTIVATED) is False:
            await self._async_activate()
        if self.state.get(KEY_ACTIVATED):
            self._activation_checked = True

    async def _async_sync_clock_if_needed(self) -> None:
        """Correct the device clock if what it reports has drifted.

        Runs on the priming path, where the clock has just been read. Silent
        when the lamp has not reported one: there is no drift to judge, and a
        blind write would be guessing at what it currently believes.
        """
        try:
            reported = protocol.device_time(self.state)
        except ValueError:  # a nonsense date is itself a reason to correct it
            _LOGGER.debug("%s reported an impossible clock; correcting", self.address)
        else:
            if reported is None:
                return
            # Naive, like the clock itself: the lamp keeps local wall-clock time.
            now = dt_util.now().replace(tzinfo=None)
            drift = abs((reported - now).total_seconds())
            if drift < _CLOCK_TOLERANCE:
                return
            _LOGGER.debug(
                "%s clock reads %s, %.0f s out; correcting",
                self.address,
                reported.isoformat(sep=" "),
                drift,
            )
        await self._write_raw(self._clock_command())

    def _clock_command(self) -> dict[int, Any]:
        """Return the command that sets the lamp's clock to now, local time."""
        return protocol.clock_command(dt_util.now())

    async def _async_activate(self) -> None:
        """Bring up a factory-reset device: clock + flags + enable light output.

        Replays the vendor app's first-pairing sequence - all local, no cloud and
        no BLE bond - so the light works without the app. The device gates its
        light output on 0x14; a virgin (factory-reset) device reports 0x14 False
        and its front-panel LEDs blink until this runs. Idempotent when already on.

        The caller must hold ``_lock`` and have a connection, as for
        ``_write_raw``, which this is three calls of. The one caller is
        ``_async_activate_if_needed``, on the priming path.
        """
        await self._write_raw({KEY_ACTIVATE_MISC: ACTIVATE_MISC_VALUE})
        await self._write_raw(self._clock_command())
        await self._write_raw({KEY_ACTIVATED: True})
        _LOGGER.info("Brought up (activated) %s", self.address)

    async def async_set_power(self, is_on: bool) -> None:
        """Turn the light on or off."""
        await self._async_write({KEY_POWER: is_on})

    async def async_set_brightness(self, value: int) -> None:
        """Set brightness as a 0..100 percentage."""
        await self._async_write({KEY_BRIGHTNESS: max(0, min(100, value))})

    async def async_set_light_state(
        self, is_on: bool, brightness: int | None = None
    ) -> None:
        """Set power and, optionally, brightness in a single CBOR command.

        The device accepts multi-key maps (as already used for the clock and
        location), so turning on at a brightness goes out atomically - one BLE
        write instead of two, with no on-at-old-then-change flicker.
        """
        payload: dict[int, Any] = {KEY_POWER: is_on}
        if brightness is not None:
            payload[KEY_BRIGHTNESS] = max(0, min(100, brightness))
        await self._async_write(payload)

    def _mode_payload(
        self, *, mode: int | None = None, ramp: bytes | None = None
    ) -> dict[int, Any]:
        """Build a lighting-mode command, preserving the other fields.

        The device expects the keys in the order mode, 0x2c, ramp, 0x32; the
        ramp (0x2f) is otherwise clobbered whenever the mode is set.

        Note the asymmetry, which is deliberate and can look like a regression:
        an unread ramp falls back to a default, an unread mode refuses. On a lamp
        that has not reported 0x2b - and some models never do - switching to
        Circadian or setting the ramp therefore raises rather than quietly
        writing index 1. See the comment on ramp_value below for why.
        """
        mode_value = (
            mode
            if mode is not None
            else self._require_read(
                self.state.get(KEY_LIGHTING_MODE), "lighting_mode_not_read"
            )
        )
        # Ramp keeps its default. Setting a mode rewrites ramp on the device
        # regardless, so there is no "leave it alone" option here and falling
        # back is deliberate. The mode above is different: defaulting it there
        # silently *changes* a setting the caller never asked to touch.
        ramp_value = (
            ramp
            if ramp is not None
            else self._desired_ramp or self.state.get(KEY_RAMP) or RAMP_DEFAULT
        )
        return {
            KEY_LIGHTING_MODE: mode_value,
            0x2C: MODE_PARAM_2C,
            KEY_RAMP: ramp_value,
            0x32: MODE_PARAM_32,
        }

    async def async_set_lighting_mode(self, index: int) -> None:
        """Select a circadian lighting mode by its index."""
        await self._async_write(self._mode_payload(mode=index))

    async def async_set_ramp(self, minutes: int) -> None:
        """Set the circadian ramp time in minutes (0 = Sun Sync auto)."""
        ramp = protocol.be2_minutes_to_bytes(minutes)
        await self._async_write(self._mode_payload(ramp=ramp))
        # Remembered only once the lamp has it. Building the command can
        # refuse and the write can fail; a ramp kept from either would be
        # re-applied by the next switch to Circadian, which then fails on
        # something the user was already told had not happened.
        self._desired_ramp = ramp

    async def async_set_operating_mode(self, mode: str) -> None:
        """Set the mutually-exclusive Manual/Circadian/Schedule mode."""
        if mode == MODE_CIRCADIAN:
            await self._async_write({KEY_SCHEDULE: False})
            await self._async_write({KEY_CIRCADIAN: True})
            # Enabling circadian resets the device's ramp to a default; re-apply
            # the user's ramp (with the current lighting mode) so it persists.
            if self._desired_ramp is not None:
                await self._async_write(self._mode_payload())
        elif mode == MODE_SCHEDULE:
            await self._async_write({KEY_CIRCADIAN: False})
            await self._async_write({KEY_SCHEDULE: True})
        else:  # manual
            await self._async_write({KEY_CIRCADIAN: False})
            await self._async_write({KEY_SCHEDULE: False})

    async def async_set_indicator(self, is_on: bool) -> None:
        """Turn the status indicator LED on or off."""
        await self._async_write({KEY_INDICATOR: is_on})

    async def async_set_dst(self, is_on: bool) -> None:
        """Enable or disable daylight-saving-time handling.

        Only the flag is changed; what is written with it, and why a lamp that
        has not reported yet is not refused, is ``protocol.with_dst``.
        """
        await self._async_write({KEY_DST: protocol.with_dst(self.state, is_on)})

    async def async_sync_location(self) -> None:
        """Push HA's home coordinates; the device recomputes its circadian curve.

        Home Assistant holds a latitude and a longitude always, as numbers:
        there is no missing one to look for, and both are zero when it was
        given no position. Zero and zero is therefore refused, and the user
        told to set a home first - written, it puts the lamp where the equator
        meets the prime meridian and its Circadian program under the sun of
        that place, after a press that said it had worked. One zero is a real
        place, on the equator or on the prime meridian, and is written.

        The refusal is the user's to put right, not a fault of the lamp or of
        the link, and is raised as such: Home Assistant shows it and keeps it
        out of the log.

        Without Home Assistant (``tools/bench.py``) there is no home to push.
        """
        if self.hass is None:
            return
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        if lat == 0 and lon == 0:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="home_location_not_set",
                translation_placeholders={"name": self.name},
            )
        await self._async_write({KEY_LATITUDE: float(lat), KEY_LONGITUDE: float(lon)})

    async def async_set_timer_start(self, hour: int, minute: int) -> None:
        """Set the schedule start time."""
        slot = self._require_read(
            protocol.with_schedule_start(self.state, hour, minute), "schedule_not_read"
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_end(self, hour: int, minute: int) -> None:
        """Set the schedule end time."""
        slot = self._require_read(
            protocol.with_schedule_end(self.state, hour, minute), "schedule_not_read"
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_brightness(self, value: int) -> None:
        """Set the schedule brightness (0..100)."""
        slot = self._require_read(
            protocol.with_schedule_brightness(self.state, value), "schedule_not_read"
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_gradual(self, minutes: int) -> None:
        """Set the schedule gradual on/off fade duration in minutes."""
        slot = self._require_read(
            protocol.with_schedule_gradual(self.state, minutes), "schedule_not_read"
        )
        await self._async_write({KEY_TIMER: slot})
