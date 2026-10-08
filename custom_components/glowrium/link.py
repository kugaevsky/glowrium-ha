"""The link to one lamp: taken, held and let go of.

Stage 1 of splitting the coordinator in two (#21). What is here was the
coordinator's: the client and the lock around it, the hang-up and the closing
of the client's bus, the Bluetooth stack that will not hang up, and whether
the lamp is in reach. Nothing here knows Home Assistant or the lamp's
protocol; what it needs of either it is handed.

The exchanges - a command, the first exchange on a new link, the question for
a silent one - are still made by the coordinator. For this one stage it
therefore reaches the link's state by name, and those attributes are public;
they close when the exchanges move here too. What it imports from here by an
underscored name keeps the name it had when it was the coordinator's.
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
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

# Under the coordinator's name, as these lines always were: a log filter that
# somebody set on it must not go half-blind because the code moved file.
_LOGGER = logging.getLogger(f"{__package__}.coordinator")
_RECONNECT_INTERVAL = timedelta(seconds=30)
# bleak-retry-connector defaults to 4 connect attempts, each of which can sit
# through a 20 s bleak timeout plus a backoff. Against an unreachable device
# that adds up to minutes while the link's lock is held, so a queued command
# cannot even start. It is the coordinator's ceiling on a connect that bounds a
# dial; the attempts are for the
# ones that fail fast. On a weak link a connect is often made and lost within
# a second or two, and the next try inside the same dial is what gets through.
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
# has to stay below the coordinator's ceiling on a command, or a link that will
# not confirm it has
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


class _NoNewLinkError(BleakError):
    """The link's own "no" to a new client: nothing the radio did.

    It has been stopped, or the last client it let go of could not be closed
    and nothing is dialled over that. Still a ``BleakError``, so that every
    background path goes on treating it as a link that could not be had. A
    command asks which of the two it was (``translation_key``) and says so,
    instead of telling its user that the lamp may be out of range and a
    Bluetooth proxy would help - and it does not try a second time, because
    neither changes within a retry.
    """

    def __init__(self, message: str, translation_key: str) -> None:
        """Keep the key of the message a command shows for this."""
        super().__init__(message)
        self.translation_key = translation_key


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
    """

    def __init__(self) -> None:
        """Start with nothing kept."""
        self.clients: set[BleakClientWithServiceCache] = set()
        self.backends: dict[BleakClientWithServiceCache, Any] = {}

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
        reach_changed: Callable[[], None],
        stack_fault: Callable[[int | None], None],
        run_lasting: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        unclosed: Unclosed | None = None,
    ) -> None:
        """Take what the link is handed; it starts with no client and unlocked.

        ``dial`` makes a link (see ``Dial``). ``notify_uuid`` is the
        characteristic the lamp reports on, and ``heard`` is given each frame
        it sends. ``reach_changed`` is called wherever a link is let go of
        because it was lost. ``stack_fault`` is given the count of hang-ups
        left unanswered when a run of them is called a fault, and None when
        the lamp answers again. ``run_lasting`` runs a hang-up where nothing
        that ends the caller can end it. ``unclosed`` is what links to this
        lamp before this one could not let go of (see ``Unclosed``); left
        out, the link keeps its own, as the bench's does.
        """
        self.address = address
        self._dial = dial
        self._notify_uuid = notify_uuid
        self._heard = heard
        self._reach_changed = reach_changed
        self._stack_fault = stack_fault
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
        # it dropped, and when (the coordinator's _LOST_GRACE).
        self.lost: tuple[BleakClientWithServiceCache, float] | None = None
        # When the lamp last answered anything (the coordinator's
        # _PROBE_INTERVAL).
        self.last_answer = monotonic()
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

        The caller holds ``lock`` and has seen that no client is held: the
        coordinator's connect and its write path both come through here, so
        a command can never race a background connect. What the coordinator
        does on the new link - the first exchange, or a command's own write
        and nothing else - is its business and comes after.
        """
        if self.stopped:
            # Only a command gets here: one already in flight when the entry
            # was unloaded, or one sent after Home Assistant began to stop.
            raise _NoNewLinkError(
                f"{self.address}: stopped, taking no new link", "not_running"
            )
        if self.unclosed:
            # Every dial opens a connection to the system bus, and the last
            # one could be neither hung up nor closed (see _async_disconnect).
            raise _NoNewLinkError(
                f"{self.address}: the previous link is still open and will "
                "not close; not dialling over it",
                "link_not_released",
            )
        client = await self._dial(self.on_lost)
        self.backends[client] = getattr(client, "_backend", None)
        try:
            with _gatt_call(client):
                await client.start_notify(self._notify_uuid, self._heard)
            if self.stopped:
                # Stopped while this connect was on its way. Keeping the
                # link would hand it to a coordinator nobody will stop
                # again, and the lamp has one slot.
                raise _NoNewLinkError(  # noqa: TRY301
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
                # being closed (see the coordinator's _async_write).
                # Shielded: a deadline ends the wait, not the hang-up. A
                # cancellation is not kept waiting at all: its deadline has
                # already run out, and the lock is held here.
                await asyncio.shield(hang_up)
            raise
        # Committed only once notifications are live: a client without them
        # reports as connected forever while no state ever arrives again.
        self.client = client
        return client

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

        The coordinator has stopped watching the lamp before it calls this,
        and has set ``stopped``.
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
        return self._run_lasting(
            self._async_disconnect(client), f"glowrium hang up {self.address}"
        )

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
        self.hang_up(client)
        self._reach_changed()
