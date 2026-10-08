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

import ast
import asyncio
from itertools import pairwise
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.exc import BleakError
from dbus_fast import MessageType
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.glowrium import cbor, coordinator as coordinator_module
from custom_components.glowrium.const import (
    DOMAIN,
    KEY_ACTIVATED,
    KEY_POWER,
    NOTIFY_UUID,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator

from .lamp import ScriptedLamp, lamp_of

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
    """What sits behind a client: bleak's BlueZ backend, as far as it matters.

    By where it lives, too: the coordinator tells BlueZ's client from any other
    by the module of its class, before it reaches for what is inside.
    """

    __module__ = "bleak.backends.bluezdbus.client"

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
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    behind: Any = _Backend,
    *,
    backs_off: bool = False,
) -> tuple[GlowriumCoordinator, _WedgedBlueZ]:
    """Return a coordinator facing a wedged stack.

    Unless ``backs_off``, it goes on dialling at every poll tick however often
    BlueZ fails to hang up: most tests here are about what each of those dials
    leaves behind, and want as many of them as there are ticks.
    """
    host = _WedgedBlueZ(behind)
    lamp = ScriptedLamp()
    lamp.dials_through(host.dial)
    coordinator = lamp.coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.01)
    if not backs_off:
        monkeypatch.setattr(coordinator_module, "_STACK_FAULT_AFTER", 10**6)
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

    lamp_of(coordinator).dials_through(_dial_then_forget)

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


class _ProxyWithABus:
    """A proxy's backend that keeps something of its own under bleak's name."""

    def __init__(self, bus: _Bus) -> None:
        self._bus = bus


async def test_a_backend_that_is_not_bluezs_is_left_alone_whatever_it_holds(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whose client it is is told by where its class lives, not by what it holds.

    The reach for the bus goes through bleak's private attributes, and only
    BlueZ's client is known to keep a connection to the system bus there.
    Another backend's attribute of the same name is its own affair: it is not
    closed for it, and a hang-up that fails behind it parks nothing.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_ProxyWithABus)

    await _polls(coordinator, hass, 4)

    assert len(host.clients) == 4  # nothing was parked: it went on dialling
    assert host.open_buses == 4  # and what the proxy held was not closed


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


