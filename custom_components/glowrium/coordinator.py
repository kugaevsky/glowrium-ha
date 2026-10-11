"""Active BLE coordinator for a single Glowrium device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime, time
from enum import Enum, auto
import logging
from time import monotonic
from typing import Any, Final, NamedTuple, Protocol

from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
    KEY_MODE_PARAM_2C,
    KEY_MODE_PARAM_32,
    KEY_POWER,
    KEY_RAMP,
    KEY_SCHEDULE,
    KEY_TIME,
    KEY_TIMER,
    KNOWN_KEYS,
    MODE_CIRCADIAN,
    MODE_PARAM_2C,
    MODE_PARAM_32,
    MODE_SCHEDULE,
    NOTIFY_UUID,
    RAMP_DEFAULT,
    STATE_KEYS,
    WRITE_UUID,
)
from .link import (
    Dial,
    Link,
    LinkLostError,
    NoNewLinkError,
    RefusedError,
    Turn,
    Unclosed,
)
from .mirror import Mirror
from .models import GlowriumModel, resolve_model
from .protocol import UNUSABLE, Unusable, usable

_LOGGER = logging.getLogger(__name__)
# How long the lamp gets to report once it has acknowledged the state request.
# On a G7 the report arrives inside the write call itself. The wait is for a
# model that splits its map across notifications, and with the dial before it
# has to fit inside the link's _CONNECT_TIMEOUT.
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
# failure means nothing on a weak link - a dropped connection can surface as
# the same error as an outright refusal - and giving up after one leaves a
# lamp that has to be read without every property a read does not carry.
_STATE_REQUEST_ATTEMPTS = 3
# ...and muted only for this long, not for the session. A model that genuinely
# refuses the request must not have its link torn down on every connect, but a
# lamp on a weak signal fails the same way and then recovers: observed on a G7 at
# RSSI -88, three consecutive failures accumulated about 70 s after start-up
# purely from a bad link, which permanently cost it four properties until Home
# Assistant was restarted. Muting expires so that heals itself.
_STATE_REQUEST_COOLDOWN = 600.0


class _Asked(Enum):
    """What came of asking the lamp to report its state."""

    REPORTED = auto()  # it did
    SILENT = auto()  # it acknowledged the request and reported nothing
    REFUSED = auto()  # it answered the request with a refusal
    LOST = auto()  # the request met a link that is gone


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


class _Refusal(NamedTuple):
    """The two messages for a command that has nothing to build on.

    A key for each setting and each reason, not one message with the
    setting's name put in: the name of a setting is a noun to translate,
    not a value.
    """

    # The lamp has not reported the setting yet: it will, and waiting helps.
    not_read: str
    # It has, and what it reported cannot be used: waiting will not.
    unusable: str


_NO_LIGHTING_MODE: Final = _Refusal("lighting_mode_not_read", "lighting_mode_unusable")
_NO_SCHEDULE: Final = _Refusal("schedule_not_read", "schedule_unusable")


class Keeper(Protocol):
    """What keeps a coordinator, and what the device half asks of it.

    Home Assistant keeps one for a config entry (``keeper.EntryKeeper``); the
    bench keeps one for a run. The device half asks for what is listed here
    and for nothing else of whatever runs it, and never asks whether it is
    there: a coordinator is built with its keeper.
    """

    def spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Start work that does not outlive what the coordinator is kept for.

        For Home Assistant that is the config entry, and its unload ends the
        work: a connect that outlived its coordinator would finish and claim
        the lamp's single slot for an owner that no longer exists. Ceasing
        to watch does not by itself end it: Home Assistant stopping ends the
        watching and unloads nothing.
        """

    def run_lasting(
        self, work: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Start work that nothing which ends the coordinator ends; return it.

        A hang-up: one that outlives its coordinator gives the lamp's slot
        back, and one cut short leaves the client's bus open.
        """

    def watch(
        self, advertising: Callable[[bool], None], tick: Callable[[], None]
    ) -> bool:
        """Watch the air for the lamp, and the clock; say if the lamp is there now.

        From here on ``advertising(present)`` is called when the lamp is
        heard and when it is no longer, and ``tick()`` on the clock. Called
        once, by the start. The lamp may be reported heard before this
        returns - Home Assistant replays the last advertisement from inside
        the registration - and ``spawn`` works by then: a keeper has what it
        puts work on from when it is built.
        """

    def unwatch(self) -> None:
        """Cease to watch: neither ``advertising`` nor ``tick`` follows.

        More than once does no harm, nor does it on a keeper that never
        watched - a setup that failed before the start is stopped as well.
        """

    def stack_fault(self, count: int | None) -> None:
        """Hear of a Bluetooth stack that will not hang up, or that it has let go.

        ``count`` is how many requests in a row to disconnect the lamp went
        unanswered when the link called it a fault; ``None`` says that the
        lamp has answered since. What is made of it is the keeper's: Home
        Assistant's raises a repair and takes it down.
        """

    def lamp_described(
        self, model_id: str | None, firmware: str | None, serial: str | None
    ) -> None:
        """Hear what the lamp said it is: its model, firmware and serial number.

        Each as the device half has it, and ``None`` for what the lamp did
        not say; the model is the one an earlier session read when this one
        did not name it. Said once the device-info string has been read,
        which can be after the keeper has ceased to watch: an unload ends
        the exchange that reads it, and Home Assistant stopping does not.
        """

    def coordinates(self) -> tuple[float, float]:
        """Say where home is: its latitude and its longitude, in degrees.

        Zero and zero from a keeper that has no home - which is what Home
        Assistant holds when it was given none (see ``async_sync_location``).
        """


class GlowriumCoordinator:
    """Maintain a BLE connection and mirror the device's CBOR property state.

    Commands are CBOR maps written to ``WRITE_UUID`` (facebd01); the device
    reports its state as CBOR maps notified on ``NOTIFY_UUID`` (facebd02).
    """

    def __init__(  # noqa: PLR0913 - what a coordinator is built from, by name
        self,
        address: str,
        name: str,
        model_id: str | None = None,
        *,
        keeper: Keeper,
        dial: Dial,
        unclosed: Unclosed | None = None,
        bring_up: bool = True,
    ) -> None:
        """Initialize the coordinator for the device at ``address``.

        ``model_id`` is the model an earlier session read off the lamp, if
        one did. The entities are built before this session has read
        anything, and which presets a lamp has depends on its model.

        ``keeper`` is what keeps this coordinator (see ``Keeper``): Home
        Assistant for a config entry, tools/bench.py for a run against a
        real lamp. There is no coordinator without one, and so nothing here
        asks whether Home Assistant is there.

        ``dial`` is what a link is made through (see ``Dial``): for a config
        entry, the lamp as Home Assistant's Bluetooth finds it at each dial.

        ``unclosed`` is what coordinators for this lamp before this one could
        neither hang up nor close (see ``Unclosed``). The integration keeps one
        for each lamp and hands it to every coordinator it makes for it.

        ``bring_up`` is whether a lamp that says it is not activated is
        brought up (see ``_async_activate_if_needed``). It is, unless whoever
        builds the coordinator is only there to look at a lamp: the bench
        must not be able to provision somebody's lamp because a flag read
        back wrong.
        """
        self._keeper = keeper
        self._brings_up = bring_up
        self.address = address
        self.name = name
        # What the lamp has said and what was written to it (mirror.py); the
        # entities read it as ``state``. It describes the lamp through
        # _model_and_firmware when it warns - the lamp describes itself only
        # after its first frames - and dates the lamp's clock by the host's.
        self._mirror = self._new_mirror()
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
        # The properties under which the lamp has reported something that
        # cannot be used, and of which the log has said so (once each).
        self._unusable_said: set[int] = set()
        # The link to the lamp: the client, the lock around it, and how it is
        # let go of (link.py). It is handed the dial, and what it needs of
        # the keeper.
        self._link = Link(
            address,
            dial,
            notify_uuid=NOTIFY_UUID,
            heard=self._on_notify,
            greet=self._greet,
            probe=self._probe,
            reach_changed=self._async_reach_changed,
            stack_fault=keeper.stack_fault,
            spawn=self._spawn,
            run_lasting=keeper.run_lasting,
            unclosed=unclosed,
        )
        self._activation_checked = False
        # The batched state request is muted until this time after a run of
        # failures, rather than for the session - see _request_state.
        self._state_request_muted_until = 0.0
        self._state_request_failures = 0
        # Set once a cooldown has already been served and the model refused
        # again: that is a refusal rather than a run of bad luck.
        self._state_request_given_up = False
        # How many writes reached the characteristic: a count, not a value,
        # so that a write that raised still counts (see _write_on).
        self._writes_sent = 0

    @property
    def _state_request_muted(self) -> bool:
        """True while the batched state request is paused, or given up on."""
        return (
            self._state_request_given_up
            or monotonic() < self._state_request_muted_until
        )

    @property
    def state(self) -> Mapping[int, Any]:
        """What the lamp has said, by property id, and what was written to it.

        Read-only: a value gets in through a frame the lamp sent or the echo
        of a write it acknowledged (``Mirror``), and nothing is taken out.
        """
        return self._mirror

    @property
    def activated(self) -> bool | Unusable | None:
        """Return the device's activation flag (False = needs pairing/bring-up)."""
        return protocol.activated(self.state)

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
        """The lamp's name without its address after it: the log says both."""
        return identity.without_address(self.name, self.address)

    # --- What the link is handed ----------------------------------------------

    async def _greet(self, turn: Turn) -> None:
        """Make the first exchange on a link: what the lamp is asked and told.

        The state is asked for, and a lamp that answers nothing ends the
        exchange there: the link is told nothing, and lets go (what counts as
        an answer: ``_request_state``). Then whatever has to be written - the
        bring-up of a lamp that is not activated, a clock that has drifted -
        and only then is the link called one that works. The device info is
        read after that, and last: on BlueZ the link does not outlive a read.
        """
        if not await self._request_state(turn):
            return
        await self._async_activate_if_needed(turn)
        await self._async_sync_clock_if_needed(turn)
        turn.answered()
        await self._async_read_device_info(turn)

    async def _probe(self, turn: Turn) -> None:
        """Ask a link that has gone silent for the state, and say if it answered."""
        if await self._request_state(turn):
            turn.answered()

    @callback
    def _async_reach_changed(self) -> None:
        """Tell the entities: the link says there is something for them to learn."""
        self._async_notify_listeners()

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
    def operating_mode(self) -> str | Unusable | None:
        """Return the active mode, read from the lamp's two mode flags.

        None while it has not been reported, which is distinct from Manual:
        Manual takes both flags read as off, so a device we cannot reach yet
        does not look like it is in Manual (``protocol.operating_mode``).
        """
        return protocol.operating_mode(self.state)

    def mode_allows(self, mode: str) -> bool:
        """Return True if the device is in ``mode``, or its mode is not known.

        Keeps mode-specific entities available while the mode is unknown -
        not reported yet, or reported as something that cannot be used -
        instead of collapsing them to unavailable.
        """
        current = self.operating_mode
        return current is None or current is UNUSABLE or current == mode

    # What the platforms read: each property of the lamp by what it means. The
    # layouts and the rules for what a value is live in protocol.py, and every
    # one of these answers a value, None while the lamp has reported nothing
    # under the id, or ``protocol.UNUSABLE`` for something that is there and
    # is not what the id means. No platform looks under an id for itself.

    @property
    def power(self) -> bool | Unusable | None:
        """Whether the lamp is on."""
        return protocol.power(self.state)

    @property
    def brightness_percent(self) -> float | Unusable | None:
        """Brightness as a level from 0 to 100."""
        return protocol.brightness_percent(self.state)

    @property
    def indicator(self) -> bool | Unusable | None:
        """Whether the indicator light is on."""
        return protocol.indicator(self.state)

    @property
    def lighting_mode(self) -> int | Unusable | None:
        """The index of the lighting mode; which preset it is, the model says."""
        return protocol.lighting_mode(self.state)

    @property
    def latitude(self) -> float | Unusable | None:
        """The latitude the lamp works its circadian curve out from."""
        return protocol.latitude(self.state)

    @property
    def longitude(self) -> float | Unusable | None:
        """The longitude the lamp works its circadian curve out from."""
        return protocol.longitude(self.state)

    @property
    def ramp_minutes(self) -> int | Unusable | None:
        """Circadian ramp duration in minutes (0x2f)."""
        return protocol.ramp_minutes(self.state)

    @property
    def schedule_start(self) -> time | Unusable | None:
        """Schedule on-time from the 0x11 slot."""
        return protocol.schedule_start(self.state)

    @property
    def schedule_end(self) -> time | Unusable | None:
        """Schedule off-time from the 0x11 slot."""
        return protocol.schedule_end(self.state)

    @property
    def schedule_brightness(self) -> int | Unusable | None:
        """Schedule target brightness (%) from the 0x11 slot."""
        return protocol.schedule_brightness(self.state)

    @property
    def schedule_gradual_minutes(self) -> int | Unusable | None:
        """Schedule gradual-fade duration in minutes."""
        return protocol.schedule_gradual_minutes(self.state)

    @property
    def dst_enabled(self) -> bool | Unusable | None:
        """Whether daylight saving is on."""
        return protocol.dst_enabled(self.state)

    def diagnostics(self) -> dict[str, Any]:
        """Describe the lamp and the link, for a diagnostics download.

        What the lamp said about itself, what the mirror holds of what it
        reported, and where the connection stands - as it is, private or not.
        What of this may leave the host is for the caller to choose, which is
        the one that knows it is writing a file to be shared (see
        diagnostics.py).
        """
        link = self._link.diagnostics()
        return {
            "device": {
                "model": self.model.name,
                "model_id": self.model_id,
                "firmware": self.sw_version,
                "info": dict(self.device_info),
            },
            # The link's part and the device half's, in the order the block
            # has always been written in: the file is read by people.
            "link": {
                "available": self.available,
                "advertising": link["advertising"],
                "connected": link["connected"],
                "primed": link["primed"],
                "client": link["client"],
                "reports": self._mirror.reports,
                "writes_sent": self._writes_sent,
                "seconds_since_last_answer": link["seconds_since_last_answer"],
                "state_request_refusals": self._state_request_failures,
                "state_request_paused": self._state_request_muted,
                "unanswered_hang_ups": link["unanswered_hang_ups"],
                "dials_held_back": link["dials_held_back"],
                "clients_that_would_not_close": link["clients_that_would_not_close"],
            },
            "state": dict(self.state),
            "clock_heard_at": self._mirror.clock_heard_at,
            "not_kept": self._mirror.not_kept,
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
        self._link.log_reach(self._plain_name)
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

    async def async_start(self) -> None:
        """Watch for the device and keep it connected.

        Returns as soon as the keeper is watching. The first connect runs as
        background work rather than being awaited: setup awaits this method,
        and a lamp that is out of range would otherwise hold the entry open for
        the whole connect budget - long enough for a reload to land inside it,
        cancel the setup and leave the entry in ``setup_error``. Waiting buys
        nothing, because entity availability follows advertisement presence
        rather than the GATT link. The work is the keeper's to end with its
        watching (``Keeper.spawn``), so a half-finished connect cannot outlive
        us.
        """
        # What the keeper hears of the lamp, and its clock, go to the link as
        # they are: when to dial is the link's to decide.
        self._link.begin(self._keeper.watch(self._link.advertising, self._link.tick))
        # Absent from the start is worth saying too.
        self._link.log_reach(self._plain_name)
        self._spawn(self._link.initial_connect(), "initial connect")

    @callback
    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        """Have the keeper run ``coro``, for no longer than it keeps the coordinator."""
        self._keeper.spawn(coro, f"glowrium {what} {self.address}")

    @callback
    def _async_stop_watching(self) -> None:
        """Stop reacting to the lamp, and take no new link from here on."""
        # Said to the link before anything else: a command may be in flight
        # and outlast this, and from here on it is refused a new link.
        self._link.halt()
        self._keeper.unwatch()

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

    def _require[T](self, reading: T | Unusable | None, refusal: _Refusal) -> T:
        """Return ``reading`` when it is a value, or refuse what would build on it.

        The schedule slot and the lighting-mode frame are read-modify-write: one
        write carries several fields at once. Substituting a default to change a
        single field silently overwrites the rest with values the user never
        chose, so refuse instead and say why.

        There are two whys, and each has words of its own (``_Refusal``). A
        setting the lamp has not reported yet will come, and the user is told
        to wait. One it has reported as something that cannot be used will
        not, and writing that back is what this refuses: the user is told so.
        """
        if reading is None or reading is UNUSABLE:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    refusal.not_read if reading is None else refusal.unusable
                ),
                translation_placeholders={"name": self.name},
            )
        return reading

    async def _async_read_device_info(self, turn: Turn) -> None:
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
            raw = await turn.read(INFO_UUID)
        except LinkLostError as err:
            _LOGGER.debug("Device-info read from %s failed: %s", self.address, err)
            return
        self.device_info = _parse_device_info(raw)
        _LOGGER.debug(
            "%s: device info read; on BlueZ this link does not outlive a read",
            self.address,
        )
        # The entities described the device when they were built, which is
        # before anything was read: what is learned now is the keeper's to
        # put where it is shown, and to remember for the next start.
        self._keeper.lamp_described(self.model_id, self.sw_version, self.serial_number)

    async def _request_state(self, turn: Turn) -> bool:
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
            read_ok, carried = await self._async_read_state(turn)
            if not carried.issuperset(STATE_KEYS) and not self._state_request_muted:
                # Judged on what this read carried rather than on the mirror:
                # the mirror accumulates, so a key seen once would look
                # covered for the rest of the session.
                await self._async_ask_state(turn)
            return read_ok
        asked = await self._async_ask_state(turn)
        if asked is _Asked.REPORTED:
            return True
        read_ok, _ = await self._async_read_state(turn)
        return read_ok or asked in (_Asked.SILENT, _Asked.REFUSED)

    async def _async_read_state(self, turn: Turn) -> tuple[bool, frozenset[int]]:
        """Read the state map; return whether it answered and what it carried."""
        try:
            raw = await turn.read(NOTIFY_UUID)
        except LinkLostError as err:
            _LOGGER.debug("%s state read failed: %s", self.address, err)
            return False, frozenset()
        return True, self._ingest(raw)

    async def _async_ask_state(self, turn: Turn) -> _Asked:
        """Write the state request and wait for the lamp to report."""
        before = self._mirror.reports
        try:
            await turn.write(NOTIFY_UUID, bytes(STATE_KEYS))
        except RefusedError as err:
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
        except LinkLostError as err:
            _LOGGER.debug(
                "%s state request failed, but not by refusing: %s", self.address, err
            )
            # The link is going, and BlueZ normally says so within seconds.
            # Noted in case it never does (the link's _LOST_GRACE).
            turn.in_doubt()
            return _Asked.LOST
        self._state_request_failures = 0  # and the write was acknowledged
        try:
            asked = frozenset(STATE_KEYS)
            async with asyncio.timeout(_REPORT_TIMEOUT):
                while not self._mirror.reported_since(before) >= asked:
                    await self._mirror.next_report()
                return _Asked.REPORTED
        except TimeoutError:
            if self._mirror.reports > before:
                return _Asked.REPORTED  # in part; a read would add nothing asked for
        _LOGGER.debug(
            "%s acknowledged the state request and reported nothing", self.address
        )
        return _Asked.SILENT

    def _say_what_cannot_be_used(self, carried: frozenset[int]) -> None:
        """Say, once for each property, that what the lamp reported cannot be used.

        Such a property reads as unknown and no command builds on it, and
        nothing else would say why: the owner sees an entity that never fills
        in. Said for the property the first time a report carries it so - the
        lamp reports all day - with its name here and nothing of the value,
        which is the lamp's to choose, in a line that asks to be posted.
        """
        for key in sorted(carried & protocol.PROPERTIES.keys() - self._unusable_said):
            name, read = protocol.PROPERTIES[key]
            if read(self.state) is not UNUSABLE:
                continue
            if key == KEY_TIME and protocol.clock_is_no_date(self.state):
                # A clock that is wrong, which the first exchange sets right: it
                # is known what this is, and there is nothing to report.
                continue
            self._unusable_said.add(key)
            _LOGGER.warning(
                "%s (%s): what the lamp reported as its %s (0x%02x) cannot be used "
                "by this integration. It reads as unknown, and no command is built "
                "on it. Said once for this property. Please report this, with a "
                "diagnostics download from the integration's page",
                self.address,
                self._model_and_firmware(),
                name,
                key,
            )

    def _ingest(self, data: bytes) -> frozenset[int]:
        """Take a frame from the lamp into the mirror; return the ids it carried.

        The one intake, for the notify callback and for the read of the state
        alike. Two things are the coordinator's and not the mirror's: that the
        lamp answered is noted first, whatever the frame says - a garbage
        frame is still the lamp speaking - and the entities are told after,
        for a report only. What a frame is and what it carried is the
        mirror's (``Mirror.take``).
        """
        self._link.note_answer()  # whatever it says, the lamp said it
        carried = self._mirror.take(data)
        if not carried:
            return carried
        self._say_what_cannot_be_used(carried)
        # Seed the remembered ramp from the device the first time we see it, so
        # it survives an HA restart (the device persists its own ramp). Only a
        # ramp that is one: what is remembered is written back with every
        # lighting mode and after every switch to Circadian, and an empty one,
        # or five bytes, would latch and keep a later ramp from being taken.
        if self._desired_ramp is None:
            self._desired_ramp = usable(protocol.reported_ramp(self.state))
        self._async_notify_listeners()
        return carried

    @callback
    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        """Take a frame the lamp notified: what the link is handed to call."""
        self._ingest(bytes(data))

    async def _write_on(self, turn: Turn, payload: dict[int, Any]) -> None:
        """Write one command frame on the turn that is given.

        A command is written through here, on the turn the link gives it for
        each attempt (``_async_deliver``), and so is what the first exchange
        has to write: the bring-up, a clock that has drifted.
        """
        self._writes_sent += 1  # counted before, so a raising write still counts
        await turn.write(WRITE_UUID, cbor.encode(payload))
        # Optimistic local echo; the device also notifies its new state.
        self._mirror.echo(payload)

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
        """Hand a command to the link, and say in the user's words if it failed.

        The link delivers it: under its lock, on a link it dials if none is
        held, with one reconnect and inside one deadline (``Link.send``). What
        it is handed is the write, to make on the turn it gives, and the
        question it asks once if that write failed - has the lamp reported
        what the command set? (``_async_device_confirms``).

        Every failure is reported as a ``HomeAssistantError`` so the user gets
        a readable message rather than a stack trace after a long hang.
        """
        # Noted before the wait for the lock: a report that comes while the
        # command waits is as fresh as one that comes after it.
        reports_before = self._mirror.reports
        try:
            await self._link.send(
                lambda turn: self._write_on(turn, payload),
                vouch=lambda: self._async_device_confirms(payload, reports_before),
            )
        except NoNewLinkError as err:
            # What stopped it, when it was the link itself: "out of range,
            # try a proxy" is advice for the radio.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
                translation_placeholders={"name": self.name},
            ) from err
        except LinkLostError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"name": self.name},
            ) from err

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

        This waits for that report for as long as it is let: how long a
        failed command is worth waiting on is the link's to say (its
        ``_CONFIRM_TIMEOUT``), and it ends the wait there.
        """
        tracked = {key: value for key, value in payload.items() if key in STATE_KEYS}
        if not tracked:
            return False
        while True:
            if all(self.state.get(k) == v for k, v in tracked.items()) and (
                tracked.keys() & self._mirror.reported_since(reports_before)
            ):
                return True
            await self._mirror.next_report()

    async def _async_activate_if_needed(self, turn: Turn) -> None:
        """Bring the device up once if it reports as not yet activated (0x14).

        Part of the first exchange, after the state request: on a new link a
        background connect made, or on one a command took, when the poll comes
        to it. Never from a command itself, which takes its link without one.
        """
        if not self._brings_up or self._activation_checked:
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
            if KEY_ACTIVATED in self.state or not turn.up:
                break
            await asyncio.sleep(0.25)
        # On the flag itself, both ways. Only a plain no is a lamp that has to
        # be brought up, and only a plain yes settles the question for the
        # session: a number or a text there reads as true and says neither.
        if self.activated is False:
            await self._async_activate(turn)
        if self.activated is True:
            self._activation_checked = True

    async def _async_sync_clock_if_needed(self, turn: Turn) -> None:
        """Correct the device clock if what it reports has drifted.

        Runs on the priming path, where the clock has just been read. Silent
        when the lamp has not reported one: there is no drift to judge, and a
        blind write would be guessing at what it currently believes. Silent as
        well for a clock kept in a shape nobody has seen, where seven bytes
        written over it would be a guess of another kind.
        """
        reported = protocol.device_time(self.state)
        if reported is None:
            return
        if reported is UNUSABLE:
            if not protocol.clock_is_no_date(self.state):
                return
            # Seven bytes with no date in them: a clock, and a wrong one. A
            # nonsense date is itself a reason to correct it.
            _LOGGER.debug("%s reported an impossible clock; correcting", self.address)
        else:
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
        await self._write_on(turn, self._clock_command())

    def _clock_command(self) -> dict[int, Any]:
        """Return the command that sets the lamp's clock to now, local time."""
        return protocol.clock_command(dt_util.now())

    def _new_mirror(self) -> Mirror:
        """Build an empty mirror of this lamp, told what the integration knows."""
        return Mirror(
            self.address,
            known=KNOWN_KEYS,
            described=self._model_and_firmware,
            now=self._now,
        )

    @staticmethod
    def _now() -> datetime:
        """Return the host's clock, looked up when asked.

        Handed to the mirror for dating the lamp's clock: looked up at the
        call and not bound at start, since the tests patch ``dt_util.now``.
        """
        return dt_util.now()

    async def _async_activate(self, turn: Turn) -> None:
        """Bring up a factory-reset device: clock + flags + enable light output.

        Replays the vendor app's first-pairing sequence - all local, no cloud and
        no BLE bond - so the light works without the app. The device gates its
        light output on 0x14; a virgin (factory-reset) device reports 0x14 False
        and its front-panel LEDs blink until this runs. Idempotent when already on.

        Three writes on the turn of the first exchange. The one caller is
        ``_async_activate_if_needed``.
        """
        await self._write_on(turn, {KEY_ACTIVATE_MISC: ACTIVATE_MISC_VALUE})
        await self._write_on(turn, self._clock_command())
        await self._write_on(turn, {KEY_ACTIVATED: True})
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
            else self._require(self.lighting_mode, _NO_LIGHTING_MODE)
        )
        # Ramp keeps its default. Setting a mode rewrites ramp on the device
        # regardless, so there is no "leave it alone" option here and falling
        # back is deliberate. The mode above is different: defaulting it there
        # silently *changes* a setting the caller never asked to touch.
        #
        # What the lamp reported goes back only as the ramp remembered, and
        # that is remembered only when it is a ramp (see _ingest): whatever
        # stood under the id used to go back, five bytes of it or a text.
        ramp_value = ramp if ramp is not None else self._desired_ramp or RAMP_DEFAULT
        return {
            KEY_LIGHTING_MODE: mode_value,
            KEY_MODE_PARAM_2C: MODE_PARAM_2C,
            KEY_RAMP: ramp_value,
            KEY_MODE_PARAM_32: MODE_PARAM_32,
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
        """Push the home's coordinates; the device recomputes its circadian curve.

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

        A keeper with no home (``tools/bench.py``) answers zero and zero, and
        is refused the same way.
        """
        lat, lon = self._keeper.coordinates()
        if lat == 0 and lon == 0:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="home_location_not_set",
                translation_placeholders={"name": self.name},
            )
        await self._async_write({KEY_LATITUDE: float(lat), KEY_LONGITUDE: float(lon)})

    async def async_set_timer_start(self, hour: int, minute: int) -> None:
        """Set the schedule start time."""
        slot = self._require(
            protocol.with_schedule_start(self.state, hour, minute), _NO_SCHEDULE
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_end(self, hour: int, minute: int) -> None:
        """Set the schedule end time."""
        slot = self._require(
            protocol.with_schedule_end(self.state, hour, minute), _NO_SCHEDULE
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_brightness(self, value: int) -> None:
        """Set the schedule brightness (0..100)."""
        slot = self._require(
            protocol.with_schedule_brightness(self.state, value), _NO_SCHEDULE
        )
        await self._async_write({KEY_TIMER: slot})

    async def async_set_timer_gradual(self, minutes: int) -> None:
        """Set the schedule gradual on/off fade duration in minutes."""
        slot = self._require(
            protocol.with_schedule_gradual(self.state, minutes), _NO_SCHEDULE
        )
        await self._async_write({KEY_TIMER: slot})
