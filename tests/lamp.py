"""A scripted lamp for the tests, standing where the coordinator dials.

The coordinator is handed its dial. In a test that dial is this lamp's: it
hands out a client, says a frame through whatever the coordinator subscribed
with, loses the link the way the Bluetooth stack reports one lost, and is
silent once it has been hung up - a lamp that went on talking after a hang-up
would vouch for commands that no link could have carried.

A test that already has a dial of its own - a client built by hand, a stack
that will not hang up - puts it behind the lamp's with ``dials_through``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
import contextlib
from typing import Any
from unittest.mock import patch
from weakref import WeakKeyDictionary

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant

from custom_components.glowrium import cbor
from custom_components.glowrium.const import NOTIFY_UUID
from custom_components.glowrium.coordinator import GlowriumCoordinator, Keeper
from custom_components.glowrium.link import Link, Turn, Unclosed

ADDRESS = "AA:BB:CC:DD:EE:FF"


class NothingBehind:
    """What a link of the scripted lamp has behind it: no bus, and not BlueZ's.

    The link tells BlueZ's client by the module its class lives in, and the
    diagnostics name a client by its class.
    """


_NOTHING_BEHIND = NothingBehind()

# The lamp each coordinator was built to dial, for a helper that is given the
# coordinator alone. Kept here and not on the coordinator: nothing outside the
# tests has any business asking a coordinator what it dials through.
_LAMPS: WeakKeyDictionary[GlowriumCoordinator, ScriptedLamp] = WeakKeyDictionary()
# And the keeper each was built with: the tests' own, unless a test handed one in.
_KEEPERS: WeakKeyDictionary[GlowriumCoordinator, ScriptedKeeper] = WeakKeyDictionary()


class LampLink:
    """One link to the scripted lamp: a client, as far as the coordinator asks."""

    def __init__(self, lamp: ScriptedLamp, lost: Callable[[Any], None]) -> None:
        """Take the callback the dial was given for a link that is lost."""
        self._lamp = lamp
        self._lost = lost
        self._heard: Callable[[Any, bytearray], None] | None = None
        self.is_connected = True
        # How many hang-ups were asked for, and how many got through.
        self.hang_ups = 0
        self.hung_up = 0
        self.subscribed = False
        # What sits behind the link, as the link asks a client: nothing with a
        # bus of its own, so a hang-up that failed leaves nothing to close.
        self._backend = _NOTHING_BEHIND

    async def start_notify(
        self, _uuid: str, heard: Callable[[Any, bytearray], None]
    ) -> None:
        """Subscribe: what the lamp says from here on goes to ``heard``."""
        self._gone_is_an_error()
        await self._lamp.subscribing()
        self._heard = heard
        self.subscribed = True

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = True
    ) -> None:
        """Take a write, and note it on the lamp - which may be scripted to fail it."""
        self._gone_is_an_error()
        await self._lamp.taken(self, uuid, bytes(data))

    async def read_gatt_char(self, uuid: str) -> bytearray:
        """Read a characteristic: what the lamp was given to read there, or nothing.

        Nothing fails as a lamp that cannot be read does. The first exchange
        on a link ends with the device-info read, and the device half carries
        on without it when it fails - as it does on a link that is gone,
        which is what the error says.
        """
        self._gone_is_an_error()
        return await self._lamp.read_of(uuid)

    async def disconnect(self) -> None:
        """Hang up. The lamp says nothing more on this link.

        Unless the lamp is scripted to be slow about it, or to fail it: then
        the hang-up was asked for and did not get through.
        """
        self.hang_ups += 1
        await self._lamp.hanging_up()
        self.hung_up += 1
        self.is_connected = False

    def say(self, frame: bytes) -> None:
        """Notify ``frame``, unless the link is gone or nobody subscribed."""
        if self.is_connected and self._heard is not None:
            self._heard(None, bytearray(frame))

    def lose(self) -> None:
        """Report the link lost, as the Bluetooth stack does: to the callback."""
        if self.is_connected:
            self.is_connected = False
            self._lost(self)

    def _gone_is_an_error(self) -> None:
        if not self.is_connected:
            raise BleakError("Not connected")


class ScriptedKeeper:
    """What keeps a test's coordinator: it starts the work, and notes what was asked.

    Work goes where Home Assistant waits for it - ``hass.async_create_task``,
    so that ``async_block_till_done`` settles a connect or a hang-up that a
    test set off - and with no Home Assistant on the loop, kept until it is
    done. Both lifetimes of work go to the same place: what ties work to a
    config entry is the adapter's (``EntryKeeper``), and is tested on it.
    """

    def __init__(self, hass: HomeAssistant | None) -> None:
        """Keep a coordinator for a test, on ``hass`` or on the bare loop."""
        self._hass = hass
        self._kept: set[asyncio.Task[None]] = set()
        # The names of the work asked for, by the lifetime it was asked for.
        self.spawned: list[str] = []
        self.lasting: list[str] = []
        # What was said of a stack that will not hang up: each count, and
        # ``None`` for each time it was said to be over.
        self.stack_faults: list[int | None] = []
        # What the lamp was said to be, each time: model id, firmware,
        # serial number.
        self.described: list[tuple[str | None, str | None, str | None]] = []
        # Where home is: latitude and longitude. Zero and zero is no home.
        self.home: tuple[float, float] = (0, 0)
        # Whether the lamp is on the air when the watching begins.
        self.present = True
        # What the device half handed over to be told through, while it
        # is watched for.
        self._told: tuple[Callable[[bool], None], Callable[[], None]] | None = None

    def spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Start work asked for as work that is not to outlive the keeping."""
        self.spawned.append(name)
        self._start(work, name)

    def run_lasting(
        self, work: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Start work that outlives the watching, and return it."""
        self.lasting.append(name)
        return self._start(work, name)

    def watch(
        self, advertising: Callable[[bool], None], tick: Callable[[], None]
    ) -> bool:
        """Take what to tell the device half through; answer ``present``."""
        self._told = (advertising, tick)
        return self.present

    def unwatch(self) -> None:
        """Tell the device half nothing from here on."""
        self._told = None

    def stack_fault(self, count: int | None) -> None:
        """Note what was said of a stack that will not hang up."""
        self.stack_faults.append(count)

    def lamp_described(
        self, model_id: str | None, firmware: str | None, serial: str | None
    ) -> None:
        """Note what the lamp was said to be."""
        self.described.append((model_id, firmware, serial))

    @property
    def watching(self) -> bool:
        """Whether the device half has asked to be told, and not asked to stop."""
        return self._told is not None

    def hears(self, present: bool) -> None:
        """Tell the device half the lamp was heard, or is no longer."""
        assert self._told is not None, "nothing is told to a keeper not watching"
        self._told[0](present)

    def ticks(self) -> None:
        """Tell the device half the clock has come round."""
        assert self._told is not None, "nothing is told to a keeper not watching"
        self._told[1]()

    def coordinates(self) -> tuple[float, float]:
        """Say where ``home`` is."""
        return self.home

    def _start(self, work: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        if self._hass is not None:
            return self._hass.async_create_task(work, name)
        task = asyncio.get_running_loop().create_task(work, name=name)
        self._kept.add(task)
        task.add_done_callback(self._kept.discard)
        return task


class ScriptedLamp:
    """The lamp, as the coordinator meets it: at its dial."""

    def __init__(self) -> None:
        """Start in range, with nothing written."""
        self.dials = 0
        self.links: list[LampLink] = []
        self.written: list[tuple[str, bytes]] = []
        # The characteristics read, in order; and what the lamp was asked,
        # written and read, in one order - the device info is to be read last
        # on a link, and that is a fact about order.
        self.read: list[str] = []
        self.exchanges: list[tuple[str, str]] = []
        self._in_range = True
        self._found: asyncio.Event | None = None
        self._through: Callable[..., Awaitable[Any]] | None = None
        self._readable: dict[str, bytes] = {}
        # How the lamp answers the state request, once told to (see answers).
        self._answer: (
            tuple[dict[int, Any] | None, tuple[int, ...] | None, float, bytes | None]
            | None
        ) = None
        self._never_acknowledges = False
        self._acknowledges_when: asyncio.Event | None = None
        self._never_reads = False
        self._read_answered_when: asyncio.Event | None = None
        self._read_fails: Exception | None = None
        self._hang_up_released: asyncio.Event | None = None
        self._hang_up_fails: Exception | None = None
        self._subscribed_when: asyncio.Event | None = None
        self._subscription_fails: Exception | None = None
        # What the next writes meet, when the lamp is scripted to fail them:
        # the error, how many writes (None: every one), what the lamp reports
        # back first if it acts on the write, and which characteristic alone
        # (None: any) - see fails_writes.
        self._failing: (
            tuple[Exception, int | None, Callable[[bytes], bytes] | None, str | None]
            | None
        ) = None

    @property
    def asked(self) -> list[bytes]:
        """What the lamp was asked to report: the ids of each state request."""
        return [frame for uuid, frame in self.written if uuid == NOTIFY_UUID]

    def answers(
        self,
        values: dict[int, Any] | None = None,
        *,
        only: tuple[int, ...] | None = None,
        after: float = 0.0,
        frame: bytes | None = None,
    ) -> None:
        """Answer the state request as a G7 does: one report, inside the write.

        It carries every id asked for, zero unless ``values`` gives one, and
        only the ids in ``only`` if that is given - a lamp that knows some of
        what it is asked. ``after`` holds the answer back for that long;
        ``frame`` is reported as it is given, in place of all of that. A lamp
        that answers is no longer one that never acknowledges a write.
        """
        self._never_acknowledges = False
        self._answer = (values, only, after, frame)

    def forget(self) -> None:
        """Forget what was written, asked and read so far.

        For a helper that takes a link for a test: what the taking wrote
        and read is cleared away, and what comes next is the test's.
        """
        self.written.clear()
        self.exchanges.clear()
        self.read.clear()

    def readable(self, uuid: str, data: bytes | None) -> None:
        """Give a characteristic a value to be read, or take it away (``None``).

        The rest fail as before.
        """
        if data is None:
            self._readable.pop(uuid, None)
        else:
            self._readable[uuid] = data

    def never_acknowledges_a_write(self) -> None:
        """Keep every write waiting: it neither returns nor fails."""
        self._never_acknowledges = True

    def acknowledges_when(self, acknowledged: asyncio.Event) -> None:
        """Hold every write until ``acknowledged`` is set: a slow acknowledgement."""
        self._acknowledges_when = acknowledged

    def never_answers_a_read(self) -> None:
        """Keep every read waiting: it neither returns nor fails."""
        self._never_reads = True

    def answers_a_read_when(self, answered: asyncio.Event) -> None:
        """Hold every read until ``answered`` is set: an answer slow to come."""
        self._read_answered_when = answered

    def fails_reads(self, error: Exception) -> None:
        """Fail every read with ``error``, whatever the lamp was given to read."""
        self._read_fails = error

    def hangs_up_when(self, released: asyncio.Event) -> None:
        """Hold every hang-up until ``released`` is set: a slow disconnect."""
        self._hang_up_released = released

    def fails_hang_ups(self, error: Exception) -> None:
        """Fail every hang-up with ``error``, as a stack that answers no."""
        self._hang_up_fails = error

    def subscribes_when(self, subscribed: asyncio.Event) -> None:
        """Hold every subscription until ``subscribed`` is set."""
        self._subscribed_when = subscribed

    def subscription_fails(self, error: Exception | None) -> None:
        """Fail every subscription with ``error`` (``None``: fail none again).

        A link taken and not kept.
        """
        self._subscription_fails = error

    async def hanging_up(self) -> None:
        """Let a link's hang-up through, late, or not at all."""
        if self._hang_up_released is not None:
            await self._hang_up_released.wait()
        if self._hang_up_fails is not None:
            raise self._hang_up_fails

    async def subscribing(self) -> None:
        """Let a link's subscription through, late, or fail it."""
        if self._subscribed_when is not None:
            await self._subscribed_when.wait()
        if self._subscription_fails is not None:
            raise self._subscription_fails

    def fails_writes(
        self,
        error: Exception,
        *,
        times: int | None = None,
        saying: Callable[[bytes], bytes] | None = None,
        of: str | None = None,
    ) -> None:
        """Fail the next ``times`` writes (every one, if not given) with ``error``.

        A failed write was still put to the lamp, and is noted as one. With
        ``saying``, the lamp reports back first - what it makes of the frame
        it was written - and only then fails the write: a lamp that acted on
        it and lost the acknowledgement. With ``of``, only the writes to that
        characteristic fail; the rest go through.
        """
        self._failing = (error, times, saying, of)

    async def taken(self, link: LampLink, uuid: str, frame: bytes) -> None:
        """Note a write ``link`` was given, and do with it what the lamp is scripted to.

        Fail it, answer it if it is the state request, or keep it waiting -
        for ever, or until it is told to acknowledge.
        """
        self.written.append((uuid, frame))
        self.exchanges.append(("write", uuid))
        if self._never_acknowledges:
            await asyncio.Event().wait()
        if self._acknowledges_when is not None:
            await self._acknowledges_when.wait()
        if self._failing is not None and self._failing[3] in (None, uuid):
            error, left, saying, of = self._failing
            if left is not None:
                left -= 1
                self._failing = (error, left, saying, of) if left > 0 else None
            if saying is not None:
                link.say(saying(frame))
            raise error
        if uuid == NOTIFY_UUID and self._answer is not None:
            values, only, after, raw = self._answer
            if after:
                await asyncio.sleep(after)
            if raw is None:
                asked = [key for key in frame if only is None or key in only]
                raw = cbor.encode(dict.fromkeys(asked, 0) | (values or {}))
            link.say(raw)

    async def read_of(self, uuid: str) -> bytearray:
        """Note a read, and return what the lamp was given to read there.

        Or keep it waiting, or fail it, as the lamp is scripted to.
        """
        self.read.append(uuid)
        self.exchanges.append(("read", uuid))
        if self._never_reads:
            await asyncio.Event().wait()
        if self._read_answered_when is not None:
            await self._read_answered_when.wait()
        if self._read_fails is not None:
            raise self._read_fails
        if uuid in self._readable:
            return bytearray(self._readable[uuid])
        raise BleakError("Not connected")

    async def dial(self, lost: Callable[[Any], None]) -> Any:
        """Hand out a link, as the coordinator's dial: this is what it is given."""
        self.dials += 1
        if self._found is not None:
            await self._found.wait()
        if self._through is not None:
            return await self._through(lost)
        if not self._in_range:
            raise BleakError(f"{ADDRESS} is not in range")
        link = LampLink(self, lost)
        self.links.append(link)
        return link

    def dials_when(self, found: asyncio.Event) -> None:
        """Hold every dial until ``found`` is set: a lamp that is slow to connect."""
        self._found = found

    def coordinator(  # noqa: PLR0913 - what a coordinator is built from, by name
        self,
        hass: HomeAssistant | None,
        name: str = "Glowrium-G7",
        model_id: str | None = None,
        unclosed: Unclosed | None = None,
        keeper: Keeper | None = None,
        *,
        bring_up: bool | None = None,
    ) -> GlowriumCoordinator:
        """Return a coordinator that dials this lamp.

        ``unclosed`` is what coordinators for the lamp before this one could
        not let go of, when a test has two of them in turn. ``keeper`` is
        what keeps the coordinator; left out, a ``ScriptedKeeper`` on
        ``hass``, which ``keeper_of`` hands back. ``bring_up`` is whether a
        lamp that says it is not activated is brought up; left out, the
        coordinator is not told, and is built as the integration builds one.
        """
        if keeper is None:
            keeper = ScriptedKeeper(hass)
        coordinator = GlowriumCoordinator(
            ADDRESS,
            name,
            model_id,
            keeper=keeper,
            dial=self.dial,
            unclosed=unclosed,
            **({} if bring_up is None else {"bring_up": bring_up}),
        )
        _LAMPS[coordinator] = self
        if isinstance(keeper, ScriptedKeeper):
            _KEEPERS[coordinator] = keeper
        return coordinator

    def out_of_range(self) -> None:
        """Answer every dial from here on as a lamp that cannot be found."""
        self._in_range = False
        self._through = None

    def dials_through(self, dial: Callable[..., Awaitable[Any]] | None) -> None:
        """Send every dial from here on to ``dial``, and hand back what it does.

        ``None`` gives the dials back to the lamp: it hands out links of its
        own again.
        """
        self._through = dial

    def say(self, frame: bytes) -> None:
        """Notify ``frame`` on every link that has not been hung up or lost."""
        for link in self.links:
            link.say(frame)

    def lose(self) -> None:
        """Lose every link that is up."""
        for link in self.links:
            link.lose()


@contextlib.contextmanager
def in_range(lamp: ScriptedLamp) -> Iterator[None]:
    """Put ``lamp`` where the integration's own dial finds it and connects to it.

    For a coordinator the integration's setup made, which is handed the dial
    of a config entry: what stands in for the lamp is what that dial is made
    of - Home
    Assistant's lookup of the device, and the library's connect, which here
    hands out the lamp's links. With the tests of the dial itself, the one
    place either is replaced at module level.
    """

    async def _connects(*_args: object, **kwargs: object) -> Any:
        return await lamp.dial(kwargs["disconnected_callback"])

    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=object(),
        ),
        patch(
            "custom_components.glowrium.link.establish_connection",
            side_effect=_connects,
        ),
    ):
        yield