async def test_a_command_does_not_dial_over_a_client_that_will_not_close(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is on every door. A button pressed ten times opens nothing."""
    coordinator, host = _wedged(hass, monkeypatch, behind=_MovedBackend)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    await _polls(coordinator, hass, 1)
    assert len(host.clients) == 1

    for _ in range(10):
        with pytest.raises(Exception):  # noqa: B017, PT011 - any refusal will do
            await coordinator.async_set_power(True)

    assert len(host.clients) == 1


async def test_a_command_refused_over_a_client_that_will_not_close_says_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the user is told is what is the matter.

    Nothing is dialled over a client that could be neither hung up nor
    closed. The lamp may be in perfect range; "out of range, try a Bluetooth
    proxy" is the wrong thing to say, and a proxy the wrong thing to buy.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_MovedBackend)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    await _polls(coordinator, hass, 1)
    assert coordinator._unreleased

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)

    assert err.value.translation_key == "link_not_released"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert len(host.clients) == 1


async def test_nothing_is_kept_of_a_client_that_was_let_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What was noted about a client goes when the client does.

    One client per poll tick for as long as Home Assistant runs: whatever is
    kept per client and never dropped is a leak of its own.
    """
    coordinator, host = _wedged(hass, monkeypatch)

    await _polls(coordinator, hass, 5)

    assert len(host.clients) == 5
    assert coordinator._backends == {}
    assert coordinator._unreleased == set()


async def test_a_client_with_nothing_known_behind_it_is_kept_too(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backend to look at, and a hang-up that failed: there is no telling.

    So it is treated as the worse case. A client that may hold a bus is not
    forgotten on the strength of not having been able to look.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=lambda _bus: None)

    await _polls(coordinator, hass, 6)

    assert len(host.clients) == 1


class _StuckBus(_Bus):
    """A bus that will not close when told to."""

    def disconnect(self) -> None:
        raise RuntimeError("busy")  # still open, whatever it is


async def test_a_bus_that_will_not_close_stops_the_dialling(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same when the bus is reached and refuses: kept, and not dialled over."""
    coordinator, host = _wedged(hass, monkeypatch)

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    lamp_of(coordinator).dials_through(_dial_a_stuck_one)

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


class _DownBus(_Bus):
    """A bus the daemon has already dropped, as dbus-fast reports one."""

    connected = False

    def __init__(self) -> None:
        super().__init__()
        self.closed = True
        self.asked = 0

    def disconnect(self) -> None:
        self.asked += 1


async def test_a_bus_that_is_already_down_is_left_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is closed twice, and a client whose bus is gone is not parked.

    dbus-fast does not raise when told to disconnect a socket that is already
    gone: it logs a warning with a traceback. With the system bus out of
    connections every client is in that state, so asking each one again would
    fill the log exactly when somebody is reading it.
    """
    coordinator, host = _wedged(hass, monkeypatch)
    buses: list[_DownBus] = []

    async def _dial_a_dead_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        bus = _DownBus()
        buses.append(bus)
        client._backend._bus = host.bus_of[client] = bus
        client.disconnect = AsyncMock(side_effect=OSError(9, "Bad file descriptor"))
        return client

    lamp_of(coordinator).dials_through(_dial_a_dead_one)

    await _polls(coordinator, hass, 4)

    assert len(host.clients) == 4  # none of them held against the lamp
    assert [bus.asked for bus in buses] == [0, 0, 0, 0]
    assert all(client._backend._bus is None for client in host.clients)


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


def test_bleak_still_calls_these_what_the_coordinator_calls_them() -> None:
    """The private names _close_bus reaches for, on a client bleak built itself.

    The test below sets them by hand, so a rename in bleak would pass it
    and leave one monitor task, one watcher or one bus per client behind.
    This asks bleak's own constructor.
    """
    backend = BleakClientBlueZDBus(
        "AA:BB:CC:DD:EE:FF", None, bluez={}, timeout=10.0, disconnected_callback=None
    )

    for name in ("_bus", "_is_connected", "_disconnect_monitor_event"):
        assert name in vars(backend), name
    assert "_remove_device_watcher" in vars(backend)  # what _cleanup_all drops
    assert callable(backend._cleanup_all)
    assert backend.is_connected is False  # a property over _bus and _is_connected


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


# --- A call still waiting its turn when the bus is closed under it ----------


class _BusyBus(_StubBus):
    """BlueZ with an earlier call on the characteristic still waiting.

    It turns every further read or write away with "in progress", and bleak
    answers that by sleeping ten milliseconds and asking again, for as long
    as it takes.
    """

    def __init__(self) -> None:
        super().__init__("error")
        self.closings = 0
        self.going_round = asyncio.Event()

    async def call(self, message: Any) -> Any:
        if message.member not in ("ReadValue", "WriteValue"):
            return await super().call(message)
        self.calls.append(message.member)
        if len(self.calls) > 1:
            self.going_round.set()  # turned away once, and back for more
        return SimpleNamespace(
            message_type=MessageType.ERROR,
            error_name=defs.BLUEZ_ERROR_IN_PROGRESS,
            body=["In Progress"],
        )

    def disconnect(self) -> None:
        super().disconnect()
        self.closings += 1


class _GattClient(_HaClient):
    """Home Assistant's wrapper, as far as a read and a write go as well."""

    @staticmethod
    def _characteristic(uuid: str) -> Any:
        path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/service0001/char0002"
        return SimpleNamespace(obj=(path, {}), uuid=uuid)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        assert self._backend is not None
        await self._backend.write_gatt_char(self._characteristic(uuid), data, response)

    async def read_gatt_char(self, uuid: str) -> bytearray:
        assert self._backend is not None
        return await self._backend.read_gatt_char(self._characteristic(uuid))


class _WorkingClient:
    """A client on a link that is up, as far as the coordinator asks of one."""

    def __init__(self) -> None:
        self.is_connected = True
        self.start_notify = AsyncMock()
        self.read_gatt_char = AsyncMock()
        self.write_gatt_char = AsyncMock()
        self.disconnect = AsyncMock()


def _on_a_busy_link(
    hass: HomeAssistant,
) -> tuple[GlowriumCoordinator, _GattClient, _BusyBus]:
    """Return a coordinator holding bleak's own client, on a link BlueZ is busy on."""
    coordinator = ScriptedLamp().coordinator(hass)
    bus = _BusyBus()
    backend, _ = _bleaks_own_client(bus)
    client = _GattClient(backend)
    coordinator._client = client
    return coordinator, client, bus


async def _turned_away(bus: _BusyBus, member: str) -> None:
    """Wait until BlueZ has turned ``member`` away twice: bleak is going round."""
    async with asyncio.timeout(1):
        await bus.going_round.wait()
    assert set(bus.calls) == {member}


def _bluez_reports_the_link_gone(
    coordinator: GlowriumCoordinator, client: _GattClient
) -> None:
    """Do what bleak does when BlueZ says the device is no longer connected.

    Its own handler (``on_connected_changed``, a closure inside ``connect()``):
    mark the client disconnected, release the monitor, tidy up, and call the
    disconnected callback - the coordinator's, which hangs the client up.
    """
    backend = client._backend
    assert backend is not None
    backend._is_connected = False
    assert backend._disconnect_monitor_event is not None
    backend._disconnect_monitor_event.set()
    backend._disconnect_monitor_event = None
    backend._cleanup_all()
    coordinator._async_on_disconnect(client)


@pytest.mark.parametrize(
    ("exchange", "member"),
    [
        ("the state request", "WriteValue"),
        ("the state read", "ReadValue"),
        ("the device-info read", "ReadValue"),
    ],
)
async def test_a_call_waiting_its_turn_when_the_link_drops_ends_as_a_lost_link(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    exchange: str,
    member: str,
) -> None:
    """Against bleak's real client: the bus is closed under a call going round.

    Seen on the G7's host, four times in thirty hours (2026-10-07): an error
    with a traceback in Home Assistant's log, "Task exception was never
    retrieved", a few milliseconds after a link dropped. BlueZ had been turning
    the state request away with "in progress" - an earlier write on that
    characteristic, abandoned at a deadline, was still waiting on a link that
    was going - and bleak was sleeping between two tries when the link was
    reported lost. The hang-up closed the client's bus, as it must, and bleak
    begins each try by asserting that it has one.

    That is a lost link like any other, and has to end like one: nothing comes
    out of the exchange, and the bus is closed once.
    """
    coordinator, client, bus = _on_a_busy_link(hass)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    exchanges = {
        "the state request": coordinator._async_prime,
        "the state read": lambda: coordinator._async_read_state(client),
        "the device-info read": lambda: coordinator._async_read_device_info(client),
    }

    waiting = asyncio.create_task(exchanges[exchange]())
    await _turned_away(bus, member)
    _bluez_reports_the_link_gone(coordinator, client)
    async with asyncio.timeout(1):
        await waiting  # ends, and with nothing to say about an assertion
    await hass.async_block_till_done()

    assert coordinator._client is None
    assert coordinator._primed_client is not client
    assert coordinator.device_info == {}
    # A link that was lost, and not a lamp that refused: nothing is counted
    # towards asking it no more.
    assert coordinator._state_request_failures == 0
    assert not coordinator._state_request_muted
    assert bus.closings == 1
    assert client._backend is not None
    assert client._backend._bus is None
    assert "hung up while a call on it was waiting" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_command_waiting_its_turn_when_the_link_drops_is_sent_on_a_new_link(
    hass: HomeAssistant,
) -> None:
    """The same moment, with a command in the write: it is tried again.

    A write that fails on a lost link is retried once on a new one. The
    assertion was not taken for a lost link, so it would have reached whoever
    pressed the button as an unknown error, with the retry never made.
    """
    coordinator, client, bus = _on_a_busy_link(hass)
    second = _WorkingClient()
    lamp_of(coordinator).dials_through(AsyncMock(return_value=second))

    command = asyncio.create_task(coordinator.async_set_power(True))
    await _turned_away(bus, "WriteValue")
    _bluez_reports_the_link_gone(coordinator, client)
    async with asyncio.timeout(1):
        await command  # delivered, and nothing raised
    await hass.async_block_till_done()

    second.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({KEY_POWER: True}), response=True
    )
    assert coordinator._client is second
    assert coordinator.state[KEY_POWER] is True
    assert bus.closings == 1


async def test_an_assertion_on_a_link_that_is_up_is_nobodys_lost_link(
    hass: HomeAssistant,
) -> None:
    """Only a client that has been hung up: any other assertion stays one.

    An assertion says that somebody was wrong - bleak, Home Assistant's
    wrapper, a test's own stand-in for the lamp - and taking every one of them
    for a link that dropped would bury it in a debug line about the radio.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = _WorkingClient()
    client.write_gatt_char.side_effect = AssertionError("not about the bus")
    client.read_gatt_char.side_effect = AssertionError("not about the bus")
    coordinator._client = client

    with pytest.raises(AssertionError, match="not about the bus"):
        await coordinator.async_set_power(True)
    with pytest.raises(AssertionError, match="not about the bus"):
        await coordinator._async_read_device_info(client)
    with pytest.raises(AssertionError, match="not about the bus"):
        await coordinator._async_prime()

    assert coordinator._client is client  # and nothing was let go of over it


_GATT_CALLS = frozenset(
    {
        "read_gatt_char",
        "read_gatt_descriptor",
        "start_notify",
        "stop_notify",
        "write_gatt_char",
        "write_gatt_descriptor",
    }
)


def test_every_gatt_call_is_made_where_a_closed_bus_is_a_lost_link() -> None:
    """Each call that goes to the lamp is made under ``_gatt_call``.

    The assertion comes out of whichever call happened to be waiting, so one
    call left outside brings the traceback back for that call alone. This
    reads the integration's source, so a call added later is seen here - and
    a guard given one client around a call made on another guards nothing.
    """
    package = Path(coordinator_module.__file__).parent
    bare: list[str] = []
    calls = 0
    for source in sorted(package.rglob("*.py")):
        guarded: set[int] = set()
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for block in ast.walk(tree):
            if not isinstance(block, ast.With):
                continue
            # The clients this block's guards are given, as they are written.
            clients = {
                ast.dump(item.context_expr.args[0])
                for item in block.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "_gatt_call"
                and item.context_expr.args
            }
            guarded.update(
                id(call)
                for call in ast.walk(block)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and ast.dump(call.func.value) in clients
            )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _GATT_CALLS
            ):
                calls += 1
                if id(node) not in guarded:
                    bare.append(f"{source.name}:{node.lineno} {node.func.attr}")

    assert bare == []
    assert calls >= 5  # the request, two reads, the command, the subscription


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


# --- A stack that will not hang up is a state, not an event -----------------


_STATE = bytearray.fromhex("a306f508184614f5")  # {power: on, brightness: 70, activated}


def _lamp(coordinator: GlowriumCoordinator, client: Any) -> AsyncMock:
    """Put a lamp behind ``client`` that answers the state request, as a G7 does.

    The request is a write of ids to ``NOTIFY_UUID``, and the answer is a
    notification carrying exactly those - on a real lamp, before the write has
    returned. Any other write is accepted.
    """

    async def _write(uuid: str, payload: bytes, **_kwargs: object) -> None:
        if uuid != NOTIFY_UUID:
            return
        answer = dict.fromkeys(bytes(payload), 0)
        answer |= {KEY_POWER: True, KEY_ACTIVATED: True}
        coordinator._on_notify(None, bytearray(cbor.encode(answer)))

    client.write_gatt_char = AsyncMock(side_effect=_write)
    return client.write_gatt_char


def _asked(client: Any) -> int:
    """Return how many times the lamp behind ``client`` was asked for its state."""
    return sum(
        1
        for call in client.write_gatt_char.await_args_list
        if call.args[0] == NOTIFY_UUID
    )


class _Clock:
    """The coordinator's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _on_a_clock(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[GlowriumCoordinator, _WedgedBlueZ, _Clock, list[float]]:
    """Return a coordinator that backs off, a clock, and the times it dialled."""
    coordinator, host = _wedged(hass, monkeypatch, backs_off=True)
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)
    dialled: list[float] = []

    async def _dial(*args: object, **kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        return await host.dial(*args, **kwargs)

    lamp_of(coordinator).dials_through(_dial)
    return coordinator, host, clock, dialled


async def _ticks(
    coordinator: GlowriumCoordinator, hass: HomeAssistant, clock: _Clock, count: int
) -> None:
    """Run ``count`` poll ticks thirty seconds apart on the coordinator's clock."""
    for _ in range(count):
        clock.now += 30
        coordinator._async_poll_reconnect(None)
        await asyncio.sleep(0.04)
    await hass.async_block_till_done()


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in pairwise(times)]


