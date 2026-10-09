"""The link to one lamp: taken, held, asked and let go of.

Splitting the coordinator in two (#21). What is here was the coordinator's:
the client and the lock around it, the hang-up and the closing of the
client's bus, the Bluetooth stack that will not hang up, whether the lamp is
in reach - and when a link is dialled, when the first exchange is made on it
and when one that has gone silent is asked whether it is still there. Nothing
here knows Home Assistant or the lamp's protocol; what it needs of either it
is handed.

What is said is the coordinator's, and it says it on a ``Turn``: in the
first exchange and to a silent link, which are two callables it hands in,
and in a command, which it hands to ``Link.send``. Some of the link's state
is still public: the coordinator keeps the names it always had for it, for
the tests and the bench that have not moved yet. What it imports from here
by an underscored name keeps the name it had when it was the coordinator's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
import contextlib
from datetime import timedelta
from enum import Enum, auto
import logging
from time import monotonic
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError, BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

# Under the coordinator's name, as these lines always were: a log filter that
# somebody set on it must not go half-blind because the code moved file.
_LOGGER = logging.getLogger(f"{__package__}.coordinator")
_RECONNECT_INTERVAL = timedelta(seconds=30)
# bleak-retry-connector defaults to 4 connect attempts, each of which can sit
# through a 20 s bleak timeout plus a backoff. Against an unreachable device
# that adds up to minutes while the link's lock is held, so a queued command
# cannot even start. It is the ceiling on a connect (_CONNECT_TIMEOUT) that
# bounds a dial; the attempts are for the ones that fail fast. On a weak link
# a connect is often made and lost within a second or two, and the next try
# inside the same dial is what gets through.
_CONNECT_ATTEMPTS = 3
# Ceiling on each thing unload waits for - the lock, then the hang-up - so
# reloading the integration does not wait out whatever connect currently holds
# the lock, or a link that is slow to close. There it bounds the waits, not the
# hang-up. It is also the ceiling on a hang-up itself once Home Assistant is
# stopping (see ``Link.shut_down``).
_STOP_TIMEOUT = 3.0
# Ceiling on hanging up a client the link is finished with (see
# ``Link.hang_up``).
# It runs in the background, so nothing waits this out except a write retry
# and an unload, each under a deadline of its own - and Home Assistant when it
# is stopping, which is why a hang-up then gets _STOP_TIMEOUT instead. It
# matches how long bleak itself waits for BlueZ to confirm a disconnect, and it
# has to stay below the ceiling on a command (_COMMAND_TIMEOUT), or a link that
# will not confirm it has closed leaves the retry no time to dial.
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
_WRITE_ATTEMPTS = 2  # the initial write plus one reconnect-and-retry
# Ceiling on getting one user-facing command out, so a button reports a clear
# failure instead of appearing to hang while the retries stack up. It has to
# outlast a background connect (see _CONNECT_TIMEOUT). A failed command may
# then spend up to _CONFIRM_TIMEOUT more deciding whether it failed after all,
# so the worst a user waits is the sum of the two.
_COMMAND_TIMEOUT = 25.0
# How long a failed command waits for the device to report the state it asked
# for before the failure is believed. A write-with-response on a marginal link
# can reach the lamp and be acted on while the acknowledgement is lost, which
# bleak reports as failure. Observed once on a G7 at RSSI -88: the confirming
# notification arrived 22-32 ms BEFORE the error was raised, so this is grace
# for a slower link rather than a wait anyone should routinely pay.
_CONFIRM_TIMEOUT = 2.0
# Ceiling on a background connect: the wait for the lock, the wait for a
# hang-up still under way (see Link.open), the dial, the subscription and the
# first exchange. Without it a connect to an unreachable device holds the lock
# indefinitely, and everything else that needs the lock waits behind it with
# no deadline of its own.
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
# Ceiling on asking a link that is already held for its state - the first
# exchange on one a command made, the question for one that has gone silent -
# including the wait for the lock. There is no dial in it, so it need not be
# as long as a connect, and a probe that is slow to give its verdict keeps a
# dead link held meanwhile.
_ASK_TIMEOUT = 10.0
# How long BlueZ gets to report a link dropped once it has called it "not
# connected". Normally two to three seconds (see _REFUSAL_MARKERS). When the
# report never comes, the client is held with is_connected True and nothing
# dials again: on the real host that lasted five hours, until a command.
_LOST_GRACE = 10.0
# How long a held link may stay silent before it is asked whether it is still
# there. The lamp only speaks when something changes, so silence is normal -
# and it is also all a link gives off that died without BlueZ noticing.
_PROBE_INTERVAL = 300.0
# What a lost link looks like from here. Besides its own errors and timeouts,
# bleak passes on whatever the bus raised. When the lamp drops the link the
# disconnected callback hangs the client up at once, which closes its D-Bus
# connection, and a call still waiting for its reply on that connection gets
# EOFError - or, with the socket gone, "Bad file descriptor" - rather than a
# BleakError. To whoever made the call all of these say the same thing: this
# link is gone. (TimeoutError is an OSError and is named only so that it can be read.)
_LINK_ERRORS = (BleakError, TimeoutError, EOFError, OSError)

# What a link is made through. It is given the callback for a link that
# is lost, and returns a connected client or raises one of ``_LINK_ERRORS``.
# Handed in, so that the link can be made to stand on something other than a
# Bluetooth adapter: the bench's own scan, and in the tests a scripted lamp.
type Dial = Callable[
    [Callable[[BleakClientWithServiceCache], None]],
    Awaitable[BleakClientWithServiceCache],
]

# What the device half says on a link. It is given a turn, and what it does
# with it is its own: the link decides when the lamp is spoken to - the first
# exchange on a new link, the question for one that has gone silent - and
# hears the verdict (``Turn.answered``).
type Talk = Callable[[Turn], Awaitable[None]]


def dial_by_bluetooth(
    find: Callable[[], BLEDevice | None], address: str, name: str
) -> Dial:
    """Return the dial that connects to whatever ``find`` finds.

    The integration's own, when it is handed none: ``find`` then asks Home
    Assistant's scanners for the lamp. A lamp they do not have is "not in
    range", said before anything is dialled.
    """

    async def _dial(
        lost: Callable[[BleakClientWithServiceCache], None],
    ) -> BleakClientWithServiceCache:
        device = find()
        if device is None:
            raise BleakError(f"{address} is not in range")
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            name,
            disconnected_callback=lost,
            max_attempts=_CONNECT_ATTEMPTS,
        )

    return _dial


@contextlib.contextmanager
def _gatt_call(client: BleakClientWithServiceCache) -> Iterator[None]:
    """Make one GATT call on ``client``; hung up under it, it ends as a lost link.

    BlueZ turns a read or a write away with "in progress" while an earlier
    call on the same characteristic is still waiting - one that a deadline
    abandoned, on a link that is going. bleak answers by sleeping ten
    milliseconds and trying again, for as long as it takes, and begins every
    try by asserting that it still has its bus. A link reported lost during
    that pause is hung up at once, which closes the bus (see
    ``Link.hang_up``),
    and the try that follows ends on the assertion: an ``AssertionError``,
    which says nothing of a link and is not among ``_LINK_ERRORS``. Seen on
    the G7's host four times in thirty hours (2026-10-07), as "Task exception
    was never retrieved" with a traceback, a few milliseconds after a drop.

    Only on a client that no longer says it is connected, and so not by
    adding the assertion to ``_LINK_ERRORS``: on a link that is up it means
    somebody was wrong, and has to be seen. Every GATT call goes through
    here - the assertion comes out of whichever was waiting.
    """
    try:
        yield
    except AssertionError as err:
        if client.is_connected:
            raise
        raise BleakError(
            "the client was hung up while a call on it was waiting"
        ) from err


class _Bus(Enum):
    """What was behind a client that had been told to disconnect."""

    CLEAR = auto()  # no bus open: closed by bleak, already down, or none at all
    CLOSED = auto()  # one still open, and closed here
    OPEN = auto()  # one that may still be open and could not be closed


def _is_bluez(backend: Any) -> bool:
    """Return True if ``backend`` is bleak's own BlueZ client.

    Told by where its class lives, not by what it holds: another backend may
    keep something of its own under a name bleak also uses.
    """
    return backend is not None and "bluezdbus" in type(backend).__module__


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
        # No backend on record: there may be a bus, and nothing to reach it by.
        return _Bus.OPEN
    if not _is_bluez(backend):
        # A Bluetooth proxy's client has no bus of its own, and nothing here
        # to close - whatever it may hold.
        return _Bus.CLEAR
    if not hasattr(backend, "_bus"):
        # BlueZ's client without one is bleak rearranged.
        return _Bus.OPEN
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
    if was_up:
        _LOGGER.debug("%s: closed the bus a hang-up left open", address)
    return _Bus.CLOSED if was_up else _Bus.CLEAR


def _reason(err: BaseException) -> str:
    """Return what ``err`` says, or what it is where it says nothing.

    For a line of the log. A deadline that ran out and a bus closed under a
    call - ``TimeoutError`` and ``EOFError`` - carry no text, and they are the
    two commonest ways for an exchange with a lamp at the edge of range to
    fail: one night on the G7's host left 290 lines that ended in "failed: "
    with nothing after it (2026-10-08).
    """
    return str(err) or repr(err)


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


class LinkLostError(Exception):
    """What the device half is told when a link is gone, or could not be had.

    It says what the Bluetooth library said, or what that was where it said
    nothing (see ``_reason``) - as words. The error itself is the link's:
    which of the library's a lost link can raise (``_LINK_ERRORS``) is known
    here and nowhere else.
    """


class NoNewLinkError(LinkLostError):
    """The link's own "no" to a new client: nothing the radio did.

    It has been stopped, or the last client it let go of could not be closed
    and nothing is dialled over that. A lost link like any other to every
    background path, which goes on treating it as a link that could not be
    had. A command asks which of the two it was (``translation_key``) and
    says so, instead of telling its user that the lamp may be out of range
    and a Bluetooth proxy would help - and it does not try a second time,
    because neither changes within a retry.
    """

    def __init__(self, message: str, translation_key: str) -> None:
        """Keep the key of the message a command shows for this."""
        super().__init__(message)
        self.translation_key = translation_key


class RefusedError(Exception):
    """What a write raises when the lamp said no, and the link stands.

    An ATT refusal: a G8 answers the state request with ``Insufficient
    authorization``. Told from a lost link by ``_looks_like_a_refusal``, once
    and here.
    """


# What ends one of the link's own flows as "this link is gone": what the
# library raises, and what a turn has made of that by the time the device
# half's part of the flow lets it through.
_LOST = (*_LINK_ERRORS, LinkLostError, RefusedError)


class Turn:
    """One go at the lamp, on a client the link holds.

    What the device half is given in place of the client: it writes a frame,
    reads a characteristic, and says what it made of the answer. Every call
    is made under the guard that turns a closed bus into a lost link (see
    ``_gatt_call``), and comes back as one of two things when it fails.
    """

    def __init__(
        self,
        link: Link,
        client: BleakClientWithServiceCache,
        answered: Callable[[], None] | None = None,
    ) -> None:
        """Take the link, the client this turn is on, and what an answer sets off."""
        self._link = link
        self._client = client
        self._on_answered = answered
        # Whether the device half has called the lamp answering (see answered).
        self.got_an_answer = False

    @property
    def up(self) -> bool:
        """Whether the link still holds the client this turn is on, connected.

        For an exchange that waits for the lamp to say something: there is
        nothing left to wait for on a link that has gone.
        """
        return self._link.client is self._client and self._client.is_connected

    async def write(self, uuid: str, frame: bytes) -> None:
        """Write ``frame`` and wait for the lamp to acknowledge it.

        An acknowledged write is an answer, and is noted as one: the link is
        alive, and the stack with it.
        """
        try:
            with _gatt_call(self._client):
                await self._client.write_gatt_char(uuid, frame, response=True)
        except _LINK_ERRORS as err:
            if _looks_like_a_refusal(err):
                raise RefusedError(_reason(err)) from err
            raise LinkLostError(_reason(err)) from err
        self._link.note_answer()

    async def read(self, uuid: str) -> bytes:
        """Read a characteristic.

        On BlueZ a read of this lamp ends the link two seconds later (see the
        coordinator's ``_request_state``); what is read here is read knowing
        that. A read that fails is a lost link, whatever it failed with.
        """
        try:
            with _gatt_call(self._client):
                return bytes(await self._client.read_gatt_char(uuid))
        except _LINK_ERRORS as err:
            raise LinkLostError(_reason(err)) from err

    def in_doubt(self) -> None:
        """Note that this link failed a call and was not reported lost.

        The link is going, and BlueZ normally says so within seconds. Noted
        in case it never does (``_LOST_GRACE``).
        """
        self._link.lost = (self._client, monotonic())

    def answered(self) -> None:
        """Say that the lamp answered what it was asked: this link works.

        The device half's verdict on the exchange it was given the turn for,
        as distinct from the proof of life the link takes for itself from
        every acknowledged write. An exchange that ends without it was held
        on a link that answers nothing, and the link lets go of it. Said
        before anything is read: on BlueZ a read ends this lamp's link, and
        nothing that comes after un-says this.
        """
        self.got_an_answer = True
        if self._on_answered is not None:
            self._on_answered()


class Unclosed:
    """What would neither hang up nor have its bus closed, kept for one lamp.

    A client lands here when its link was let go of, BlueZ would not hang
    up, and the bus behind it could not be closed either (see
    ``Link._async_disconnect``). While there is one, nothing is dialled. It
    used to be kept by the coordinator, and a reload of the entry made a new
    coordinator that knew nothing of it and dialled - a second bus beside the
    one nothing could close. So there is one of these for each lamp, for as
    long as the process runs, and every link to that lamp is handed it.

    A client is kept with what is behind it. That is what its bus is closed
    through, and by the time the next link tries, Home Assistant's wrapper
    may have forgotten it.

    The hang-ups still under way are here too, for the same reason: the
    coordinator a reload makes must wait for its predecessor's before it
    dials, or it dials over a client that is kept a few seconds later.
    """

    def __init__(self) -> None:
        """Start with nothing kept."""
        self.clients: set[BleakClientWithServiceCache] = set()
        self.backends: dict[BleakClientWithServiceCache, Any] = {}
        # The hang-ups still under way. Until one has ended nobody knows
        # whether its client will close, so a dial waits for them (see
        # ``Link.open``) - the next link's as well as the one that began them.
        self.hang_ups: set[asyncio.Task[None]] = set()

    def hanging_up(self, hang_up: asyncio.Task[None]) -> None:
        """Note ``hang_up`` for as long as it runs."""
        self.hang_ups.add(hang_up)
        hang_up.add_done_callback(self.hang_ups.discard)

    def under_way(self) -> tuple[asyncio.Task[None], ...]:
        """Return the hang-ups that have not ended.

        Not the ones that have, though they may still be noted for a turn of
        the loop: a command's second try has just waited for its own hang-up
        and has nothing left to wait for.
        """
        return tuple(one for one in self.hang_ups if not one.done())

    def keep(self, client: BleakClientWithServiceCache, backend: Any) -> None:
        """Keep ``client``, with what is behind it."""
        self.clients.add(client)
        self.backends[client] = backend

    def forget(self, client: BleakClientWithServiceCache) -> None:
        """Forget ``client``: it has been let go of."""
        self.clients.discard(client)
        self.backends.pop(client, None)


class Link:
    """One lamp's link: who holds the client, and how it is let go of."""

    def __init__(  # noqa: PLR0913 - what a link is handed, each by its name
        self,
        address: str,
        dial: Dial,
        *,
        notify_uuid: str,
        heard: Callable[[Any, bytearray], None],
        greet: Talk,
        probe: Talk,
        reach_changed: Callable[[], None],
        stack_fault: Callable[[int | None], None],
        spawn: Callable[[Coroutine[Any, Any, None], str], None],
        run_lasting: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        unclosed: Unclosed | None = None,
    ) -> None:
        """Take what the link is handed; it starts with no client and unlocked.

        ``dial`` makes a link (see ``Dial``). ``notify_uuid`` is the
        characteristic the lamp reports on, and ``heard`` is given each frame
        it sends. ``greet`` is the first exchange on a new link and ``probe``
        the question for one that has gone silent: each is given a turn, and
        says on it whether the lamp answered (see ``Talk``). ``reach_changed``
        is called wherever the entities have something to learn of the link:
        a client was taken, it was let go of because of what it did, the
        first exchange was made on it, the lamp began or stopped advertising.
        ``stack_fault`` is given
        the count of hang-ups left unanswered when a run of them is called a
        fault, and None when the lamp answers again. ``spawn`` runs the
        background work - a connect, a first exchange, a probe - for no longer
        than whoever watches the lamp does; ``run_lasting`` runs a hang-up
        where nothing that ends the caller can end it. ``unclosed`` is what
        links to this lamp before this one could not let go of (see
        ``Unclosed``); left out, the link keeps its own, as the bench's does.
        """
        self.address = address
        self._dial = dial
        self._notify_uuid = notify_uuid
        self._heard = heard
        self._greet = greet
        self._probe = probe
        self._reach_changed = reach_changed
        self._stack_fault = stack_fault
        self._spawn = spawn
        self._run_lasting = run_lasting
        self.client: BleakClientWithServiceCache | None = None
        self.lock = asyncio.Lock()
        # What sits behind each client, noted when the client is taken: by
        # the time it has to be closed, Home Assistant's wrapper may have
        # forgotten its backend (see _close_bus).
        self.backends: dict[BleakClientWithServiceCache, Any] = {}
        # Clients that would not hang up and whose bus could not be closed
        # either. While there is one, nothing is dialled (see open). The set
        # is the lamp's, not this link's: the next link finds them in it.
        self._kept = unclosed or Unclosed()
        self.unclosed = self._kept.clients
        # Hang-ups in a row that BlueZ left unanswered (see _STACK_FAULT_AFTER),
        # and the moment before which the poll does not dial because of them.
        self.stuck_hang_ups = 0
        self.dial_not_before = 0.0
        # Whether this link has announced such a run - in the log and as a
        # repair - and so has an episode to call over (see note_answer).
        self.fault_announced = False
        # The client whose link BlueZ called "not connected" without reporting
        # it dropped, and when (_LOST_GRACE).
        self.lost: tuple[BleakClientWithServiceCache, float] | None = None
        # When the lamp last answered anything (_PROBE_INTERVAL).
        self.last_answer = monotonic()
        # Whether a background connect is on its way: one at a time, however
        # often the lamp advertises and the tick comes round.
        self.reconnecting = False
        # The client the first exchange has been made on. A command takes a
        # link without one (see send), so this is how the tick notices a link
        # whose lamp was never asked for its state.
        self.primed: BleakClientWithServiceCache | None = None
        self.present = False
        # What the log last said about the lamp being in reach (see
        # log_reach). Starts as "in reach", so a lamp that is absent from the
        # first moment is said to be.
        self._logged_in_reach = True
        # Set when the coordinator stops and never cleared: a stopped link
        # takes no new client (see open). Nothing would ever let go of it.
        self.stopped = False
        # Set when Home Assistant itself is stopping (see shut_down).
        self._shutting_down = False

    @property
    def connected(self) -> bool:
        """Return True while a live GATT connection is held."""
        return self.client is not None and self.client.is_connected

    @property
    def in_reach(self) -> bool:
        """Return True while the lamp is advertising, or a client is held.

        What the entities' availability goes by (the coordinator's ``available``
        says why it is not the link alone).
        """
        return self.connected or self.present

    def diagnostics(self) -> dict[str, Any]:
        """Describe where the link stands, for a diagnostics download.

        The link's part of the block the coordinator hands on (its
        ``diagnostics``): nothing here is the lamp's own word, and of the
        client only what kind it is.
        """
        client = self.client
        backend = self.backends.get(client) if client is not None else None
        return {
            "advertising": self.present,
            "connected": self.connected,
            "primed": client is not None and client is self.primed,
            "client": type(backend).__name__ if backend is not None else None,
            "seconds_since_last_answer": round(monotonic() - self.last_answer),
            "unanswered_hang_ups": self.stuck_hang_ups,
            "dials_held_back": monotonic() < self.dial_not_before,
            "clients_that_would_not_close": len(self.unclosed),
        }

    def log_reach(self, name: str) -> None:
        """Say once when the lamp goes out of reach, and once when it is back.

        The entities go unavailable then, and without this nothing says why or
        since when. Judged by what the entities are judged by - an
        advertisement or a link - so a lamp that is quiet while connected is
        not reported as gone. Checked wherever the listeners are told, which
        is everywhere either of the two changes while the lamp is watched: a
        coordinator that is stopping lets go of its link and says nothing.
        """
        if self.stopped:
            # It let go of its link because it was told to and no longer hears
            # advertisements: where the lamp is, it cannot say. A command that
            # still arrives tells the listeners, and comes through here.
            return
        in_reach = self.in_reach
        if in_reach == self._logged_in_reach:
            return
        self._logged_in_reach = in_reach
        if in_reach:
            _LOGGER.info("%s (%s) is back in reach", name, self.address)
        else:
            _LOGGER.info(
                "%s (%s) is out of reach: it is not advertising and there is no "
                "link to it. Its entities are unavailable until it is heard again",
                name,
                self.address,
            )

    async def open(self) -> BleakClientWithServiceCache:
        """Dial, subscribe, and only then keep the client; return it.

        The caller holds ``lock`` and has seen that no client is held: a
        background connect (``connect_locked``) or a command (``send``),
        neither of which can race the other for that. What is done on the
        new link - the first exchange, or a command's own write and nothing
        else - comes after, and is not this method's business.
        """
        self._refuse_what_must_not_be_dialled()
        under_way = self._kept.under_way()
        if under_way:
            # A client is kept only once its hang-up has ended; until then
            # nothing says that it will not close. A dial made in
            # between went ahead beside it: one more client on a stack that
            # was not letting go of the first - within one coordinator, and
            # across a reload, where the next one dialled as its predecessor's
            # hang-up was still running. So the dial waits. On a stack that
            # hangs up that is the time a disconnect takes; the caller's own
            # deadline bounds it, and ending the wait ends no hang-up.
            await asyncio.wait(under_way)
            self._refuse_what_must_not_be_dialled()
        client = await self._dial(self.on_lost)
        self.backends[client] = getattr(client, "_backend", None)
        try:
            with _gatt_call(client):
                await client.start_notify(self._notify_uuid, self._heard)
            if self.stopped:
                # Stopped while this connect was on its way. Keeping the
                # link would hand it to a coordinator nobody will stop
                # again, and the lamp has one slot.
                raise NoNewLinkError(  # noqa: TRY301
                    f"{self.address}: stopped while connecting", "not_running"
                )
        except BaseException as err:
            # Including cancellation by a deadline. Nothing references this
            # client yet, and bleak does not hang up on garbage collection, so
            # walking away here would leave the lamp's only slot taken.
            hang_up = self.hang_up(client)
            if isinstance(err, Exception):
                # A failure rather than a cancellation, so the caller may dial
                # again at once - and must not be handed the link that is
                # being closed (see send).
                # Shielded: a deadline ends the wait, not the hang-up. A
                # cancellation is not kept waiting at all: its deadline has
                # already run out, and the lock is held here.
                await asyncio.shield(hang_up)
            raise
        # Committed only once notifications are live: a client without them
        # reports as connected forever while no state ever arrives again.
        self.client = client
        # Taken, and so told: a link is half of what the lamp's reach goes by,
        # and what is done on it next can take seconds, or never end.
        self._reach_changed()
        return client

    async def send(self, say: Talk, *, vouch: Callable[[], Awaitable[bool]]) -> None:
        """Deliver a command: under the lock, on a link, with one reconnect.

        ``say`` is the command, said on the turn it is given - once, or once
        more on a new link if the first was lost under it. It runs inside
        ``lock`` so it cannot race a background connect.

        The whole attempt - including the wait for ``lock``, which a
        background connect may be holding - is capped by
        ``_COMMAND_TIMEOUT``. A command that could not be delivered ends as
        a ``LinkLostError``, or as the link's own "no" where it would not
        dial (``NoNewLinkError``): one thing for the caller to tell the
        user, rather than a stack trace after a long hang.

        ``vouch`` is asked once, when the command was put to a link and the
        write failed all the same: has the lamp done what it was told? On a
        weak link the acknowledgement is what goes missing. It is given
        ``_CONFIRM_TIMEOUT`` to say yes, and a command it vouches for is
        delivered.
        """
        # Whether the command got as far as a link. One that never did has
        # nothing to be vouched for.
        said = False
        # The client the last attempt failed on, if it got as far as having one.
        failed: BleakClientWithServiceCache | None = None
        # The client a write is being waited on, for as long as it is.
        writing_to: BleakClientWithServiceCache | None = None
        try:
            async with asyncio.timeout(_COMMAND_TIMEOUT), self.lock:
                for attempt in range(1, _WRITE_ATTEMPTS + 1):
                    try:
                        # The link and its own write, nothing else. The first
                        # exchange is expensive: the state request and the
                        # wait for its answer, up to 3 s waiting for the
                        # activation flag and, the first time, the device-info
                        # read, all before the write is even attempted and all
                        # inside the command's budget. On a lamp where the
                        # connect alone is marginal, that is what turns a
                        # working command into a reported failure. The tick
                        # makes it afterwards (see tick).
                        held = self.client
                        writing_to = (
                            held
                            if held is not None and held.is_connected
                            else await self.open()
                        )
                        said = True
                        await say(Turn(self, writing_to))
                        writing_to = None
                        break
                    except _LOST as err:
                        writing_to = None
                        client, self.client = self.client, None
                        if attempt == _WRITE_ATTEMPTS or isinstance(
                            err, NoNewLinkError
                        ):
                            # The last attempt - or the link's own "no",
                            # which a second attempt would only be given
                            # again.
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
                            await asyncio.shield(self.hang_up(client))
        except _LOST as err:
            if writing_to is not None and writing_to is self.client:
                # The deadline ran out inside the write. The handler above
                # never saw it - a deadline arrives as a cancellation - so the
                # link is still held, and a link that has kept a write waiting
                # this long is not one to hand the next command. Unless it has
                # been let go of meanwhile: then it is no longer ours to take.
                failed, self.client = writing_to, None
            try:
                if said and await self._vouched(vouch):
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
                    if isinstance(err, LinkLostError):
                        # Already what this ends as: a turn that lost its
                        # link, or the link's own "no", which says which of
                        # its reasons it was - the caller has words for each.
                        raise
                    raise LinkLostError(_reason(err)) from err
            finally:
                # Only now. The link let go of this client when the write
                # failed, but its notifications are the channel the vouching
                # above listens on, so it had to stay up until the device had
                # its chance to answer. Hung up, and told like any other link
                # that is let go of: whoever handed the command over says how
                # it ended, but that the lamp may be out of reach now is said
                # here, whoever that was.
                if failed is not None:
                    self._drop(failed)

    async def _vouched(self, vouch: Callable[[], Awaitable[bool]]) -> bool:
        """Ask whether the lamp did what a failed write told it, and not for long."""
        try:
            async with asyncio.timeout(_CONFIRM_TIMEOUT):
                return await vouch()
        except TimeoutError:
            return False

    async def connect(self) -> None:
        """Connect if not already connected, under a bounded wait for the lock.

        Every background connect - setup, the tick and an advertisement -
        funnels through here, so the ceiling applies to all of them. It has
        to cover the wait for ``lock`` too: the starvation that hung setup
        was one holder grinding through connect attempts to an unreachable
        lamp while another waited on the lock with no deadline.
        """
        if self.connected:
            return
        async with asyncio.timeout(_CONNECT_TIMEOUT), self.lock:
            await self.connect_locked()

    async def connect_locked(self) -> None:
        """Establish the GATT link, and make the first exchange on it.

        The caller must hold ``lock``: that is ``connect``. A command takes
        its link under the same lock (``send``), so that neither can race the
        other - a connect that waited for the lock while a command dialled
        finds the link held, and dials nothing.
        """
        if self.connected:
            return
        client = await self.open()
        if await self._greet_on(client, "%s: connected and primed"):
            self._reach_changed()

    async def _greet_on(self, client: BleakClientWithServiceCache, line: str) -> bool:
        """Make the first exchange on ``client``; return whether the lamp answered.

        What is said in it is the device half's (``greet``). ``line`` is
        what the log says once the device half has called the lamp
        answering: from then on ``client`` is one the first exchange has been
        made on, whatever comes after - and what comes after is the read that
        the link does not outlive on BlueZ.

        An exchange that raises - a write lost half-way, the caller's
        deadline - leaves the link held and not primed, and the next tick
        takes it from there.
        """

        def _answered() -> None:
            self.primed = client
            _LOGGER.debug(line, self.address)

        turn = Turn(self, client, _answered)
        await self._greet(turn)
        if turn.got_an_answer:
            return True
        # Established, but it answers nothing, however connected it claims to
        # be: bleak can hand back a client that reports itself connected while
        # every call on it answers "not connected". Dropped here rather than
        # held until the tick comes round. The tick rebuilds it either way,
        # and in the meantime ``connected`` would claim a link a command would
        # write into before failing - and on a link a command made, nothing
        # would reconnect or greet again at all, leaving the entities frozen
        # for the whole life of a link that never worked.
        _LOGGER.debug("%s: link answers nothing, dropping", self.address)
        self._drop(client)
        return False

    async def prime_held(self) -> None:
        """Make the first exchange on a link that was established by a command."""
        try:
            async with asyncio.timeout(_ASK_TIMEOUT), self.lock:
                client = self.client
                if client is None or client is self.primed:
                    return
                if not await self._greet_on(client, "%s: primed a link a command made"):
                    # Told where it was let go of: this return skips the else
                    # at the end.
                    return
        except _LOST as err:
            _LOGGER.debug("Priming state of %s failed: %s", self.address, _reason(err))
        else:
            self._reach_changed()

    async def probe_held(self) -> None:
        """Ask a link that has been silent whether it is still there.

        With the device half's question (``probe``): the lamp is asked for
        its state, which costs one write, proves the link if it answers, and
        refreshes the mirror for free. A link that does not answer - or keeps
        the question waiting until the deadline - is dropped, and the tick
        rebuilds it.
        """
        client: BleakClientWithServiceCache | None = None
        alive = False
        try:
            async with asyncio.timeout(_ASK_TIMEOUT), self.lock:
                held = self.client
                if held is None or (monotonic() - self.last_answer < _PROBE_INTERVAL):
                    return
                client = held
                turn = Turn(self, client)
                await self._probe(turn)
                alive = turn.got_an_answer
        except _LOST as err:
            _LOGGER.debug(
                "Probing the link to %s failed: %s", self.address, _reason(err)
            )
        if client is None:
            return  # never got as far as asking; nothing was learnt
        if alive:
            _LOGGER.debug("%s: the held link answers", self.address)
            return
        _LOGGER.debug("%s: the held link no longer answers, dropping", self.address)
        if client is self.client:
            self._drop(client)

    async def initial_connect(self) -> None:
        """Connect once at start-up, off the setup path."""
        try:
            await self.connect()
        except _LOST as err:
            self._log_connect_ended("Initial connect", err)

    async def reconnect(self) -> None:
        """Connect in the background: what an advertisement or a tick set off."""
        try:
            await self.connect()
        except _LOST as err:
            self._log_connect_ended("Reconnect", err)
        finally:
            self.reconnecting = False

    def _log_connect_ended(self, what: str, err: Exception) -> None:
        """Say how a background connect ended, when it did not end as meant.

        Time can run out with the link already taken: during its first
        exchange, during the device-info read after it, or because a command
        got the lock first and connected. That link is held, and what becomes
        of it is the tick's business - it greets one that was not, drops one
        that answers nothing, probes one that is silent. Calling that a failed
        connect sent whoever read the log looking for a lamp out of range: on
        the G7's host (2026-10-06) the line was written of a link that was held
        for ten seconds more, until the radio lost it.
        """
        if isinstance(err, TimeoutError) and self.connected:
            _LOGGER.debug(
                "%s to %s ran out of time, but the link is held (%s); "
                "it is left to the poll",
                what,
                self.address,
                "primed" if self.client is self.primed else "not primed yet",
            )
            return
        _LOGGER.debug("%s to %s failed: %s", what, self.address, _reason(err))

    def advertising(self, present: bool) -> None:
        """Hear that the lamp is advertising, or that it has stopped.

        What the entities' availability goes by when no link is held (see
        ``in_reach``), and the sooner of the two things that set a connect
        off: the other is the tick.
        """
        if not present:
            # The device stopped advertising (powered off / out of range).
            self.present = False
            self._reach_changed()
            return
        was_present = self.present
        self.present = True
        # Reconnect when the device reappears, but only one attempt at a time
        # (advertisements arrive ~every second; don't spawn a connect storm).
        if (
            not self.connected
            and not self.reconnecting
            and monotonic() >= self.dial_not_before
        ):
            self.reconnecting = True
            self._spawn(self.reconnect(), "reconnect")
        if not was_present:
            self._reach_changed()

    def tick(self) -> None:
        """Look the link over: what the poll does every ``_RECONNECT_INTERVAL``.

        Advertisement callbacks are throttled, so a link that dropped is
        dialled again from here whatever the lamp is heard to do. And a link
        that is held is not thereby one that works: one a command made has
        had no first exchange yet, one that failed a call and was never
        reported lost has to be let go of, and one that has been silent for
        long enough is asked.
        """
        for client in tuple(self.unclosed):
            # Still holding a bus nothing could close. The stack may have
            # come round since, and until this goes through nothing dials.
            self.hang_up(client)
        if not self.connected:
            # Held back only while BlueZ will not hang up (note_stuck_hang_up).
            if not self.reconnecting and monotonic() >= self.dial_not_before:
                self.reconnecting = True
                self._spawn(self.reconnect(), "reconnect")
            return
        lost, self.lost = self.lost, None
        if lost is not None and lost[0] is self.client:
            if monotonic() - lost[1] < _LOST_GRACE:
                self.lost = lost
            else:
                # BlueZ said "not connected" and then never reported the link
                # dropped. Waiting any longer is waiting for ever.
                _LOGGER.debug(
                    "%s: the state request failed on this link and it was "
                    "never reported dropped; dropping it",
                    self.address,
                )
                self._drop(lost[0])
                return
        # Connected, but by a command, which skips the first exchange to stay
        # fast. Make it now, off the command's critical path.
        if self.client is not self.primed:
            self._spawn(self.prime_held(), "prime")
        elif monotonic() - self.last_answer >= _PROBE_INTERVAL:
            self._spawn(self.probe_held(), "probe")

    def _drop(self, client: BleakClientWithServiceCache) -> None:
        """Hang ``client`` up, and say that the lamp's reach may have changed.

        For a link let go of because of what it did: lost, answering nothing,
        silent for too long, or given up on by a command. A stop says nothing
        (see ``let_go`` and ``shut_down``).
        """
        self.hang_up(client)
        self._reach_changed()

    def _refuse_what_must_not_be_dialled(self) -> None:
        """Raise the link's own "no" where a dial is known to be wrong."""
        if self.stopped:
            # Only a command gets here: one already in flight when the entry
            # was unloaded, or one sent after Home Assistant began to stop.
            raise NoNewLinkError(
                f"{self.address}: stopped, taking no new link", "not_running"
            )
        if self.unclosed:
            # Every dial opens a connection to the system bus, and the last
            # one could be neither hung up nor closed (see _async_disconnect).
            raise NoNewLinkError(
                f"{self.address}: the previous link is still open and will "
                "not close; not dialling over it",
                "link_not_released",
            )

    def begin(self, present: bool) -> None:
        """Take what is known of the lamp as the watching of it begins.

        Whether it is advertising at that moment. Nobody is told and nothing
        is dialled for it: the first connect is whoever watches the lamp's
        next step, and the entities are not there yet.
        """
        self.present = present

    def halt(self) -> None:
        """Take no new client from here on, and have no episode left to call over.

        The first half of a stop, said before anything is let go of. A
        command may be in flight and outlast it; from here on it is refused a
        new link, so that what is let go of next is the last client this link
        will ever hold. And a link that is no longer watched cannot say when
        the stack lets go: an episode it announced is not its to call over
        any more - the repair standing under the entry from here on is its
        successor's.
        """
        self.stopped = True
        self.fault_announced = False

    def shut_down(self) -> None:
        """Hang up as Home Assistant stops: at once, and without waiting.

        Home Assistant does not unload its config entries when it stops, so
        ``let_go`` never runs then, and nothing hangs the link up but bleak on
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
        client, self.client = self.client, None
        # Said either way: nothing else will tell, afterwards, whether a link
        # was held at this moment and let go of.
        _LOGGER.debug(
            "%s: Home Assistant is stopping: %s",
            self.address,
            "no link held" if client is None else "hanging up",
        )
        if client is not None:
            self.hang_up(client)

    async def let_go(self) -> None:
        """Let go of the client, if one is held: the letting-go half of a stop.

        Whoever watched the lamp has stopped watching it before this is
        called, and has said so (``halt``).
        """
        if self.client is None:
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
        # hang_up), once for every reload that meets a slow link.
        held = False
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_STOP_TIMEOUT):
                    await self.lock.acquire()
                    held = True
        finally:
            # Taken only now, and taken even if the wait was cancelled: taking
            # it first and waiting afterwards meant a cancellation in between
            # dropped the only reference to a connected client.
            client, self.client = self.client, None
            hang_up = None if client is None else self.hang_up(client)
        try:
            if hang_up is not None:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(_STOP_TIMEOUT):
                        await asyncio.shield(hang_up)
        finally:
            if held:
                self.lock.release()

    def hang_up(self, client: BleakClientWithServiceCache) -> asyncio.Task[None]:
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

        So this is called for every client the link is finished with,
        including one whose link is already gone - then it costs nothing, as
        bleak has no device left to disconnect and only closes the bus.

        The task is run where nothing that ends the caller can end it
        (``run_lasting``), and its caller's deadline cannot cancel it. That
        rule exists because a connect that outlives its coordinator claims the
        lamp's slot for nobody; a hang-up that outlives it gives the slot back,
        and one cut short is exactly the leak described above.
        """
        if client is self.client:
            self.client = None
        hang_up = self._run_lasting(
            self._async_disconnect(client), f"glowrium hang up {self.address}"
        )
        # Known to whatever dials next, for as long as it runs (see open).
        self._kept.hanging_up(hang_up)
        return hang_up

    async def _async_disconnect(self, client: BleakClientWithServiceCache) -> None:
        """Disconnect ``client`` under a ceiling, and leave no bus open behind it.

        Asking bleak to disconnect is the polite half, and it can fail: BlueZ
        may never answer, may answer with an error, and the ceiling or a
        cancellation may cut the call short. In each of those bleak has not
        reached the lines that close the client's bus. So whatever came of the
        call, the bus is closed here (see ``_close_bus``).

        If that cannot be done either, the client is not forgotten: it stays in
        ``unclosed``, nothing is dialled over it, and the poll tries it
        again. One connection is then held for as long as the stack stays
        wedged - instead of one more every time the poll comes round.
        """
        # Short once Home Assistant is stopping: it waits for this task.
        ceiling = _STOP_TIMEOUT if self._shutting_down else _HANG_UP_TIMEOUT
        backend = self.backends.get(client)
        if backend is None:
            # One a link before this one could not let go of.
            backend = self._kept.backends.get(client)
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
                self.backends.pop(client, None)
                self._kept.forget(client)
            else:
                self._kept.keep(client, backend)
            if unanswered and bus is not _Bus.CLEAR and _is_bluez(backend):
                # BlueZ's own client, its bus still open, and no answer at
                # all: BlueZ would not hang up. An error would have been an
                # answer, and a proxy's client is not BlueZ's to answer for.
                # Whether the bus could then be closed makes no difference to
                # that: where it could not, the client is kept above, and the
                # stack is every bit as stuck.
                self.note_stuck_hang_up()
            elif hung_up and self.stuck_hang_ups < _STACK_FAULT_AFTER:
                # "In a row" means in a row. Once it has been called a
                # fault, only the lamp answering ends it (note_answer).
                self.stuck_hang_ups = 0

    def note_stuck_hang_up(self) -> None:
        """Count a hang-up BlueZ did not answer; a run of them is the stack.

        From ``_STACK_FAULT_AFTER`` on, the background dials back off: the
        link they would get is the one BlueZ will not let go of, and it answers
        nothing. Said once per episode, and loudly, because nothing the
        integration can do ends it - somebody has to reset the adapter.
        """
        self.stuck_hang_ups += 1
        over = self.stuck_hang_ups - _STACK_FAULT_AFTER
        if over < 0 or self.stopped:
            # A hang-up is given longer than an unload waits for it, so the
            # one that makes it a run can come in after the watching stopped.
            # Nothing dials any more, and an episode announced now is one
            # nobody would be there to call over: the repair would stand
            # until Home Assistant restarted.
            return
        # The exponent is capped as well as the gap: a wedge left alone for
        # days would otherwise raise OverflowError here, on every hang-up.
        gap = _RECONNECT_INTERVAL.total_seconds() * 2 ** min(over + 1, 16)
        self.dial_not_before = monotonic() + min(gap, _STACK_FAULT_BACKOFF_MAX)
        if over == 0:
            self.fault_announced = True
            self._stack_fault(self.stuck_hang_ups)
            _LOGGER.warning(
                "%s: BlueZ has left %d requests in a row to disconnect the lamp "
                "unanswered. That points at the host's Bluetooth stack holding on "
                "to a link that no longer exists, not at the lamp; it clears when "
                "the adapter is power-cycled (bluetoothctl power off, then power "
                "on) or the bluetooth service is restarted. Until the lamp "
                "answers again it is tried less often",
                self.address,
                self.stuck_hang_ups,
            )

    def note_answer(self) -> None:
        """Record that the lamp answered: the link is alive, the stack with it."""
        self.last_answer = monotonic()
        if self.fault_announced:
            self.fault_announced = False
            # At the level the episode was announced at, or whoever read
            # that warning never learns it is over. Only an episode this
            # link announced: a count can reach the mark after it has
            # stopped, without a word, and the repair standing under the
            # entry's id by then is the next coordinator's.
            _LOGGER.warning(
                "%s: the lamp answers again; the Bluetooth stack has let go",
                self.address,
            )
            self._stack_fault(None)
        self.stuck_hang_ups = 0
        self.dial_not_before = 0.0
        self.lost = None  # whatever BlueZ called it, it is answering

    def on_lost(self, client: BleakClientWithServiceCache) -> None:
        """Hear that ``client``'s link is gone: what a dial is given to call."""
        if client is not self.client:
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
        # (see hang_up).
        self._drop(client)
