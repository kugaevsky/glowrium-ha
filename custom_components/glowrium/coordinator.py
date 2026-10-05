"""Active BLE coordinator for a single Glowrium device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from datetime import datetime, time, timedelta
from enum import Enum, auto
import logging
from time import monotonic
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.util import dt as dt_util

from . import cbor, protocol
from .const import (
    ACTIVATE_MISC_VALUE,
    DOMAIN,
    DST_OFF,
    DST_ON,
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
    KEY_TIME_SYNCED,
    KEY_TIMER,
    MODE_CIRCADIAN,
    MODE_MANUAL,
    MODE_PARAM_2C,
    MODE_PARAM_32,
    MODE_SCHEDULE,
    NOTIFY_UUID,
    RAMP_DEFAULT,
    STATE_KEYS,
    TIMER_BRIGHTNESS,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
    TIMER_START_H,
    TIMER_START_M,
    WRITE_UUID,
)
from .models import GlowriumModel, resolve_model

_LOGGER = logging.getLogger(__name__)
_RECONNECT_INTERVAL = timedelta(seconds=30)
_WRITE_ATTEMPTS = 2  # the initial write plus one reconnect-and-retry
# bleak-retry-connector defaults to 4 connect attempts, each of which can sit
# through a 20 s bleak timeout plus a backoff. Against an unreachable device
# that adds up to minutes while _lock is held, so a queued command cannot even
# start. Two attempts is enough: the reconnect poll comes round again in 30 s.
_CONNECT_ATTEMPTS = 2
# Ceiling on getting one user-facing command out, so a button reports a clear
# failure in seconds instead of appearing to hang while the retries stack up.
# A failed command may then spend up to _CONFIRM_TIMEOUT more deciding whether
# it failed after all, so the worst a user waits is the sum of the two.
_COMMAND_TIMEOUT = 15.0
# Ceiling on a background connect, including the wait for _lock. Without it a
# connect to an unreachable device holds the lock indefinitely, and everything
# else that needs the lock waits behind it with no deadline of its own.
#
# It is deliberately SHORTER than _COMMAND_TIMEOUT, and the relationship is the
# point rather than the number: a background connect holds the lock while a
# command waits for it inside its own budget, so a holder allowed longer than
# the waiter means pressing a switch during a background connect reports
# failure on a reachable lamp, having attempted nothing. It is also shorter
# than _RECONNECT_INTERVAL, so priming spawned by one poll tick is finished
# before the next. test_no_path_holds_the_lock_longer_than_a_command_will_wait
# pins both.
_CONNECT_TIMEOUT = 10.0
# How long a failed command waits for the device to report the state it asked
# for before the failure is believed. A write-with-response on a marginal link
# can reach the lamp and be acted on while the acknowledgement is lost, which
# bleak reports as failure. Observed once on a G7 at RSSI -88: the confirming
# notification arrived 22-32 ms BEFORE the error was raised, so this is grace
# for a slower link rather than a wait anyone should routinely pay.
_CONFIRM_TIMEOUT = 2.0
# Ceiling on each thing unload waits for - the lock, then the hang-up - so
# reloading the integration does not wait out whatever connect currently holds
# the lock, or a link that is slow to close. There it bounds the waits, not the
# hang-up. It is also the ceiling on a hang-up itself once Home Assistant is
# stopping (see async_shutdown).
_STOP_TIMEOUT = 3.0
# Ceiling on hanging up a link the coordinator has given up on (see _hang_up).
# It runs in the background, so nothing waits this out except a write retry
# and an unload, each under a deadline of its own - and Home Assistant when it
# is stopping, which is why a hang-up then gets _STOP_TIMEOUT instead. It
# matches how long bleak itself waits for BlueZ to confirm a disconnect, and it
# has to stay below _COMMAND_TIMEOUT, or a link that will not confirm it has
# closed leaves the retry no time to dial.
_HANG_UP_TIMEOUT = 10.0
# How many hang-ups in a row BlueZ may leave unanswered before it is taken for
# what it is: a stack holding on to a link that no longer exists. Seen on a
# real host (BlueZ 5.82, 2026-10-04): bluetoothd went on reporting the lamp
# connected after the controller had lost the link, every dial "connected" at
# once to nothing, and no Disconnect was answered again until the adapter was
# power-cycled. One unanswered hang-up proves nothing - BlueZ can just be slow.
_STACK_FAULT_AFTER = 3
# ...and how far apart the background dials may then drift. Dialling a wedged
# stack on every poll tick achieves nothing, and costs it a connect, a
# disconnect it cannot honour and a line in its journal each time. The gap
# doubles from the poll interval up to this. A command is never held back, and
# the first answer from the lamp ends it.
_STACK_FAULT_BACKOFF_MAX = 300.0
# Where the repair raised for that fault sends the reader for what to do.
_TROUBLESHOOTING_URL = "https://github.com/kugaevsky/glowrium-ha#troubleshooting"
# How much of the lamp's name the repair shows (see _as_text).
_NAME_SHOWN = 48
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
# 0x05 is year_BE(2), month, day, hour, minute, second.
_CLOCK_LENGTH = 7

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
# What a lost link looks like from here. Besides its own errors and timeouts,
# bleak passes on whatever the bus raised. When the lamp drops the link the
# disconnected callback hangs the client up at once, which closes its D-Bus
# connection, and a call still waiting for its reply on that connection gets
# EOFError - or, with the socket gone, "Bad file descriptor" - rather than a
# BleakError. To the coordinator all of these say the same thing: this link is
# gone. (TimeoutError is an OSError and is named only so that it can be read.)
_LINK_ERRORS = (BleakError, TimeoutError, EOFError, OSError)

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


class _Bus(Enum):
    """What was behind a client that had been told to disconnect."""

    CLEAR = auto()  # no bus open: closed by bleak, already down, or none at all
    CLOSED = auto()  # one still open, and closed here
    OPEN = auto()  # one that may still be open and could not be closed


class _Asked(Enum):
    """What came of asking the lamp to report its state."""

    REPORTED = auto()  # it did
    SILENT = auto()  # it acknowledged the request and reported nothing
    REFUSED = auto()  # it answered the request with a refusal
    LOST = auto()  # the request met a link that is gone


def _close_bus(backend: Any, address: str) -> _Bus:
    """See that no D-Bus connection is left open behind a client we let go of.

    ``backend`` is what sits behind the client: for a local adapter, bleak's
    BlueZ client, which opens a connection to the system bus in ``connect()``
    and closes it on the last lines of a ``disconnect()`` that got that far.
    One that did not - BlueZ never answered, answered with an error, or the
    call was cancelled - leaves the connection open with nothing public to
    close it by. So this reaches for the bus itself.

    Before closing it, bleak is put in the state it puts itself in when BlueZ
    reports a link gone, which is the report that did not come: its monitor
    task released, its watcher removed, its services forgotten. Otherwise each
    client closed this way would leave those behind for as long as the stack
    stays silent.

    A bus that is already down is left alone: dbus-fast's ``disconnect()``
    shuts the socket down, and on one that is gone it logs a warning with a
    traceback instead of raising - once per client, in the very state where the
    log is being read.

    Returns ``_Bus.OPEN`` when there may be a connection and it could not be
    closed - bleak has moved what this reaches for, or the bus refused. The
    caller then keeps the client instead of forgetting it (see
    ``_async_disconnect``).
    """
    if backend is None:
        return _Bus.OPEN
    if not hasattr(backend, "_bus"):
        # A Bluetooth proxy's client has no bus of its own, and nothing to
        # close. BlueZ's client without one is bleak rearranged.
        moved = "bluezdbus" in type(backend).__module__
        return _Bus.OPEN if moved else _Bus.CLEAR
    # bleak's own attributes from here on: there is no public way to do this.
    bus = backend._bus  # noqa: SLF001
    if bus is None:
        return _Bus.CLEAR
    try:
        backend._is_connected = False  # noqa: SLF001
        monitor = getattr(backend, "_disconnect_monitor_event", None)
        if monitor is not None:
            monitor.set()
            backend._disconnect_monitor_event = None  # noqa: SLF001
        backend._cleanup_all()  # noqa: SLF001
    except Exception as err:  # noqa: BLE001 - whatever bleak's own tidying raises
        # Tidying, and not ours to rely on: the bus is closed either way.
        _LOGGER.debug("%s: tidying up behind a client failed: %r", address, err)
    was_up = getattr(bus, "connected", True)
    try:
        if was_up:
            bus.disconnect()
    except Exception as err:  # noqa: BLE001 - the bus is closed or it is not
        _LOGGER.debug("%s: closing a client's bus failed: %r", address, err)
        return _Bus.OPEN
    backend._bus = None  # noqa: SLF001
    if not was_up:
        return _Bus.CLEAR
    _LOGGER.debug("%s: closed the bus a hang-up left open", address)
    return _Bus.CLOSED


def _looks_like_a_refusal(err: Exception) -> bool:
    """Return True if ``err`` reads as the device declining, not as a lost link.

    Deliberately narrow: an unrecognised error is treated as the link, because
    the cost of guessing wrong that way is one more request on the next connect,
    while guessing wrong the other way silences a working lamp for the session.
    """
    text = str(err).lower()
    return any(marker in text for marker in _REFUSAL_MARKERS)


def _encode_device_time(now: datetime | None = None) -> bytes:
    """Encode local time as the device clock: year_be(2), month, day, H, M, S."""
    now = now or dt_util.now()
    return bytes(
        [
            now.year >> 8,
            now.year & 0xFF,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
        ]
    )


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


def _as_text(name: str) -> str:
    """Return ``name`` with nothing in it that Markdown or HTML would act on.

    For a name that goes into text Home Assistant renders: the lamp's name
    comes off the air, and whatever advertises one beginning with "Glowrium"
    can be set up. Letters and digits of any script, spaces, dashes and
    underscores are kept - enough to recognise the lamp by. Not a dot: that is
    all it takes to make an address clickable.
    """
    kept = "".join(char if char.isalnum() or char in " -_" else " " for char in name)
    return " ".join(kept.split())[:_NAME_SHOWN] or "the lamp"


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

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        model_id: str | None = None,
    ) -> None:
        """Initialize the coordinator for the device at ``address``.

        ``model_id`` is the model an earlier session read off the lamp, if
        one did. The entities are built before this session has read
        anything, and which presets a lamp has depends on its model.
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
        self._client: BleakClientWithServiceCache | None = None
        # The device resets its ramp to a default when circadian is re-enabled,
        # so remember the user's chosen ramp and re-apply it on mode switch.
        self._desired_ramp: bytes | None = None
        self._lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
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
        # Hang-ups in flight when there is no hass to keep them (see _hang_up).
        self._hang_ups: set[asyncio.Task[None]] = set()
        # What sits behind each client, noted when the client is taken: by
        # the time it has to be closed, Home Assistant's wrapper may have
        # forgotten its backend (see _close_bus).
        self._backends: dict[BleakClientWithServiceCache, Any] = {}
        # Clients that would not hang up and whose bus could not be closed
        # either. While there is one, nothing is dialled (see _connect_locked).
        self._unreleased: set[BleakClientWithServiceCache] = set()
        # Hang-ups in a row that BlueZ left unanswered (see _STACK_FAULT_AFTER),
        # and the moment before which the poll does not dial because of them.
        self._stuck_hang_ups = 0
        self._dial_not_before = 0.0
        # Whether this coordinator has announced such a run - in the log and
        # as a repair - and so has an episode to call over (see _note_answer).
        self._fault_announced = False
        # The client whose link BlueZ called "not connected" without reporting
        # it dropped, and when (see _LOST_GRACE).
        self._lost: tuple[BleakClientWithServiceCache, float] | None = None
        # When the lamp last answered anything (see _PROBE_INTERVAL).
        self._last_answer = monotonic()
        # The keys the lamp has reported since it was last asked for its state.
        self._carried: set[int] = set()
        self._present = False
        # What the log last said about the lamp being in reach (see
        # _async_log_reach). Starts as "in reach", so a lamp that is absent
        # from the first moment is said to be.
        self._logged_in_reach = True
        self._reconnecting = False
        # Set by async_stop and never cleared: a stopped coordinator takes no
        # new link (see _connect_locked). Nothing would ever let go of it.
        self._stopped = False
        # Set when Home Assistant itself is stopping (see async_shutdown).
        self._shutting_down = False
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

    @property
    def _plain_name(self) -> str:
        """The lamp's name without its address after it.

        A lamp picked from the list of discovered devices is titled with its
        name and its address in brackets. Where the address is said anyway, or
        where brackets and colons do not survive (see ``_as_text``), the name
        alone reads better.
        """
        return self.name.removesuffix(f" ({self.address})")

    @property
    def _is_connected(self) -> bool:
        """Return True while a live GATT connection is held."""
        return self._client is not None and self._client.is_connected

    @property
    def available(self) -> bool:
        """Entity availability: the device is advertising, or there is a link.

        A GATT link to this lamp does not last, and tying availability to it
        makes every entity flap to ``unavailable`` on each reconnect. The
        device advertises continuously, so "present, or currently connected"
        is treated as available and the link is rebuilt silently underneath.
        """
        return self._is_connected or self._present

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

        return _remove

    @callback
    def _async_notify_listeners(self) -> None:
        self._async_log_reach()
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _async_log_reach(self) -> None:
        """Say once when the lamp goes out of reach, and once when it is back.

        The entities go unavailable then, and without this nothing says why or
        since when. Judged by what the entities are judged by - an
        advertisement or a link - so a lamp that is quiet while connected is
        not reported as gone. Checked wherever the listeners are told, which
        is everywhere either of the two changes while the lamp is watched: a
        coordinator that is stopping lets go of its link and says nothing.
        """
        if self._stopped:
            # It let go of its link because it was told to and no longer hears
            # advertisements: where the lamp is, it cannot say. A command that
            # still arrives tells the listeners, and comes through here.
            return
        in_reach = self.available
        if in_reach == self._logged_in_reach:
            return
        self._logged_in_reach = in_reach
        if in_reach:
            _LOGGER.info("%s (%s) is back in reach", self._plain_name, self.address)
        else:
            _LOGGER.info(
                "%s (%s) is out of reach: it is not advertising and there is no "
                "link to it. Its entities are unavailable until it is heard again",
                self._plain_name,
                self.address,
            )

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
        # Before anything is registered. Home Assistant replays the last
        # advertisement from inside async_register_callback when it already
        # knows the device - every reload, for a lamp that advertises all the
        # time - and the reconnect that callback starts needs the entry to be
        # put on. Without it the task lands on hass and outlives the unload.
        self._entry = entry
        self._cancel_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Track presence so entity availability follows the device, not the link.
        self._cancel_unavailable = bluetooth.async_track_unavailable(
            self.hass, self._async_on_unavailable, self.address, connectable=True
        )
        self._present = bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )
        self._async_log_reach()  # absent from the start is worth saying too
        self._spawn(self._async_initial_connect(), "initial connect")
        # Advertisement callbacks are throttled, so also poll: reconnect within
        # _RECONNECT_INTERVAL after any drop, regardless of advertisement timing.
        self._cancel_poll = async_track_time_interval(
            self.hass, self._async_poll_reconnect, _RECONNECT_INTERVAL
        )

    @callback
    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        """Run ``coro`` in the background, for no longer than the entry lives."""
        name = f"glowrium {what} {self.address}"
        if self._entry is None:  # standalone use, outside Home Assistant
            self.hass.async_create_task(coro, name)
            return
        self._entry.async_create_background_task(self.hass, coro, name)

    async def _async_initial_connect(self) -> None:
        """Connect once at start-up, off the setup path."""
        try:
            await self._async_ensure_connected()
        except _LINK_ERRORS as err:
            _LOGGER.debug("Initial connect to %s failed: %s", self.address, err)

    @callback
    def _async_stop_watching(self) -> None:
        """Stop reacting to the lamp, and take no new link from here on."""
        # A command may be in flight and outlast this; from here on it is
        # refused a new link, so that what the caller lets go of next is the
        # last one this coordinator will ever hold.
        self._stopped = True
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
        self._fault_announced = False

    @callback
    def async_shutdown(self, _event: Event | None = None) -> None:
        """Hang up as Home Assistant stops: at once, and without waiting.

        Home Assistant does not unload its config entries when it stops, so
        async_stop never runs then, and nothing hangs the link up but bleak on
        the way out of a stop that runs to its end. One that is cut short - a
        container is given ten seconds - leaves BlueZ holding a link to a
        lamp with a single slot, which reads as connected and answers
        nothing until the adapter is power-cycled.

        So this asks BlueZ to drop the link the moment the stop is announced.
        It does not wait for the lock, and from here on every hang-up gets
        the short ceiling - this one, and any that a connect cancelled by
        the stop or a command finishing after it starts later. Home
        Assistant waits for whatever starts once it has begun to stop, and
        spending the grace period on a link that will not confirm it has
        closed would cost the rest of the shutdown. The bus is not worth
        waiting for either - the process is leaving.
        """
        self._shutting_down = True
        self._async_stop_watching()
        client, self._client = self._client, None
        # Said either way: nothing else will tell, afterwards, whether a link
        # was held at this moment and let go of.
        _LOGGER.debug(
            "%s: Home Assistant is stopping: %s",
            self.address,
            "no link held" if client is None else "hanging up",
        )
        if client is not None:
            self._hang_up(client)

    async def async_stop(self) -> None:
        """Cancel watching and disconnect."""
        # Before anything is awaited.
        self._async_stop_watching()
        if self._client is None:
            # Nothing is held, and from here on nothing can be: a command still
            # dialling is refused the link it is dialling for. So there is
            # nothing to wait for, not even the lock that command holds -
            # waiting for it is what made a reload take seconds for nothing.
            return
        # The lock and the hang-up are separate problems, so they get separate
        # deadlines. Waiting for the lock is best-effort: a connect in flight
        # holds it for longer than anyone should wait on unload, and that is
        # what made reloading take the best part of ten seconds. Taking it when
        # it is free keeps the disconnect from racing an in-flight write; not
        # getting it is no reason to skip the disconnect, since the watchers
        # are cancelled and this coordinator is finished either way.
        #
        # The link itself must be closed exactly once: bleak does not hang up
        # on garbage collection, and this lamp has one connection slot, so an
        # abandoned link keeps its successor out until it drops by itself.
        # Trying twice just spends the ceiling twice against a link
        # that is already gone.
        #
        # The ceiling is on how long unload waits, not on the hang-up, which is
        # why it is shielded: a disconnect cancelled here has asked BlueZ to
        # drop the link and left the client's D-Bus connection open (see
        # _hang_up), once for every reload that meets a slow link.
        held = False
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_STOP_TIMEOUT):
                    await self._lock.acquire()
                    held = True
        finally:
            # Taken only now, and taken even if the wait was cancelled: taking
            # it first and waiting afterwards meant a cancellation in between
            # dropped the only reference to a connected client.
            client, self._client = self._client, None
            hang_up = None if client is None else self._hang_up(client)
        try:
            if hang_up is not None:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(_STOP_TIMEOUT):
                        await asyncio.shield(hang_up)
        finally:
            if held:
                self._lock.release()

    @callback
    def _hang_up(self, client: BleakClientWithServiceCache) -> asyncio.Task[None]:
        """Let go of ``client`` and disconnect it in the background.

        The two belong together. bleak opens a D-Bus connection of its own for
        every client. A connect that fails closes it; once a client has
        connected, it is closed in ``disconnect()`` and nowhere else - not when
        the reference is dropped, and not when the link itself goes down.
        A client that is merely forgotten keeps that connection for the life
        of the process, and the system bus allows one user 256 of them. With
        a link made and lost on every poll tick - which is what reading the
        state on every connect did on BlueZ - that took about two and a half
        hours; after it nothing running as that user could reach the bus at
        all, Bluetooth included.

        So this is called for every client the coordinator is finished with,
        including one whose link is already gone - then it costs nothing, as
        bleak has no device left to disconnect and only closes the bus.

        The task goes on hass, not on the entry like the rest of the background
        work (see ``_spawn``), and its caller's deadline cannot cancel it. That
        rule exists because a connect that outlives its coordinator claims the
        lamp's slot for nobody; a hang-up that outlives it gives the slot back,
        and one cut short is exactly the leak described above.
        """
        if client is self._client:
            self._client = None
        coro = self._async_disconnect(client)
        name = f"glowrium hang up {self.address}"
        if self.hass is not None:
            return self.hass.async_create_task(coro, name)
        # Standalone, with no Home Assistant behind it (tools/bench.py). Nothing
        # keeps the task for us there, and the loop itself holds it only weakly.
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._hang_ups.add(task)
        task.add_done_callback(self._hang_ups.discard)
        return task

    async def _async_disconnect(self, client: BleakClientWithServiceCache) -> None:
        """Disconnect ``client`` under a ceiling, and leave no bus open behind it.

        Asking bleak to disconnect is the polite half, and it can fail: BlueZ
        may never answer, may answer with an error, and the ceiling or a
        cancellation may cut the call short. In each of those bleak has not
        reached the lines that close the client's bus. So whatever came of the
        call, the bus is closed here (see ``_close_bus``).

        If that cannot be done either, the client is not forgotten: it stays in
        ``_unreleased``, nothing is dialled over it, and the poll tries it
        again. One connection is then held for as long as the stack stays
        wedged - instead of one more every time the poll comes round.
        """
        # Short once Home Assistant is stopping: it waits for this task.
        ceiling = _STOP_TIMEOUT if self._shutting_down else _HANG_UP_TIMEOUT
        backend = self._backends.get(client)
        if backend is None:
            backend = getattr(client, "_backend", None)
        hung_up = unanswered = False
        try:
            async with asyncio.timeout(ceiling):
                await client.disconnect()
            hung_up = True
        except Exception as err:  # noqa: BLE001 - explained below
            unanswered = isinstance(err, TimeoutError)
            # Anything at all: besides its own errors, bleak passes on whatever
            # the bus raised and ends on an assertion. Nobody is waiting for
            # this, and there is nothing a caller could do about it.
            _LOGGER.debug("Hanging up %s failed: %r", self.address, err)
        finally:
            # On a cancellation too: it leaves the bus as open as a failure.
            bus = _close_bus(backend, self.address)
            if hung_up or bus is not _Bus.OPEN:
                self._backends.pop(client, None)
                self._unreleased.discard(client)
            else:
                self._unreleased.add(client)
            if unanswered and bus is _Bus.CLOSED:
                # BlueZ's own client, its bus still open, and no answer at
                # all: BlueZ would not hang up. An error would have been an
                # answer, and a proxy's client is not BlueZ's to answer for.
                self._note_stuck_hang_up()
            elif hung_up and self._stuck_hang_ups < _STACK_FAULT_AFTER:
                # "In a row" means in a row. Once it has been called a
                # fault, only the lamp answering ends it (_note_answer).
                self._stuck_hang_ups = 0

    def _note_stuck_hang_up(self) -> None:
        """Count a hang-up BlueZ did not answer; a run of them is the stack.

        From ``_STACK_FAULT_AFTER`` on, the background dials back off: the
        link they would get is the one BlueZ will not let go of, and it answers
        nothing. Said once per episode, and loudly, because nothing the
        integration can do ends it - somebody has to reset the adapter.
        """
        self._stuck_hang_ups += 1
        over = self._stuck_hang_ups - _STACK_FAULT_AFTER
        if over < 0 or self._stopped:
            # A hang-up is given longer than an unload waits for it, so the
            # one that makes it a run can come in after the watching stopped.
            # Nothing dials any more, and an episode announced now is one
            # nobody would be there to call over: the repair would stand
            # until Home Assistant restarted.
            return
        # The exponent is capped as well as the gap: a wedge left alone for
        # days would otherwise raise OverflowError here, on every hang-up.
        gap = _RECONNECT_INTERVAL.total_seconds() * 2 ** min(over + 1, 16)
        self._dial_not_before = monotonic() + min(gap, _STACK_FAULT_BACKOFF_MAX)
        if over == 0:
            self._fault_announced = True
            self._async_raise_stack_issue()
            _LOGGER.warning(
                "%s: BlueZ has left %d requests in a row to disconnect the lamp "
                "unanswered. That points at the host's Bluetooth stack holding on "
                "to a link that no longer exists, not at the lamp; it clears when "
                "the adapter is power-cycled (bluetoothctl power off, then power "
                "on) or the bluetooth service is restarted. Until the lamp "
                "answers again it is tried less often",
                self.address,
                self._stuck_hang_ups,
            )

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
    def _async_raise_stack_issue(self) -> None:
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
                "name": _as_text(self._plain_name),
                "count": str(self._stuck_hang_ups),
            },
        )

    @callback
    def _async_clear_stack_issue(self) -> None:
        """Take the repair down: the lamp answered, or nobody is watching."""
        if self.hass is None:
            return
        ir.async_delete_issue(self.hass, DOMAIN, self._stack_issue_id)

    def _note_answer(self) -> None:
        """Record that the lamp answered: the link is alive, the stack with it."""
        self._last_answer = monotonic()
        if self._fault_announced:
            self._fault_announced = False
            # At the level the episode was announced at, or whoever read
            # that warning never learns it is over. Only an episode this
            # coordinator announced: a count can reach the mark after it has
            # stopped, without a word, and the repair standing under the
            # entry's id by then is the next coordinator's.
            _LOGGER.warning(
                "%s: the lamp answers again; the Bluetooth stack has let go",
                self.address,
            )
            self._async_clear_stack_issue()
        self._stuck_hang_ups = 0
        self._dial_not_before = 0.0
        self._lost = None  # whatever BlueZ called it, it is answering

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
            async with asyncio.timeout(_CONNECT_TIMEOUT), self._lock:
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
            _LOGGER.debug("Priming state of %s failed: %s", self.address, err)
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
            async with asyncio.timeout(_CONNECT_TIMEOUT), self._lock:
                held = self._client
                if held is None or (monotonic() - self._last_answer < _PROBE_INTERVAL):
                    return
                client = held
                alive = await self._request_state(client)
        except _LINK_ERRORS as err:
            _LOGGER.debug("Probing the link to %s failed: %r", self.address, err)
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
            _LOGGER.debug("Reconnect to %s failed: %s", self.address, err)
        finally:
            self._reconnecting = False

    def _ble_device(self) -> BLEDevice | None:
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
        if self._stopped:
            # Only a command gets here: one already in flight when the entry
            # was unloaded, or one sent after Home Assistant began to stop.
            raise BleakError(f"{self.address}: stopped, taking no new link")
        if self._unreleased:
            # Every dial opens a connection to the system bus, and the last
            # one could be neither hung up nor closed (see _async_disconnect).
            raise BleakError(
                f"{self.address}: the previous link is still open and will "
                "not close; not dialling over it"
            )
        device = self._ble_device()
        if device is None:
            raise BleakError(f"{self.address} is not in range")
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.name,
            disconnected_callback=self._async_on_disconnect,
            max_attempts=_CONNECT_ATTEMPTS,
        )
        self._backends[client] = getattr(client, "_backend", None)
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
            if self._stopped:
                # Stopped while this connect was on its way. Keeping the
                # link would hand it to a coordinator nobody will stop
                # again, and the lamp has one slot.
                raise BleakError(f"{self.address}: stopped while connecting")  # noqa: TRY301
        except BaseException as err:
            # Including cancellation by a deadline. Nothing references this
            # client yet, and bleak does not hang up on garbage collection, so
            # walking away here would leave the lamp's only slot taken.
            hang_up = self._hang_up(client)
            if isinstance(err, Exception):
                # A failure rather than a cancellation, so the caller may dial
                # again at once - and must not be handed the link that is
                # being closed (see _async_write). Shielded: a deadline ends
                # the wait, not the hang-up. A cancellation is not kept
                # waiting at all: its deadline has already run out, and the
                # lock is held here.
                await asyncio.shield(hang_up)
            raise
        # Committed only once notifications are live: a client without them
        # reports as connected forever while no state ever arrives again.
        self._client = client
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
            raw = await client.read_gatt_char(INFO_UUID)
        except _LINK_ERRORS as err:
            _LOGGER.debug("Device-info read from %s failed: %s", self.address, err)
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
        report them in a notification. ``NOTIFY_UUID`` is readable too, and
        from 0.2.0 it was read first, on every connect - which looked sturdier,
        and on BlueZ ended every link it touched. Measured on a G7 (BlueZ 5.82,
        2026-10-05), outside this integration and between its poll ticks: a
        link left alone, subscribed to, or asked for its state by a write was
        still up at the end of the test, nine runs out of nine; a link that had
        one characteristic read - the 235-byte state, the 93-byte device info,
        a single byte - was gone 2.02 to 2.04 s later, six runs out of six. The
        read itself succeeds; BlueZ takes the ATT channel down right after it,
        so the very next call answers "Not connected", and two seconds later
        the link follows. That was read as a lamp at the edge of range dropping
        its link on every poll. It was this method. (Why BlueZ does it is not
        established; a read through macOS does no such thing.)

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
            raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
        except _LINK_ERRORS as err:
            _LOGGER.debug("%s state read failed: %s", self.address, err)
            return False, frozenset()
        return True, self._ingest(raw)

    async def _async_ask_state(self, client: BleakClientWithServiceCache) -> _Asked:
        """Write the state request and wait for the lamp to report."""
        self._carried.clear()
        before = self._reports
        try:
            await client.write_gatt_char(NOTIFY_UUID, bytes(STATE_KEYS), response=True)
        except _LINK_ERRORS as err:
            if not _looks_like_a_refusal(err):
                _LOGGER.debug(
                    "%s state request failed, but not by refusing: %s",
                    self.address,
                    err,
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
                "%s (model %s, firmware %s) refused the batched state request "
                "%d times in a row, most recently: %s. %s "
                "Commands still work; properties a read of the state does not "
                "carry stay unknown. Please report this model",
                self.address,
                self.model_id or "unknown",
                self.sw_version or "unknown",
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
            "%s (model %s, firmware %s) sent a frame with %d trailing bytes "
            "and it was dropped: %s. The frame declared less than it carried, "
            "so accepting the remainder could mean acting on a corrupt state. "
            "Please report this frame - it is exactly the hex dump needed. %s",
            self.address,
            self.model_id or "unknown",
            self.sw_version or "unknown",
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
            "%s (model %s, firmware %s) sent a frame with an item this "
            "integration cannot read (%s): %s. The %d properties ahead of it "
            "were kept; whatever follows it could not be found. Please report "
            "this frame - it is exactly the hex dump needed. %s",
            self.address,
            self.model_id or "unknown",
            self.sw_version or "unknown",
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
    def _async_on_disconnect(self, client: BleakClientWithServiceCache) -> None:
        if client is not self._client:
            # A client we already gave up on - a failed write drops one and the
            # retry establishes another, and the OS notices the first is gone
            # some time later. Clearing the live connection here would leave it
            # holding the lamp's single slot with nothing referencing it, while
            # every reconnect fails for want of that slot.
            #
            # Or a client that is not ours yet: bleak reports a link lost in
            # the middle of a connect to this same callback, while
            # establish_connection is still at work on that client. Neither is
            # hung up from here. The first already has been; the second is
            # bleak's to clean up, and disconnecting it underneath its own
            # connect closes the bus that clean-up needs - seen on a real lamp
            # as "Failed to cancel connection ... Bad file descriptor" on
            # every such drop.
            _LOGGER.debug("%s: a superseded connection dropped", self.address)
            return
        _LOGGER.debug("%s disconnected", self.address)
        # The link is down, but the client still holds its D-Bus connection
        # (see _hang_up).
        self._hang_up(client)
        self._async_notify_listeners()

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
        try:
            async with asyncio.timeout(_COMMAND_TIMEOUT), self._lock:
                for attempt in range(1, _WRITE_ATTEMPTS + 1):
                    try:
                        await self._connect_locked(prime=False)
                        await self._write_raw(payload)
                        break
                    except _LINK_ERRORS as err:
                        client, self._client = self._client, None
                        if attempt == _WRITE_ATTEMPTS:
                            failed = client
                            raise
                        _LOGGER.debug(
                            "Write to %s failed (%s); reconnecting and retrying",
                            self.address,
                            err,
                        )
                        if client is not None:
                            # Finished before the retry dials: the hang-up
                            # closes the link in BlueZ, and a connect made
                            # ahead of that either fails or is handed the very
                            # link being closed. Shielded, so the command's
                            # deadline ends the wait and not the hang-up.
                            await asyncio.shield(self._hang_up(client))
        except _LINK_ERRORS as err:
            try:
                if (
                    self._writes_sent > writes_before
                    and await self._async_device_confirms(payload, reports_before)
                ):
                    _LOGGER.debug(
                        "Command to %s reported %s, but the device reports the "
                        "state it asked for - treating it as delivered",
                        self.address,
                        err,
                    )
                else:
                    _LOGGER.debug("Command to %s failed: %s", self.address, err)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="cannot_connect",
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
            await self.async_activate()
        if self.state.get(KEY_ACTIVATED):
            self._activation_checked = True

    async def _async_sync_clock_if_needed(self) -> None:
        """Correct the device clock if what it reports has drifted.

        Runs on the priming path, where the clock has just been read. Silent
        when the lamp has not reported one: there is no drift to judge, and a
        blind write would be guessing at what it currently believes.
        """
        raw = self.state.get(KEY_TIME)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < _CLOCK_LENGTH:
            return
        try:
            # Naive on purpose: the lamp keeps local wall-clock time and has
            # no notion of a zone, and it is compared with local time below.
            reported = datetime(  # noqa: DTZ001
                (raw[0] << 8) | raw[1], raw[2], raw[3], raw[4], raw[5], raw[6]
            )
        except ValueError:  # a nonsense date is itself a reason to correct it
            reported = None
        now = dt_util.now().replace(tzinfo=None)
        if reported is not None:
            drift = abs((reported - now).total_seconds())
            if drift < _CLOCK_TOLERANCE:
                return
            _LOGGER.debug(
                "%s clock reads %s, %.0f s out; correcting",
                self.address,
                reported.isoformat(sep=" "),
                drift,
            )
        else:
            _LOGGER.debug("%s reported an impossible clock; correcting", self.address)
        await self._write_raw({KEY_TIME: _encode_device_time(), KEY_TIME_SYNCED: 1})

    async def async_activate(self) -> None:
        """Bring up a factory-reset device: clock + flags + enable light output.

        Replays the vendor app's first-pairing sequence - all local, no cloud and
        no BLE bond - so the light works without the app. The device gates its
        light output on 0x14; a virgin (factory-reset) device reports 0x14 False
        and its front-panel LEDs blink until this runs. Idempotent when already on.
        """
        await self._write_raw({KEY_ACTIVATE_MISC: ACTIVATE_MISC_VALUE})
        await self._write_raw({KEY_TIME: _encode_device_time(), KEY_TIME_SYNCED: 1})
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

        The 0x35 slot is a flag plus the offset to apply, written together, so
        only the flag is ours to change. Sending a fixed hour would turn a
        half-hour region into a full one the moment the switch is touched,
        discarding a value the lamp had been reporting correctly all along.
        Unlike the schedule slot this is a single field with a near-universal
        default, so a lamp that has not reported yet gets the hour rather than
        a refusal - that keeps the switch usable before priming.
        """
        reported = self.state.get(KEY_DST)
        default = DST_ON if is_on else DST_OFF
        if not isinstance(reported, (bytes, bytearray)) or len(reported) != len(
            default
        ):
            await self._async_write({KEY_DST: default})
            return
        await self._async_write({KEY_DST: bytes([int(is_on)]) + bytes(reported[1:])})

    async def async_sync_location(self) -> None:
        """Push HA's home coordinates; the device recomputes its circadian curve."""
        if self.hass is None:
            return
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        if lat is None or lon is None:
            return
        await self._async_write({KEY_LATITUDE: float(lat), KEY_LONGITUDE: float(lon)})

    async def async_set_timer_start(self, hour: int, minute: int) -> None:
        """Set the schedule start time."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_START_H], slot[TIMER_START_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_end(self, hour: int, minute: int) -> None:
        """Set the schedule end time."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_END_H], slot[TIMER_END_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_brightness(self, value: int) -> None:
        """Set the schedule brightness (0..100)."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_BRIGHTNESS] = max(0, min(100, value))
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_gradual(self, minutes: int) -> None:
        """Set the schedule gradual on/off fade duration in minutes."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_GRADUAL : TIMER_GRADUAL + 2] = protocol.be2_minutes_to_bytes(minutes)
        await self._async_write({KEY_TIMER: bytes(slot)})