async def test_a_stack_that_will_not_hang_up_is_dialled_less_and_less(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three unanswered hang-ups in a row, and the poll stops hammering.

    Each dial into a wedged stack is handed the link BlueZ will not let go of,
    learns nothing, and costs the stack a disconnect it cannot honour. So the
    gap doubles, up to a ceiling - and the log says so once, in words that tell
    the owner what only the owner can do about it.
    """
    coordinator, _host, clock, dialled = _on_a_clock(hass, monkeypatch)

    await _ticks(coordinator, hass, clock, 44)

    assert _gaps(dialled) == [30, 30, 60, 120, 240, 300, 300]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "power-cycled" in warnings[0].getMessage()


async def test_one_slow_hang_up_is_not_a_wedged_stack(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BlueZ may just be slow. Two unanswered hang-ups change nothing."""
    coordinator, _host, clock, dialled = _on_a_clock(hass, monkeypatch)

    await _ticks(coordinator, hass, clock, 2)

    assert coordinator._dial_not_before == 0.0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    await _ticks(coordinator, hass, clock, 1)
    assert _gaps(dialled) == [30, 30]
    assert (
        len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    )  # the third


async def test_a_weak_link_is_not_mistaken_for_a_wedged_stack(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Links that answer nothing but hang up properly are just a bad signal.

    At the edge of range a link can die before the first read, again and
    again. BlueZ reports each one gone and every hang-up goes through, and
    that is the difference: nothing is wedged, so nothing backs off.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)
    host.released.set()  # BlueZ answers every Disconnect

    await _ticks(coordinator, hass, clock, 8)

    assert _gaps(dialled) == [30] * 7
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.parametrize(
    "answer",
    [
        OSError(9, "Bad file descriptor"),
        BleakError("[org.bluez.Error.Failed] Operation already in progress"),
    ],
    ids=["the bus is gone", "bluez says no"],
)
async def test_a_hang_up_that_was_answered_with_an_error_is_not_a_wedge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: Exception,
) -> None:
    """Only silence counts. An error is BlueZ answering, or the bus itself gone.

    With the system bus out of connections every hang-up ends in "Bad file
    descriptor". Nothing is wedged then, and telling the owner to power-cycle
    the adapter would be sending them to the wrong machine.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)

    async def _dial(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        client.disconnect = AsyncMock(side_effect=answer)
        return client

    lamp_of(coordinator).dials_through(_dial)

    await _ticks(coordinator, hass, clock, 6)

    assert _gaps(dialled) == [30] * 5
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_proxy_that_times_out_is_not_blamed_on_bluez(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning names BlueZ and its remedy, so it is for BlueZ's client only.

    A lamp behind a Bluetooth proxy has no BlueZ under it. A disconnect that
    times out there is the proxy's affair, and telling its owner to
    power-cycle the host's adapter would send them to the wrong machine.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_ProxyBackend, backs_off=True)
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)

    await _ticks(coordinator, hass, clock, 6)

    assert len(host.clients) == 6
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.parametrize("out_of_reach", ["bleak rearranged", "the bus refuses"])
async def test_a_stack_is_no_less_stuck_for_a_bus_that_cannot_be_closed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    out_of_reach: str,
) -> None:
    """BlueZ leaving disconnects unanswered is the fault, wherever the bus is.

    When the bus behind such a client cannot be closed either, the client is
    kept and nothing is dialled over it. That used to be all: the unanswered
    disconnects were only counted where the bus had been closed, so this owner
    had a stuck stack, no warning and no repair - and a command that failed
    with words about a connection that would not close.
    """
    rearranged = out_of_reach == "bleak rearranged"
    coordinator, host = _wedged(
        hass,
        monkeypatch,
        behind=_MovedBackend if rearranged else _Backend,
        backs_off=True,
    )
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    if not rearranged:
        lamp_of(coordinator).dials_through(_dial_a_stuck_one)

    await _ticks(coordinator, hass, clock, 4)

    assert len(host.clients) == 1  # kept, and nothing dialled over it
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "power-cycled" in warnings[0].getMessage()
    assert _stack_issue(hass) is not None


async def test_a_client_with_no_backend_on_record_is_not_blamed_on_bluez(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kept, since there may be a bus behind it - and nobody's to call BlueZ's.

    The warning and the repair name BlueZ and what clears it. With no backend
    on record there is no telling whose client it is that went silent, and
    sending its owner to power-cycle an adapter would be a guess.
    """
    coordinator, host = _wedged(
        hass, monkeypatch, behind=lambda _bus: None, backs_off=True
    )
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)

    await _ticks(coordinator, hass, clock, 6)

    assert len(host.clients) == 1  # kept, and nothing dialled over it
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert _stack_issue(hass) is None


