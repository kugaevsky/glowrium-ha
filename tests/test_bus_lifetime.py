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
from itertools import pairwise
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.exc import BleakError
from dbus_fast import MessageType
from homeassistant.core import HomeAssistant
import pytest

from custom_components.glowrium import cbor, coordinator as coordinator_module
from custom_components.glowrium.const import KEY_ACTIVATED, KEY_POWER, NOTIFY_UUID
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
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    host = _WedgedBlueZ(behind)
    monkeypatch.setattr(coordinator_module, "establish_connection", host.dial)
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.01)
    if not backs_off:
        monkeypatch.setattr(coordinator_module, "_STACK_FAULT_AFTER", 10**6)
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

    monkeypatch.setattr(coordinator_module, "establish_connection", _dial_a_dead_one)

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

    monkeypatch.setattr(coordinator_module, "establish_connection", _dial)
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

    monkeypatch.setattr(coordinator_module, "establish_connection", _dial)

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
    nothing. If that quietly cleared the count, the lamp's eventual answer
    would find no episode to close, and the warning in the log would be
    left standing with nothing after it.
    """
    coordinator, host, clock, dialled = _on_a_clock(hass, monkeypatch)
    coordinator._activation_checked = True
    await _ticks(coordinator, hass, clock, 3)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    host.released.set()  # hang-ups go through now; the lamp still answers nothing
    await _ticks(coordinator, hass, clock, 6)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        _lamp(coordinator, client)
        client.read_gatt_char = AsyncMock(return_value=bytearray(b"brand:x;;"))
        return client

    monkeypatch.setattr(coordinator_module, "establish_connection", _healthy)
    await _ticks(coordinator, hass, clock, 12)

    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(said) == 2
    assert "answers again" in said[1]


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

    monkeypatch.setattr(coordinator_module, "establish_connection", _healthy)
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
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
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
    monkeypatch.setattr(coordinator_module, "establish_connection", AsyncMock())
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
    monkeypatch.setattr(coordinator_module, "_CONNECT_TIMEOUT", 0.05)
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
    monkeypatch.setattr(coordinator_module, "_CONNECT_TIMEOUT", 0.05)
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
