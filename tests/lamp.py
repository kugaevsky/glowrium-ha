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

from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant

from custom_components.glowrium.coordinator import GlowriumCoordinator

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

    async def start_notify(
        self, _uuid: str, heard: Callable[[Any, bytearray], None]
    ) -> None:
        """Subscribe: what the lamp says from here on goes to ``heard``."""
        self._gone_is_an_error()
        self._heard = heard

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = True
    ) -> None:
        """Take a write, and note it on the lamp."""
        self._gone_is_an_error()
        self._lamp.written.append((uuid, bytes(data)))

    async def read_gatt_char(self, uuid: str) -> bytearray:
        """Answer a read with what the lamp was told to hold there."""
        self._gone_is_an_error()
        if uuid not in self._lamp.readable:
            raise BleakError(f"Characteristic {uuid} was not found")
        return bytearray(self._lamp.readable[uuid])

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
        """Start in range, with nothing written and nothing to read."""
        self.dials = 0
        self.links: list[LampLink] = []
        self.written: list[tuple[str, bytes]] = []
        self.readable: dict[str, bytes] = {}
        self._in_range = True
        self._through: Callable[..., Awaitable[Any]] | None = None

    async def dial(self, lost: Callable[[Any], None]) -> Any:
        """Hand out a link, as the coordinator's dial: this is what it is given."""
        self.dials += 1
        if self._through is not None:
            return await self._through(lost)
        if not self._in_range:
            raise BleakError(f"{ADDRESS} is not in range")
        link = LampLink(self, lost)
        self.links.append(link)
        return link

    def coordinator(
        self,
        hass: HomeAssistant | None,
        name: str = "Glowrium-G7",
        model_id: str | None = None,
    ) -> GlowriumCoordinator:
        """Return a coordinator that dials this lamp."""
        coordinator = GlowriumCoordinator(hass, ADDRESS, name, model_id, dial=self.dial)
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


def lamp_of(coordinator: GlowriumCoordinator) -> ScriptedLamp:
    """Return the lamp ``coordinator`` was built to dial."""
    return _LAMPS[coordinator]