async def test_unanswered_hang_ups_count_only_in_a_row(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two slow hang-ups, one that goes through, one more slow: no wedge.

    A hang-up BlueZ answers breaks the run. And so does the lamp answering
    anything - neither is how a stack behaves that is holding on to a link.
    """
    coordinator, host, clock, _dialled = _on_a_clock(hass, monkeypatch)

    await _ticks(coordinator, hass, clock, 2)
    assert coordinator._stuck_hang_ups == 2
    host.released.set()  # this one BlueZ answers
    await _ticks(coordinator, hass, clock, 1)
    host.released.clear()
    await _ticks(coordinator, hass, clock, 1)

    assert coordinator._stuck_hang_ups == 1
    assert coordinator._dial_not_before == 0.0

    await _ticks(coordinator, hass, clock, 1)
    assert coordinator._stuck_hang_ups == 2
    coordinator._on_notify(None, _STATE)  # the lamp says something
    await _ticks(coordinator, hass, clock, 1)

    assert coordinator._stuck_hang_ups == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_wedge_left_alone_for_days_does_not_overflow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap doubles per unanswered hang-up, and the count has no ceiling.

    One dial every five minutes reaches a thousand of them in three and a
    half days, and two to that power does not fit a float: the hang-up
    would end in OverflowError, the backoff would stop moving, and the
    poll would be back to dialling a wedged stack on every tick.
    """
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    coordinator._stuck_hang_ups = 5000

    coordinator._note_stuck_hang_up()

    assert coordinator._dial_not_before == (
        clock.now + coordinator_module._STACK_FAULT_BACKOFF_MAX
    )


async def test_a_fault_ends_with_the_lamp_and_not_with_a_hang_up(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once announced, the episode is closed by the lamp answering - and said.

    BlueZ may start answering disconnects again while the lamp still says
    nothing, and stop again. That is one episode, not two. If an answered
    hang-up quietly cleared the count, the next run of unanswered ones would
    be announced as a second fault in the middle of the first - with the
    backoff starting over - and the log would read as two faults and one end.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)
    coordinator._activation_checked = True
    await _ticks(coordinator, hass, clock, 3)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    host.released.set()  # hang-ups go through now; the lamp still answers nothing
    await _ticks(coordinator, hass, clock, 6)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    host.released.clear()  # ...and then they stop going through again
    await _ticks(coordinator, hass, clock, 24)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert coordinator._stuck_hang_ups > coordinator_module._STACK_FAULT_AFTER
    host.released.set()

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        _lamp(coordinator, client)
        client.read_gatt_char = AsyncMock(return_value=bytearray(b"brand:x;;"))
        return client

    lamp_of(coordinator).dials_through(_healthy)
    await _ticks(coordinator, hass, clock, 12)

    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(said) == 2
    assert "answers again" in said[1]


def _stack_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the repair raised for the wedged stack, if one stands."""
    raised = [
        issue
        for (domain, _id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    ]
    assert len(raised) <= 1
    return raised[0] if raised else None


async def test_a_wedged_stack_is_put_in_front_of_the_user(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fault is raised as a repair, not left for whoever reads the log.

    Nothing the integration does ends it; somebody has to reset the adapter.
    A warning in the log says so to the person who goes looking. A repair
    says it on the dashboard, names the lamp, and carries what to do - and
    it is raised when the warning is, not before: two unanswered hang-ups
    are a slow stack, not a wedged one.
    """
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)

    await _ticks(coordinator, hass, clock, 2)
    assert _stack_issue(hass) is None

    await _ticks(coordinator, hass, clock, 1)
    issue = _stack_issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.is_persistent is False  # the next start finds out for itself
    assert issue.translation_key == "bluetooth_stack_stuck"
    assert issue.translation_placeholders == {"name": "Glowrium-G7", "count": "3"}
    assert issue.learn_more_url.endswith("#troubleshooting")


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        (
            "Glowrium-![x](http://evil.example/p.png)<img src=x>",
            "Glowrium- x http evil example p png img src x",
        ),
        ("Glowrium [here](javascript:alert(1)) `rm` *now*", None),
        ("Glowrium www.evil.example", "Glowrium www evil example"),
        ("![]()<>`*#|~", "the lamp"),
        ("Лампа над столом G7", "Лампа над столом G7"),
        ("Glowrium-G7_DDEEFF", "Glowrium-G7_DDEEFF"),
        # Picked from the list of discovered lamps: titled with its address.
        ("Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)", "Glowrium-G7_DDEEFF"),
        (
            "Glowrium-G7_DDEEFF (11:22:33:44:55:66)",  # not this lamp's
            "Glowrium-G7_DDEEFF 11 22 33 44 55 66",
        ),
    ],
)
async def test_the_repair_shows_the_lamps_name_as_text_and_nothing_more(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    shown: str | None,
) -> None:
    """The lamp's name comes off the air, and a repair is rendered as Markdown.

    Whatever advertises a name beginning with "Glowrium" can be set up, and
    its name becomes the entry's title. Put into the repair as it is, a name
    could carry a link or an image - which the dashboard would fetch - into
    a message the user has every reason to trust. So only letters, digits,
    spaces, dashes and underscores go in: enough to recognise the lamp by, in
    any script, and nothing Markdown or HTML acts on - not even a dot, which
    is all it takes to make an address clickable.
    """
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    coordinator.name = name

    await _ticks(coordinator, hass, clock, 3)

    issue = _stack_issue(hass)
    assert issue is not None
    in_repair = issue.translation_placeholders["name"]
    assert not set(in_repair) & set("[]()!<>`*#|~\\:/=\"'.")
    assert in_repair.strip() == in_repair
    assert in_repair
    if shown is not None:
        assert in_repair == shown


