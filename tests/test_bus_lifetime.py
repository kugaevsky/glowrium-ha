"""The coordinator against a BlueZ that never finishes a disconnect.

bleak's BlueZ client opens a D-Bus connection in ``connect()`` and closes it on
the last lines of a ``disconnect()`` that got that far. Everything the
coordinator did about hanging up rested on that call finishing. These tests
take the assumption away and ask the only question that matters to the host:
how many connections are open afterwards.

Seen on a real lamp, 2026-10-04: bluetoothd went on reporting the device as
connected after the controller had lost the link. Every new client "connected"
at once, every GATT call answered "Not connected", and BlueZ never answered
``Disconnect`` - so each poll tick left one more connection behind, two a
minute, until the bus refused Home Assistant's user at its limit of 256.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.exc import BleakError
from dbus_fast import MessageType
from homeassistant.core import HomeAssistant
import pytest

from custom_components.glowrium import coordinator as coordinator_module
from custom_components.glowrium.coordinator import GlowriumCoordinator

_NOT_CONNECTED = "[org.bluez.Error.Failed] Not connected"


class _Bus:
    """The D-Bus connection bleak opens for one client."""

    def __init__(self) -> None:
        self.closed = False

    def disconnect(self) -> None:
        self.closed = True

    async def wait_for_disconnect(self) -> None:
        return


class _Backend:
    """What sits behind a client: bleak's BlueZ backend, as far as it matters."""

    def __init__(self, bus: _Bus) -> None:
        self._bus: _Bus | None = bus


class _WedgedClient:
    """A client of a device bluetoothd believes connected and the radio lost.

    ``connect`` has returned, so the bus is open. Every GATT call is refused.
    ``disconnect`` follows bleak 3.0.2: nothing to do once the bus is closed,
    otherwise ask BlueZ and wait - and here BlueZ never answers, until
    ``host.released`` says the stack has come back to its senses.
    """

    def __init__(self, host: _WedgedBlueZ, backend: Any) -> None:
        self._host = host
        self._backend = backend
        self.is_connected = True
        self.start_notify = AsyncMock()
        self.read_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))
        self.write_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1
        bus = self._host.bus_of[self]
        if bus is None or bus.closed:
            return
        await self._host.released.wait()
        bus.disconnect()
        backend = self._backend
        if backend is not None and hasattr(backend, "_bus"):
            backend._bus = None
        self.is_connected = False


class _WedgedBlueZ:
    """Hands out wedged clients and keeps every bus it ever opened."""

    def __init__(self, behind: Any = _Backend) -> None:
        self.released = asyncio.Event()
        self.clients: list[_WedgedClient] = []
        self.bus_of: dict[_WedgedClient, _Bus] = {}
        self._behind = behind

    async def dial(self, *_args: object, **_kwargs: object) -> _WedgedClient:
        bus = _Bus()
        client = _WedgedClient(self, self._behind(bus))
        self.clients.append(client)
        self.bus_of[client] = bus
        return client

    @property
    def open_buses(self) -> int:
        return sum(1 for bus in self.bus_of.values() if not bus.closed)


def _wedged(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, behind: Any = _Backend
) -> tuple[GlowriumCoordinator, _WedgedBlueZ]:
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    host = _WedgedBlueZ(behind)
    monkeypatch.setattr(coordinator_module, "establish_connection", host.dial)
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.01)
    coordinator._ble_device = object
    return coordinator, host


async def _polls(
    coordinator: GlowriumCoordinator, hass: HomeAssistant, count: int
) -> None:
    """Run ``count`` poll ticks, each given time to reach the hang-up's ceiling."""
    for _ in range(count):
        coordinator._async_poll_reconnect(None)
        await asyncio.sleep(0.04)
    await hass.async_block_till_done()