def link_of(coordinator: GlowriumCoordinator) -> Link:
    """Return the link ``coordinator`` speaks through: the tests' one door to it.

    The link's own interface - connected, in reach, the tick, an
    advertisement, a connect - is what Home Assistant's watchers drive. A test
    drives it through here instead of standing up the Bluetooth manager and
    the timer. This is the one reach into the coordinator the tests keep, and
    this is its reason.
    """
    return coordinator._link


def turn_over(coordinator: GlowriumCoordinator, client: Any) -> Turn:
    """Return a turn at the lamp on ``client``: as a rule, the lamp's own link.

    The device half's exchanges take a turn, which the link makes over a client
    it holds. A test that calls one of them directly - one step of the first
    exchange, where the test is of that step - has the same made here.
    """
    return Turn(link_of(coordinator), client)


async def taken_bare(link: Link) -> Any:
    """Take a link as a command takes one, and return its client.

    Under the link's lock, as ``Link.open`` says its caller holds it: dialled,
    subscribed to, and nothing asked of the lamp.
    """
    async with link.lock:
        return await link.open()


def lamp_of(coordinator: GlowriumCoordinator) -> ScriptedLamp:
    """Return the lamp ``coordinator`` was built to dial."""
    if coordinator not in _LAMPS:
        raise LookupError(
            "this coordinator was not built at a scripted lamp: "
            "build it with ScriptedLamp().coordinator(...)"
        )
    return _LAMPS[coordinator]