async def test_the_repair_is_filed_under_the_entry_and_not_under_the_address(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair's id leaves the host, so the lamp's address must not be in it.

    Home Assistant puts the ids of an integration's open repairs into its
    diagnostics download - the file this integration tells people to attach
    to public issues, and takes care to keep the address out of. The config
    entry's id tells two lamps apart just as well and names neither.
    """
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    coordinator._entry = SimpleNamespace(
        entry_id="01JENTRY",
        async_create_background_task=lambda _hass, coro, name: hass.async_create_task(
            coro, name
        ),
    )

    await _ticks(coordinator, hass, clock, 3)

    issue = _stack_issue(hass)
    assert issue is not None
    assert issue.issue_id == "bluetooth_stack_stuck_01JENTRY"
    for part in ("AA:BB:CC:DD:EE:FF", "aa:bb", "DDEEFF", "EE:FF"):
        assert part not in json.dumps(issue.to_json())

    await coordinator.async_stop()
    assert _stack_issue(hass) is None  # taken down under the same id


async def test_the_repair_shows_no_more_of_a_name_than_it_takes_to_know_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name is cut at a length that is still a name, not a paragraph."""
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    coordinator.name = "Glowrium " + "x" * 200

    await _ticks(coordinator, hass, clock, 3)

    issue = _stack_issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["name"] == "Glowrium " + "x" * 39


async def test_the_repair_goes_when_the_lamp_answers_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair left standing after the fault is over would be a false alarm.

    It is taken down by the same thing that ends the episode: the lamp saying
    something. BlueZ answering disconnects again is not that.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)
    coordinator._activation_checked = True
    await _ticks(coordinator, hass, clock, 3)
    assert _stack_issue(hass) is not None

    host.released.set()  # hang-ups go through now; the lamp still answers nothing
    await _ticks(coordinator, hass, clock, 6)
    assert _stack_issue(hass) is not None

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        _lamp(coordinator, client)
        client.read_gatt_char = AsyncMock(return_value=bytearray(b"brand:x;;"))
        return client

    lamp_of(coordinator).dials_through(_healthy)
    await _ticks(coordinator, hass, clock, 12)

    assert _stack_issue(hass) is None


