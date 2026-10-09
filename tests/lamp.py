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
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant

from custom_components.glowrium import cbor
from custom_components.glowrium.const import NOTIFY_UUID
from custom_components.glowrium.coordinator import GlowriumCoordinator
from custom_components.glowrium.link import Link, Turn, Unclosed

ADDRESS = "AA:BB:CC:DD:EE:FF"

# The lamp each coordinator was built to dial, for a helper that is given the
# coordinator alone. Kept here and not on the coordinator: nothing outside the
# tests has any business asking a coordinator what it dials through.
_LAMPS: WeakKeyDictionary[GlowriumCoordinator, ScriptedLamp] = WeakKeyDictionary()


class LampLink:
    """One link to the scripted lamp: a client, as far as the coordinator asks."""

    def __init__(self, lamp: ScriptedLamp, lost: Callable[[Any], None]) -> None:
        """Take the callback the dial was given for a link that is lost."""
        self._lamp = lamp
        self._lost = lost
        self._heard: Callable[[Any, bytearray], None] | None = None
        self.is_connected = True
        self.hung_up = False
        self.subscribed = False

    async def start_notify(
        self, _uuid: str, heard: Callable[[Any, bytearray], None]
    ) -> None:
        """Subscribe: what the lamp says from here on goes to ``heard``."""
        self._gone_is_an_error()
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
        return self._lamp.read_of(uuid)

    async def disconnect(self) -> None:
        """Hang up. The lamp says nothing more on this link."""
        self.hung_up = True
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
        # What the next writes meet, when the lamp is scripted to fail them:
        # the error, how many writes (None: every one), and what the lamp
        # reports back first, if it acts on the write (see fails_writes).
        self._failing: (
            tuple[Exception, int | None, Callable[[bytes], bytes] | None] | None
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
        ``frame`` is reported as it is given, in place of all of that.
        """
        self._answer = (values, only, after, frame)

    def readable(self, uuid: str, data: bytes) -> None:
        """Give a characteristic a value to be read; the rest fail as before."""
        self._readable[uuid] = data

    def never_acknowledges_a_write(self) -> None:
        """Keep every write waiting: it neither returns nor fails."""
        self._never_acknowledges = True

    def fails_writes(
        self,
        error: Exception,
        *,
        times: int | None = None,
        saying: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """Fail the next ``times`` writes (every one, if not given) with ``error``.

        A failed write was still put to the lamp, and is noted as one. With
        ``saying``, the lamp reports back first - what it makes of the frame
        it was written - and only then fails the write: a lamp that acted on
        it and lost the acknowledgement.
        """
        self._failing = (error, times, saying)

    async def taken(self, link: LampLink, uuid: str, frame: bytes) -> None:
        """Note a write ``link`` was given, and do with it what the lamp is scripted to.

        Fail it, answer it if it is the state request, or keep it waiting.
        """
        self.written.append((uuid, frame))
        self.exchanges.append(("write", uuid))
        if self._never_acknowledges:
            await asyncio.Event().wait()
        if self._failing is not None:
            error, left, saying = self._failing
            if left is not None:
                left -= 1
                self._failing = (error, left, saying) if left > 0 else None
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

    def read_of(self, uuid: str) -> bytearray:
        """Note a read, and return what the lamp was given to read there."""
        self.read.append(uuid)
        self.exchanges.append(("read", uuid))
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

    def coordinator(
        self,
        hass: HomeAssistant | None,
        name: str = "Glowrium-G7",
        model_id: str | None = None,
        unclosed: Unclosed | None = None,
    ) -> GlowriumCoordinator:
        """Return a coordinator that dials this lamp.

        ``unclosed`` is what coordinators for the lamp before this one could
        not let go of, when a test has two of them in turn.
        """
        coordinator = GlowriumCoordinator(
            hass, ADDRESS, name, model_id, dial=self.dial, unclosed=unclosed
        )
        _LAMPS[coordinator] = self
        return coordinator

    def out_of_range(self) -> None:
        """Answer every dial from here on as a lamp that cannot be found."""
        self._in_range = False
        self._through = None

    def dials_through(self, dial: Callable[..., Awaitable[Any]]) -> None:
        """Send every dial from here on to ``dial``, and hand back what it does."""
        self._through = dial

    def say(self, frame: bytes) -> None:
        """Notify ``frame`` on every link that has not been hung up or lost."""
        for link in self.links:
            link.say(frame)

    def lose(self) -> None:
        """Lose every link that is up."""
        for link in self.links:
            link.lose()


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
    """Return a turn at the lamp on ``client``, for a test that built one by hand.

    The device half's exchanges take a turn, which the link makes over a client
    it holds. A test that calls one of them directly on a client of its own
    making has the same made here.
    """
    return Turn(link_of(coordinator), client)


def lamp_of(coordinator: GlowriumCoordinator) -> ScriptedLamp:
    """Return the lamp ``coordinator`` was built to dial."""
    if coordinator not in _LAMPS:
        raise LookupError(
            "this coordinator was not built at a scripted lamp: "
            "build it with ScriptedLamp().coordinator(...)"
        )
    return _LAMPS[coordinator]