def keeper_of(coordinator: GlowriumCoordinator) -> ScriptedKeeper:
    """Return the keeper ``coordinator`` was built with, when it is the tests' own."""
    if coordinator not in _KEEPERS:
        raise LookupError(
            "this coordinator is not kept by a ScriptedKeeper: "
            "build it with ScriptedLamp().coordinator(...) and no keeper of its own"
        )
    return _KEEPERS[coordinator]


def nothing_heard(coordinator: GlowriumCoordinator) -> None:
    """Start the coordinator's mirror afresh: nothing heard, nothing echoed.

    For a test that took a link by a command and wants no trace of that
    command in the mirror. The mirror itself forgets nothing (``Mirror``), so
    the coordinator is handed a new one, built as it builds its own.
    """
    coordinator._mirror = coordinator._new_mirror()


def flood(take: Callable[[bytes], object], ids: int = 200) -> None:
    """Hand ``take`` frames that report ``ids`` ids never sent before, fifty a frame.

    Ids from 1000 on, which nobody has a name for, and more of them than the
    mirror has room for: what a device does that answers at the lamp's
    address and is not a lamp.
    """
    for first in range(1000, 1000 + ids, 50):
        last = min(first + 50, 1000 + ids)
        take(cbor.encode(dict.fromkeys(range(first, last), True)))