async def test_the_repair_goes_with_the_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An integration that has been unloaded is not watching the stack any more."""
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    await _ticks(coordinator, hass, clock, 3)
    assert _stack_issue(hass) is not None

    await coordinator.async_stop()

    assert _stack_issue(hass) is None


async def test_a_hang_up_that_outlives_the_entry_raises_no_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hang-up is given longer than an unload waits for it, and may be the third.

    Two were left unanswered and the third is in flight when the entry is
    unloaded; it runs out afterwards, on a coordinator that has stopped
    watching. A repair raised then is one nothing takes down: the coordinator
    that follows starts a count of its own, after a removal there is none, and
    the repair would go on saying that it goes away by itself. The episode is
    not announced in the log either - nobody is left to say when it is over.
    """
    coordinator, _host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    await _ticks(coordinator, hass, clock, 2)
    assert coordinator._stuck_hang_ups == 2

    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.3)
    clock.now += 30
    coordinator._async_poll_reconnect(None)
    await asyncio.sleep(0.05)  # dialled, asked, dropped: the hang-up is in flight

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        await coordinator.async_stop()
        assert _stack_issue(hass) is None
        await asyncio.sleep(0.5)  # ...and it runs out, unanswered
        await hass.async_block_till_done()

    assert coordinator._stuck_hang_ups == 3  # counted, as any other
    assert _stack_issue(hass) is None
    assert "BlueZ has left" not in caplog.text


async def test_the_hang_up_of_the_unload_itself_raises_no_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The third unanswered hang-up can be the one the unload makes.

    The entry is unloaded with a link still held, after two hang-ups that got
    no answer. Letting go of that link is the third, and it is the unload that
    asked for it.
    """
    coordinator, host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    await _ticks(coordinator, hass, clock, 2)
    assert coordinator._stuck_hang_ups == 2
    client = await host.dial()
    coordinator._client = client
    coordinator._backends[client] = client._backend

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        await coordinator.async_stop()
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()

    assert coordinator._stuck_hang_ups == 3
    assert _stack_issue(hass) is None
    assert "BlueZ has left" not in caplog.text


@pytest.mark.parametrize("announced_before_it_stopped", [False, True])
async def test_a_stopped_coordinator_ends_no_episode_and_takes_down_no_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    announced_before_it_stopped: bool,
) -> None:
    """An episode is ended by the coordinator that announced it, and by no other.

    The count of a stopped coordinator can reach three without a word, and
    its link can still say something afterwards - a notification on the way
    in while it is hung up, a write that was in flight. That is not the end
    of an episode nobody announced. And the repair filed under the entry's
    id by now belongs to the coordinator that took over after a reload: it
    is not this one's to take down. Nor is it if this coordinator did
    announce an episode while it was watching: stopping closed it.
    """
    coordinator, host, clock, _dialled = _on_a_clock(hass, monkeypatch)
    entry = SimpleNamespace(
        entry_id="01JENTRY",
        async_create_background_task=lambda _hass, coro, name: hass.async_create_task(
            coro, name
        ),
    )
    coordinator._entry = entry
    if announced_before_it_stopped:
        await _ticks(coordinator, hass, clock, 3)
        assert _stack_issue(hass) is not None
    else:
        await _ticks(coordinator, hass, clock, 2)
        client = await host.dial()
        coordinator._client = client
        coordinator._backends[client] = client._backend
    await coordinator.async_stop()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert coordinator._stuck_hang_ups == 3
    assert _stack_issue(hass) is None

    successor, _host, successor_clock, _ = _on_a_clock(hass, monkeypatch)
    successor._entry = entry
    await _ticks(successor, hass, successor_clock, 3)
    assert _stack_issue(hass) is not None

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        coordinator._note_answer()

    assert _stack_issue(hass) is not None  # the successor's, and still standing
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    await successor.async_stop()


async def test_a_wedged_stack_is_survived_without_home_assistant() -> None:
    """The bench has no dashboard to raise a repair on, and counts all the same."""
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")

    for _ in range(coordinator_module._STACK_FAULT_AFTER):
        coordinator._note_stuck_hang_up()
    coordinator._note_answer()

    assert coordinator._stuck_hang_ups == 0


async def test_a_command_is_not_held_back_by_the_backoff(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backoff is for the poll. Somebody pressing a button gets a dial."""
    coordinator, _host, clock, dialled = _on_a_clock(hass, monkeypatch)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    await _ticks(coordinator, hass, clock, 3)
    assert clock.now < coordinator._dial_not_before
    before = len(dialled)

    with pytest.raises(Exception):  # noqa: B017, PT011 - the stack is still wedged
        await coordinator.async_set_power(True)

    assert len(dialled) > before