async def test_a_wedged_bluez_does_not_cost_a_connection_per_poll(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poll after poll against a stack that never hangs up: the count stays put.

    This is the whole incident in one number. Whatever the coordinator does
    about a hang-up that cannot finish, it may not leave open a connection it
    has no way of closing, and then go and open another.
    """
    coordinator, host = _wedged(hass, monkeypatch)

    await _polls(coordinator, hass, 20)

    assert len(host.clients) > 1  # it did go on dialling
    assert host.open_buses <= 1


async def test_a_wedged_bluez_does_not_cost_a_connection_per_command(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same by the other door: a user pressing the button again and again."""
    coordinator, host = _wedged(hass, monkeypatch)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)

    for _ in range(10):
        with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal will do
            await coordinator.async_set_power(True)
        await asyncio.sleep(0.04)
    await hass.async_block_till_done()

    assert host.open_buses <= 1


async def test_nothing_is_left_open_once_bluez_lets_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the stack recovers, every connection opened meanwhile is closed.

    A bound on the count is not enough by itself: a client parked for ever
    would hold its connection for the life of the process.
    """
    coordinator, host = _wedged(hass, monkeypatch)
    await _polls(coordinator, hass, 5)

    host.released.set()
    await _polls(coordinator, hass, 3)

    assert host.open_buses == 0


async def test_the_bus_is_found_even_after_the_wrapper_let_go_of_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is behind a client is noted when it is taken, not when it is dropped.

    Home Assistant's wrapper forgets its backend when it gives a link up for
    lost - "the link leaks to BlueZ", in its own words - and by then the only
    way to the bus is the reference taken at the start.
    """
    coordinator, host = _wedged(hass, monkeypatch)

    async def _dial_then_forget(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()

        async def _wrapper_gives_up(*_a: object, **_k: object) -> None:
            client._backend = None

        client.start_notify = AsyncMock(side_effect=_wrapper_gives_up)
        return client

    monkeypatch.setattr(coordinator_module, "establish_connection", _dial_then_forget)

    await _polls(coordinator, hass, 5)

    assert len(host.clients) == 5  # closed each time, so it went on dialling
    assert host.open_buses == 0


class _ProxyBackend:
    """A backend with no D-Bus connection of its own, as a Bluetooth proxy's."""

    def __init__(self, _bus: _Bus) -> None:
        pass


async def test_a_backend_with_no_bus_is_not_held_against_the_lamp(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy's client has nothing to close, so a failed hang-up parks nothing.

    The bound below is for connections to the system bus. Refusing to dial
    because a client that never had one would not disconnect only takes the
    lamp away.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_ProxyBackend)

    await _polls(coordinator, hass, 4)

    assert len(host.clients) == 4


# bleak's BlueZ client by its module, but without the attribute this
# integration reaches for - as it would look after bleak renamed it.
_MovedBackend = type(
    "BleakClientBlueZDBus",
    (),
    {
        "__module__": "bleak.backends.bluezdbus.client",
        "__init__": lambda self, _bus: None,
    },
)


async def test_a_bus_that_cannot_be_reached_stops_the_dialling(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If bleak moves its internals, the count is still bounded - at one.

    Closing a client's bus goes through bleak's private attributes, and those
    can change under us. When a hang-up fails and the bus cannot be reached,
    the client is kept and nothing more is dialled until it has disconnected:
    the lamp is lost for that while, the system bus is not.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_MovedBackend)

    await _polls(coordinator, hass, 6)

    assert len(host.clients) == 1  # one dial, and no more over it
    assert host.open_buses == 1
    assert host.clients[0].disconnects > 1  # and it keeps trying to close it

    host.released.set()
    await _polls(coordinator, hass, 3)

    assert host.open_buses == 0
    assert len(host.clients) > 1  # dialling again


class _StuckBus(_Bus):
    """A bus that will not close when told to."""

    def disconnect(self) -> None:
        raise OSError(9, "Bad file descriptor")


async def test_a_bus_that_will_not_close_stops_the_dialling(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same when the bus is reached and refuses: kept, and not dialled over."""
    coordinator, host = _wedged(hass, monkeypatch)

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    monkeypatch.setattr(coordinator_module, "establish_connection", _dial_a_stuck_one)

    await _polls(coordinator, hass, 6)

    assert len(host.clients) == 1


async def test_a_disconnect_that_returns_is_not_taken_at_its_word(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnect() that returns may have left the bus open all the same.

    bleak's does when another disconnect of the same client is under way:
    it waits for that one and leaves the closing to it. Whether the other one
    got that far is not something the caller is told, so the bus is looked at
    after a hang-up that succeeded as well.
    """
    coordinator, host = _wedged(hass, monkeypatch)
    client = await host.dial()
    client.disconnect = AsyncMock()  # returns, having closed nothing
    coordinator._client = client

    await coordinator._hang_up(client)

    assert host.open_buses == 0


class _StubBus:
    """dbus-fast's bus as bleak's client uses it, with BlueZ's answer scripted."""

    def __init__(self, answer: str) -> None:
        self.closed = False
        self.calls: list[str] = []
        self._answer = answer

    async def call(self, message: Any) -> Any:
        self.calls.append(message.member)
        if self._answer == "never":
            await asyncio.Event().wait()
        return SimpleNamespace(
            message_type=MessageType.ERROR,
            error_name="org.bluez.Error.Failed",
            body=["Operation already in progress"],
        )

    def disconnect(self) -> None:
        self.closed = True

    async def wait_for_disconnect(self) -> None:
        return


class _HaClient:
    """Home Assistant's wrapper around a backend, as far as hanging up goes."""

    def __init__(self, backend: BleakClientBlueZDBus) -> None:
        self._backend: BleakClientBlueZDBus | None = backend

    @property
    def is_connected(self) -> bool:
        return self._backend is not None and self._backend.is_connected

    async def disconnect(self) -> None:
        if self._backend is None:
            return
        await self._backend.disconnect()


def _bleaks_own_client(bus: _StubBus) -> tuple[BleakClientBlueZDBus, list[str]]:
    """Return bleak's BlueZ client in the state ``connect()`` leaves it in."""
    backend = object.__new__(BleakClientBlueZDBus)
    removed: list[str] = []
    backend._bus = bus
    backend._is_connected = True
    backend._disconnecting_event = None
    backend._disconnect_monitor_event = asyncio.Event()
    backend._remove_device_watcher = lambda: removed.append("watcher")
    backend._device_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    backend.services = object()
    return backend, removed


@pytest.mark.parametrize("answer", ["never", "error"])
async def test_bleaks_own_client_ends_up_closed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Against bleak's real client: a failed hang-up still closes everything.

    Two ways ``disconnect()`` stops short of the lines that close the bus:
    BlueZ never answers ``Disconnect`` (the incident), and BlueZ answers it
    with an error. The doubles above stand in for bleak; this runs bleak's own
    code, so that a release which renames what the coordinator reaches for
    fails here and not on somebody's host.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.05)
    bus = _StubBus(answer)
    backend, removed = _bleaks_own_client(bus)
    monitor = backend._disconnect_monitor_event
    client = _HaClient(backend)
    coordinator._client = client

    await coordinator._hang_up(client)

    assert bus.calls == ["Disconnect"]  # asked properly first
    assert bus.closed
    # And bleak is left as it leaves itself when BlueZ reports a link gone:
    # no bus, no watcher, no services, its monitor task released.
    assert backend._bus is None
    assert backend._is_connected is False
    assert not client.is_connected
    assert removed == ["watcher"]
    assert backend.services is None
    assert monitor.is_set()
    await client.disconnect()  # nothing left for bleak to do, and no error
    assert bus.calls == ["Disconnect"]


async def test_a_hang_up_cancelled_half_way_still_closes_the_bus(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation is not an exception, and it leaves the bus open just the same."""
    coordinator, host = _wedged(hass, monkeypatch)
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 60)
    client = await host.dial()
    coordinator._client = client

    hang_up = coordinator._hang_up(client)
    await asyncio.sleep(0.01)  # into disconnect(), waiting on BlueZ
    hang_up.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hang_up

    assert host.open_buses == 0