async def test_an_advertisement_does_not_dial_through_the_backoff(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lamp advertises all through a wedge, about once a second.

    Each advertisement asks for a reconnect when there is no link. If that
    door stayed open, the backoff would be a thirty-second poll standing
    politely aside for a one-second one.
    """
    coordinator, _host, clock, dialled = _on_a_clock(hass, monkeypatch)
    await _ticks(coordinator, hass, clock, 3)
    before = len(dialled)

    coordinator._async_on_advertisement(object(), object())
    await asyncio.sleep(0.04)
    await hass.async_block_till_done()

    assert len(dialled) == before


async def test_the_first_answer_ends_the_backoff(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the lamp answers, the stack is believed again - at once, and aloud.

    After the adapter has been reset the next dial is a real one. Its first
    read ends the episode: the log gets its closing line, and a link lost
    afterwards is redialled on the next tick, not minutes later.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)
    coordinator._activation_checked = True
    await _ticks(coordinator, hass, clock, 3)
    assert clock.now < coordinator._dial_not_before

    # The adapter is power-cycled: BlueZ answers, and the lamp does.
    host.released.set()

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        _lamp(coordinator, client)
        client.read_gatt_char = AsyncMock(return_value=bytearray(b"brand:x;;"))
        return client

    lamp_of(coordinator).dials_through(_healthy)
    await _ticks(coordinator, hass, clock, 2)

    assert coordinator._is_connected
    assert coordinator._dial_not_before == 0.0
    assert coordinator._stuck_hang_ups == 0  # the next slow one starts from none
    # Said at the level the episode was announced at, or whoever read the
    # warning never learns that it is over.
    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(said) == 2
    assert "answers again" in said[1]

    held = coordinator._client
    coordinator._async_on_disconnect(held)  # the lamp drops it, as it does
    already = len(dialled)
    await _ticks(coordinator, hass, clock, 1)
    assert len(dialled) == already + 1


# --- A link that died without BlueZ noticing --------------------------------


def _holding(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[GlowriumCoordinator, AsyncMock, _Clock]:
    """Return a coordinator holding a primed link to a lamp that answers."""
    coordinator = ScriptedLamp().coordinator(hass)
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)
    client = AsyncMock()
    client.is_connected = True
    client._backend = None
    client.read_gatt_char = AsyncMock(return_value=_STATE)
    _lamp(coordinator, client)
    coordinator._client = coordinator._primed_client = client
    coordinator._activation_checked = True
    coordinator._last_answer = clock.now
    return coordinator, client, clock


async def _tick(coordinator: GlowriumCoordinator, hass: HomeAssistant) -> None:
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()


async def test_a_link_called_not_connected_and_never_dropped_is_let_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BlueZ says "not connected" and then says nothing more, for ever.

    Seen on the real host at 16:04:56, when every lamp was still read first:
    the read answered, the request failed with "Not connected", and the report
    that the link had dropped - due two seconds later, as on every other cycle
    that day - never came. The client went on reading as connected, so nothing
    dialled again. Five hours, until somebody pressed a button.
    """
    coordinator, client, clock = _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1  # a lamp that is read first
    client.write_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))

    assert await coordinator._request_state(client)  # the read did answer
    clock.now += 5
    await _tick(coordinator, hass)  # inside the grace BlueZ is given
    assert coordinator._client is client

    clock.now += 30
    await _tick(coordinator, hass)

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_lamp_that_speaks_again_is_not_let_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request can fail on a link that is perfectly alive.

    Not every failure that is not a refusal means the link has gone: a lamp
    has answered a request with "Unlikely Error" and gone on notifying. What
    it says afterwards settles it, whatever BlueZ called the link before.
    """
    coordinator, client, clock = _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("GATT Protocol Error: Unlikely Error")
    )
    await coordinator._request_state(client)

    clock.now += 5
    coordinator._on_notify(None, _STATE)  # and it is still talking
    clock.now += 30
    await _tick(coordinator, hass)

    assert coordinator._client is client
    client.disconnect.assert_not_awaited()


async def test_a_link_bluez_did_report_dropped_is_not_hung_up_twice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case stays as it was: BlueZ reports, and that is the end of it."""
    coordinator, client, clock = _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    client.write_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))
    lamp_of(coordinator).dials_through(AsyncMock())
    await coordinator._request_state(client)
    assert coordinator._lost is not None

    coordinator._async_on_disconnect(client)  # two seconds later, as usual
    await hass.async_block_till_done()
    fresh = AsyncMock()
    fresh.is_connected = True
    fresh._backend = None
    coordinator._client = coordinator._primed_client = fresh
    clock.now += 60
    await _tick(coordinator, hass)

    client.disconnect.assert_awaited_once()
    fresh.disconnect.assert_not_awaited()  # the note was about the old client
    assert coordinator._client is fresh


async def test_a_silent_link_is_asked_whether_it_is_still_there(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held link that has said nothing for five minutes is asked for its state.

    The lamp only speaks when something changes, so a dead link and an idle
    one look the same from here. Asking tells them apart, and the answer is
    the lamp's state, which is worth having anyway. Asked, not read: a read
    would end the very link it was checking.
    """
    coordinator, client, clock = _holding(hass, monkeypatch)

    clock.now += coordinator_module._PROBE_INTERVAL - 1
    await _tick(coordinator, hass)
    assert _asked(client) == 0  # not before its time

    clock.now += 1
    await _tick(coordinator, hass)

    assert _asked(client) == 1
    client.read_gatt_char.assert_not_awaited()
    assert coordinator._client is client
    assert coordinator.state[KEY_POWER] is True  # and the answer was taken in
    assert coordinator._last_answer == clock.now

    await _tick(coordinator, hass)  # it has just answered
    assert _asked(client) == 1

    clock.now += coordinator_module._PROBE_INTERVAL  # and silent again since
    await _tick(coordinator, hass)
    assert _asked(client) == 2


async def test_a_silent_link_that_does_not_answer_is_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And one that answers nothing is hung up, for the poll to rebuild."""
    coordinator, client, clock = _holding(hass, monkeypatch)
    client.write_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))
    client.read_gatt_char = AsyncMock(side_effect=BleakError(_NOT_CONNECTED))

    clock.now += coordinator_module._PROBE_INTERVAL
    await _tick(coordinator, hass)

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_question_that_is_never_answered_drops_the_link_too(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link that keeps the question waiting is as dead as one that says so.

    BlueZ can leave a call without any answer. The deadline then ends the
    wait, and that has to count as the link's answer: left as a mere log
    line, the same question would be put on every poll tick for ever, each
    time holding the lock for as long as the deadline allows.
    """
    monkeypatch.setattr(coordinator_module, "_ASK_TIMEOUT", 0.05)
    coordinator, client, clock = _holding(hass, monkeypatch)

    async def _never(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    client.write_gatt_char = AsyncMock(side_effect=_never)

    clock.now += coordinator_module._PROBE_INTERVAL
    async with asyncio.timeout(2):
        await _tick(coordinator, hass)

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_question_that_could_not_be_put_proves_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not getting the lock in time says nothing about the link.

    A command may hold it for its whole budget. The link is fine then, and
    the question simply waits for the next tick.
    """
    monkeypatch.setattr(coordinator_module, "_ASK_TIMEOUT", 0.05)
    coordinator, client, clock = _holding(hass, monkeypatch)
    await coordinator._lock.acquire()  # somebody is at work on the link

    clock.now += coordinator_module._PROBE_INTERVAL
    await _tick(coordinator, hass)

    assert coordinator._client is client
    assert _asked(client) == 0
    client.disconnect.assert_not_awaited()
    coordinator._lock.release()


async def test_a_link_that_is_talking_is_not_asked(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notification is an answer. So is a command that went through."""
    coordinator, client, clock = _holding(hass, monkeypatch)

    clock.now += coordinator_module._PROBE_INTERVAL - 10
    coordinator._on_notify(None, _STATE)  # the lamp reports a change
    clock.now += 20
    await _tick(coordinator, hass)
    assert _asked(client) == 0

    clock.now += coordinator_module._PROBE_INTERVAL - 10
    await coordinator.async_set_power(True)  # a command, answered
    clock.now += 20
    await _tick(coordinator, hass)
    assert _asked(client) == 0


async def test_a_refusal_is_not_taken_for_a_link_that_is_going(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp that says no has answered; nothing about it is waiting to drop."""
    coordinator, client, clock = _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("Insufficient authorization (8)")
    )

    await coordinator._request_state(client)
    clock.now += 60
    await _tick(coordinator, hass)

    assert coordinator._lost is None
    assert coordinator._client is client
    client.disconnect.assert_not_awaited()


async def test_two_ticks_do_not_ask_the_same_question_twice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question still waiting for the lock when the answer comes is dropped.

    The poll does not know that the last tick's question is still queued, and
    asks again. The second one looks, once it has the lock, at whether the
    lamp has answered meanwhile.
    """
    coordinator, client, clock = _holding(hass, monkeypatch)
    answer = client.write_gatt_char.side_effect

    async def _in_a_moment(*args: object, **kwargs: object) -> None:
        # Long enough for the next tick to queue up behind this one: Home
        # Assistant starts a task at once, and an answer that came inside the
        # call would be there before the second tick had looked.
        await asyncio.sleep(0.01)
        await answer(*args, **kwargs)

    client.write_gatt_char = AsyncMock(side_effect=_in_a_moment)
    clock.now += coordinator_module._PROBE_INTERVAL

    coordinator._async_poll_reconnect(None)
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()

    assert _asked(client) == 1
