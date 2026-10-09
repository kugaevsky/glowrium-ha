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
from weakref import WeakKeyDictionary

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.exc import BleakError
from dbus_fast import MessageType
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.glowrium import (
    cbor,
    coordinator as coordinator_module,
    link as link_module,
)
from custom_components.glowrium.const import (
    DOMAIN,
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_POWER,
    NOTIFY_UUID,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator

from .lamp import (
    LampLink,
    ScriptedLamp,
    lamp_of,
    link_of,
    nothing_heard,
    taken_bare,
    turn_over,
)

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


def _a_wedged_stack(
    monkeypatch: pytest.MonkeyPatch, behind: Any, *, backs_off: bool
) -> tuple[_WedgedBlueZ, ScriptedLamp]:
    """Return a wedged stack, and the lamp whose dial goes to it.

    Unless ``backs_off``, whoever dials it goes on dialling at every poll tick
    however often BlueZ fails to hang up: most tests here are about what each
    of those dials leaves behind, and want as many of them as there are ticks.
    """
    host = _WedgedBlueZ(behind)
    lamp = ScriptedLamp()
    lamp.dials_through(host.dial)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.01)
    if not backs_off:
        monkeypatch.setattr(link_module, "_STACK_FAULT_AFTER", 10**6)
    return host, lamp


def _wedged(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    behind: Any = _Backend,
    *,
    backs_off: bool = False,
) -> tuple[GlowriumCoordinator, _WedgedBlueZ]:
    """Return a coordinator facing a wedged stack (see ``_a_wedged_stack``).

    For a test that needs the device half as well: what the user is told, or
    the repair. The link alone is had from ``_wedged_link``.
    """
    host, lamp = _a_wedged_stack(monkeypatch, behind, backs_off=backs_off)
    return lamp.coordinator(hass), host


# The lamp a bare link was built to dial, for a test that sends its dials
# elsewhere part-way through (as ``lamp_of`` finds a coordinator's).
_LAMP_BEHIND: WeakKeyDictionary[link_module.Link, ScriptedLamp] = WeakKeyDictionary()


def _lamp_behind(link: link_module.Link) -> ScriptedLamp:
    """Return the lamp ``link`` dials, as ``_wedged_link`` built it."""
    return _LAMP_BEHIND[link]


def _wedged_link(
    monkeypatch: pytest.MonkeyPatch,
    behind: Any = _Backend,
    *,
    backs_off: bool = False,
    stack_fault: Any = None,
) -> tuple[link_module.Link, _WedgedBlueZ]:
    """Return a link facing a wedged stack, with nothing behind the link.

    It asks the lamp for its state on every link it takes (``_asks``), as
    the device half's first exchange does, and learns nothing from a wedged
    one. ``stack_fault`` is what it tells of a stack that will not hang up,
    for a test that listens.
    """
    host, lamp = _a_wedged_stack(monkeypatch, behind, backs_off=backs_off)
    link = _a_link(lamp.dial, greet=_asks, stack_fault=stack_fault)
    _LAMP_BEHIND[link] = lamp
    return link, host


async def _polls(
    link: link_module.Link, count: int, *, hass: HomeAssistant | None = None
) -> None:
    """Run ``count`` poll ticks, each given time to reach the hang-up's ceiling.

    ``hass`` is where the link's work runs when it is a coordinator's, and
    then what is waited for at the end; a bare link's is waited for by itself.
    """
    for _ in range(count):
        link.tick()
        await asyncio.sleep(0.04)
    await _done(link, hass)


# What a bare link has set off and not finished: a connect, a first
# exchange, a hang-up. Home Assistant keeps a coordinator's tasks and
# ``async_block_till_done`` waits for them; a link built by ``_a_link`` has
# only this.
_SET_OFF: WeakKeyDictionary[link_module.Link, set[asyncio.Task[None]]] = (
    WeakKeyDictionary()
)


async def _settled(link: link_module.Link) -> None:
    """Wait for what a bare ``link`` set off, and for what that set off in turn.

    Waited for, not gathered: a hang-up a test cancelled is finished too, and
    is nobody's error here.
    """
    while pending := {task for task in _SET_OFF[link] if not task.done()}:
        await asyncio.wait(pending)


async def _done(link: link_module.Link, hass: HomeAssistant | None) -> None:
    """Wait for what ``link`` set off: on ``hass`` where it runs there."""
    if hass is None:
        await _settled(link)
    else:
        await hass.async_block_till_done()


def _a_link(  # noqa: PLR0913 - what a link is handed, each by its name
    dial: Any = None,
    unclosed: link_module.Unclosed | None = None,
    reach_changed: Any = None,
    *,
    greet: link_module.Talk | None = None,
    probe: link_module.Talk | None = None,
    stack_fault: Any = None,
) -> link_module.Link:
    """Return a link with no coordinator and no Home Assistant behind it.

    What it is handed does nothing, but for what a test hands it: a dial, a
    holder of what would not close, something to tell of its reach, what to
    say in the first exchange or to a silent link, and what to tell of a
    stack that will not hang up. What it sets off is kept for ``_settled``.
    """
    set_off: set[asyncio.Task[None]] = set()

    def _on_the_loop(coro: Any, name: str) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        set_off.add(task)
        task.add_done_callback(set_off.discard)
        return task

    async def _says_nothing(_turn: link_module.Turn) -> None:
        return None

    link = link_module.Link(
        "AA:BB:CC:DD:EE:FF",
        dial or AsyncMock(),
        notify_uuid=NOTIFY_UUID,
        heard=lambda _characteristic, _data: None,
        greet=greet or _says_nothing,
        probe=probe or _says_nothing,
        reach_changed=reach_changed or (lambda: None),
        stack_fault=stack_fault or (lambda _count: None),
        spawn=_on_the_loop,
        run_lasting=_on_the_loop,
        unclosed=unclosed,
    )
    _SET_OFF[link] = set_off
    return link


# The state request, as far as the link sees it: a write of ids to
# NOTIFY_UUID. Which ids is the device half's business.
_REQUEST = bytes([KEY_POWER])


async def _asks(turn: link_module.Turn) -> None:
    """Ask the lamp for its state on ``turn``, as the device half's talks do.

    Acknowledged, the lamp has answered. Lost, nothing was learnt and the
    turn ends unanswered - and the link is not put in doubt here: that is
    the device half's judgement, made in one place of its own.
    """
    try:
        await turn.write(NOTIFY_UUID, _REQUEST)
    except link_module.LinkLostError:
        return
    turn.answered()


async def _a_command(turn: link_module.Turn) -> None:
    """Say a command on ``turn``: one write, which is all the link sees of one."""
    await turn.write(WRITE_UUID, b"\xa0")


async def _unvouched() -> bool:
    """Vouch for nothing: the lamp reported none of what a failed command set."""
    return False


async def test_a_wedged_bluez_does_not_cost_a_connection_per_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poll after poll against a stack that never hangs up: the count stays put.

    This is the whole incident in one number. Whatever the coordinator does
    about a hang-up that cannot finish, it may not leave open a connection it
    has no way of closing, and then go and open another.
    """
    link, host = _wedged_link(monkeypatch)

    await _polls(link, 20)

    assert len(host.clients) > 1  # it did go on dialling
    assert host.open_buses <= 1


async def test_a_wedged_bluez_does_not_cost_a_connection_per_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same by the other door: a user pressing the button again and again."""
    link, host = _wedged_link(monkeypatch)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)

    for _ in range(10):
        with pytest.raises(link_module.LinkLostError):
            await link.send(_a_command, vouch=_unvouched)
        await asyncio.sleep(0.04)
    await _settled(link)

    assert host.open_buses <= 1


async def test_the_bus_is_found_even_after_the_wrapper_let_go_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is behind a client is noted when it is taken, not when it is dropped.

    Home Assistant's wrapper forgets its backend when it gives a link up for
    lost - "the link leaks to BlueZ", in its own words - and by then the only
    way to the bus is the reference taken at the start.
    """
    link, host = _wedged_link(monkeypatch)

    async def _dial_then_forget(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()

        async def _wrapper_gives_up(*_a: object, **_k: object) -> None:
            client._backend = None

        client.start_notify = AsyncMock(side_effect=_wrapper_gives_up)
        return client

    _lamp_behind(link).dials_through(_dial_then_forget)

    await _polls(link, 5)

    assert len(host.clients) == 5  # closed each time, so it went on dialling
    assert host.open_buses == 0


async def test_a_client_nothing_was_noted_of_still_has_its_bus_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no note of a client, what is behind it is asked of the client.

    What is behind a client the link dials is noted when it is taken, and of
    one that would not close by the holder of those. A client that comes to
    the hang-up with neither note - hung up a second time, or by something
    that did not dial it through the link - has a bus behind it all the
    same, and the hang-up is the one place that ever closes it.
    """
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.01)
    link, host = _a_link(), _WedgedBlueZ()
    client = await host.dial()  # not through the link: nothing is noted of it

    await link.hang_up(client)

    assert host.open_buses == 0
    assert not link.unclosed


class _ProxyBackend:
    """A backend with no D-Bus connection of its own, as a Bluetooth proxy's."""

    def __init__(self, _bus: _Bus) -> None:
        pass


async def test_a_backend_with_no_bus_is_not_held_against_the_lamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy's client has nothing to close, so a failed hang-up parks nothing.

    The bound below is for connections to the system bus. Refusing to dial
    because a client that never had one would not disconnect only takes the
    lamp away.
    """
    link, host = _wedged_link(monkeypatch, behind=_ProxyBackend)

    await _polls(link, 4)

    assert len(host.clients) == 4


class _ProxyWithABus:
    """A proxy's backend that keeps something of its own under bleak's name."""

    def __init__(self, bus: _Bus) -> None:
        self._bus = bus


async def test_a_backend_that_is_not_bluezs_is_left_alone_whatever_it_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whose client it is is told by where its class lives, not by what it holds.

    The reach for the bus goes through bleak's private attributes, and only
    BlueZ's client is known to keep a connection to the system bus there.
    Another backend's attribute of the same name is its own affair: it is not
    closed for it, and a hang-up that fails behind it parks nothing.
    """
    link, host = _wedged_link(monkeypatch, behind=_ProxyWithABus)

    await _polls(link, 4)

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If bleak moves its internals, the count is still bounded - at one.

    Closing a client's bus goes through bleak's private attributes, and those
    can change under us. When a hang-up fails and the bus cannot be reached,
    the client is kept and nothing more is dialled until it has disconnected:
    the lamp is lost for that while, the system bus is not.
    """
    link, host = _wedged_link(monkeypatch, behind=_MovedBackend)

    await _polls(link, 6)

    assert len(host.clients) == 1  # one dial, and no more over it
    assert host.open_buses == 1
    assert host.clients[0].disconnects > 1  # and it keeps trying to close it

    host.released.set()
    await _polls(link, 3)

    assert host.open_buses == 0
    assert len(host.clients) > 1  # dialling again


async def test_a_command_does_not_dial_over_a_client_that_will_not_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is on every door. A button pressed ten times opens nothing."""
    link, host = _wedged_link(monkeypatch, behind=_MovedBackend)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    await _polls(link, 1)
    assert len(host.clients) == 1

    for _ in range(10):
        with pytest.raises(link_module.NoNewLinkError):
            await link.send(_a_command, vouch=_unvouched)

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
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    await _polls(link_of(coordinator), 1, hass=hass)
    assert link_of(coordinator).unclosed

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)

    assert err.value.translation_key == "link_not_released"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert len(host.clients) == 1


async def test_what_would_not_close_is_not_dialled_over_by_the_next_coordinator(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload does not forget a client that could be neither hung up nor closed.

    Such a client is kept and nothing is dialled over it - by the coordinator
    that kept it. Reloading the entry made another, which knew nothing of it
    and dialled: one more bus beside the one nothing could close, and one more
    with every reload after that. What would not close is now held for the
    lamp, and the coordinator that comes next is handed it.
    """
    held = link_module.Unclosed()
    host = _WedgedBlueZ(_MovedBackend)
    lamp = ScriptedLamp()
    lamp.dials_through(host.dial)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.01)
    monkeypatch.setattr(link_module, "_STACK_FAULT_AFTER", 10**6)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    first = lamp.coordinator(hass, unclosed=held)
    await _polls(link_of(first), 1, hass=hass)
    assert held.clients
    await first.async_stop()

    second = lamp.coordinator(hass, unclosed=held)
    with pytest.raises(HomeAssistantError) as err:
        await second.async_set_power(True)
    await _polls(link_of(second), 3, hass=hass)

    assert err.value.translation_key == "link_not_released"
    assert len(host.clients) == 1  # nothing was dialled over it
    assert host.open_buses == 1
    assert host.clients[0].disconnects > 2  # and the second goes on trying it

    host.released.set()
    await _polls(link_of(second), 3, hass=hass)

    assert host.open_buses == 0
    assert len(host.clients) > 1  # dialling again


async def test_what_is_handed_on_can_still_be_closed_by_the_one_it_is_handed_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client goes on with what is behind it, or nobody could close it again.

    A bus is closed through the client's backend, which is noted when the
    client is taken because Home Assistant's wrapper may forget it. Handed on
    bare, a client whose wrapper has forgotten could never be let go of by
    the next coordinator, and the lamp would stay undialled until a restart.
    """
    held = link_module.Unclosed()
    host = _WedgedBlueZ()

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    lamp = ScriptedLamp()
    lamp.dials_through(_dial_a_stuck_one)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.01)
    monkeypatch.setattr(link_module, "_STACK_FAULT_AFTER", 10**6)
    first = _a_link(lamp.dial, held, greet=_asks)
    await _polls(first, 1)
    assert held.clients
    first.halt()
    await first.let_go()
    stuck = host.clients[0]
    backend = stuck._backend
    # The bus would close now, and the wrapper no longer knows its backend.
    backend._bus = host.bus_of[stuck] = _Bus()
    stuck._backend = None

    second = _a_link(lamp.dial, held, greet=_asks)
    lamp.dials_through(host.dial)
    await _polls(second, 3)

    assert host.bus_of[stuck].closed  # closed through what was handed on
    assert held.clients == set()
    assert held.backends == {}  # and nothing is kept of it once it is let go
    assert len(host.clients) > 1  # and dialling again


async def test_no_dial_gets_in_before_a_client_that_will_not_close_is_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial waits for a hang-up still under way.

    Whether a client will close is not known until its hang-up has run out
    its ceiling, and only then is the client kept. A dial made in between -
    by a command, by the lamp advertising - knew of nothing in its way and
    went ahead: one more client on a stack that was not letting go of the
    first. So a dial waits for the hang-up to end, and then finds the client
    kept and is refused, or finds nothing kept and dials.
    """
    host = _WedgedBlueZ(_MovedBackend)
    lamp = ScriptedLamp()
    lamp.dials_through(host.dial)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_STACK_FAULT_AFTER", 10**6)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 2.0)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    coordinator = lamp.coordinator(hass)
    link_of(coordinator).tick()
    await asyncio.sleep(0.05)  # dialled, found to answer nothing, being hung up
    assert len(host.clients) == 1
    assert host.clients[0].disconnects == 1
    assert not link_of(coordinator).unclosed  # and not yet known not to close

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)

    assert err.value.translation_key == "link_not_released"
    assert len(host.clients) == 1  # nothing was dialled beside it
    assert host.open_buses == 1
    await hass.async_block_till_done()


async def test_no_dial_gets_in_across_a_reload_either(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coordinator a reload makes waits for its predecessor's hang-up.

    A link held when the entry is unloaded is hung up in the background, and
    the unload waits three seconds for it and no longer. The coordinator that
    followed dialled at once - over a client that was kept a few seconds
    later, on a stack that would not hang up. The hang-ups under way are held
    for the lamp, like the clients that would not close.
    """
    held = link_module.Unclosed()
    host = _WedgedBlueZ(_MovedBackend)
    lamp = ScriptedLamp()
    lamp.dials_through(host.dial)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.02)
    monkeypatch.setattr(link_module, "_STACK_FAULT_AFTER", 10**6)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 2.0)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    first = lamp.coordinator(hass, unclosed=held)
    await taken_bare(link_of(first))  # a link it holds when the reload comes

    await first.async_stop()  # gives the hang-up its three seconds, and goes
    assert not held.clients  # still being hung up: not kept yet
    second = lamp.coordinator(hass, unclosed=held)
    with pytest.raises(HomeAssistantError) as err:
        await second.async_set_power(True)

    assert err.value.translation_key == "link_not_released"
    assert len(host.clients) == 1
    assert host.open_buses == 1
    await hass.async_block_till_done()


async def test_a_dial_that_waited_for_a_hang_up_goes_ahead_when_it_has_ended(
    hass: HomeAssistant,
) -> None:
    """On a stack that does hang up, the wait is the hang-up and nothing more.

    The dial is not refused and not put off to the next poll: it follows the
    hang-up, as a command's second try always has.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    await coordinator.async_set_power(True)
    first = lamp.links[0]
    dialled_when_it_ended: list[int] = []
    hang_up_may_end = asyncio.Event()

    async def _slow_to_hang_up() -> None:
        await hang_up_may_end.wait()
        dialled_when_it_ended.append(lamp.dials)

    first.disconnect = _slow_to_hang_up
    lamp.lose()  # reported lost; its hang-up is under way
    command = asyncio.create_task(coordinator.async_set_power(False))
    await asyncio.sleep(0.02)
    assert lamp.dials == 1  # the command waits; nothing is dialled

    hang_up_may_end.set()
    await command

    assert dialled_when_it_ended == [1]  # the hang-up ended, and then
    assert lamp.dials == 2  # the dial went ahead
    assert lamp.written[-1] == (WRITE_UUID, cbor.encode({KEY_POWER: False}))


async def test_a_dial_waits_for_every_hang_up_under_way_and_none_stays_noted() -> None:
    """All of them, not the first to end; and once ended they are forgotten.

    The hang-ups are noted for the lamp, where a reload does not reach. One
    left noted after it ended would be kept for as long as the process runs.
    """
    held = link_module.Unclosed()
    dial = AsyncMock(return_value=_WorkingClient())
    link = _a_link(dial, held)
    may_end = [asyncio.Event(), asyncio.Event()]
    for one in may_end:
        client = _WorkingClient()
        client.disconnect = one.wait
        link.hang_up(client)

    opening = asyncio.create_task(taken_bare(link))
    await asyncio.sleep(0.01)
    may_end[0].set()
    await asyncio.sleep(0.01)
    dial.assert_not_awaited()  # one has ended; the other has not

    may_end[1].set()
    await opening
    await asyncio.sleep(0)

    dial.assert_awaited_once()
    assert held.hang_ups == set()


async def test_a_dial_that_waited_is_refused_if_the_coordinator_stopped_meanwhile(
    hass: HomeAssistant,
) -> None:
    """What the wait has shown is looked at again, and a stop is part of it."""
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    await coordinator.async_set_power(True)
    hang_up_may_end = asyncio.Event()
    lamp.links[0].disconnect = hang_up_may_end.wait
    lamp.lose()
    command = asyncio.create_task(coordinator.async_set_power(False))
    await asyncio.sleep(0.02)  # the command is waiting for the hang-up

    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)
    hang_up_may_end.set()
    with pytest.raises(HomeAssistantError) as err:
        await command
    await stopping

    assert err.value.translation_key == "not_running"
    assert lamp.dials == 1  # nothing was dialled for a coordinator that stopped


async def test_a_command_is_refused_at_once_over_a_client_already_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is already known is not waited for.

    While a client is kept the poll goes on trying to hang it up, so on a
    stack that will not there is nearly always a hang-up under way. A button
    pressed then is told at once that the link was not released - not after
    that hang-up's ten seconds.
    """
    coordinator, host = _wedged(hass, monkeypatch, behind=_MovedBackend)
    await _polls(link_of(coordinator), 1, hass=hass)
    assert link_of(coordinator).unclosed
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 60)
    link_of(coordinator).tick()  # tries the kept client again
    await asyncio.sleep(0)

    async with asyncio.timeout(0.5):
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)

    assert err.value.translation_key == "link_not_released"
    assert len(host.clients) == 1
    host.released.set()  # let the long hang-up end with the test
    await hass.async_block_till_done()


async def test_nothing_is_kept_of_a_client_that_was_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What was noted about a client goes when the client does.

    One client per poll tick for as long as Home Assistant runs: whatever is
    kept per client and never dropped is a leak of its own.
    """
    link, host = _wedged_link(monkeypatch)

    await _polls(link, 5)

    assert len(host.clients) == 5
    assert link.backends == {}
    assert link.unclosed == set()


async def test_a_client_with_nothing_known_behind_it_is_kept_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No backend to look at, and a hang-up that failed: there is no telling.

    So it is treated as the worse case. A client that may hold a bus is not
    forgotten on the strength of not having been able to look.
    """
    link, host = _wedged_link(monkeypatch, behind=lambda _bus: None)

    await _polls(link, 6)

    assert len(host.clients) == 1


class _StuckBus(_Bus):
    """A bus that will not close when told to."""

    def disconnect(self) -> None:
        raise RuntimeError("busy")  # still open, whatever it is


async def test_a_bus_that_will_not_close_stops_the_dialling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same when the bus is reached and refuses: kept, and not dialled over."""
    link, host = _wedged_link(monkeypatch)

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    _lamp_behind(link).dials_through(_dial_a_stuck_one)

    await _polls(link, 6)

    assert len(host.clients) == 1


async def test_a_disconnect_that_returns_is_not_taken_at_its_word() -> None:
    """A disconnect() that returns may have left the bus open all the same.

    bleak's does when another disconnect of the same client is under way:
    it waits for that one and leaves the closing to it. Whether the other one
    got that far is not something the caller is told, so the bus is looked at
    after a hang-up that succeeded as well.
    """
    host = _WedgedBlueZ()
    link = _a_link(host.dial)
    client = await taken_bare(link)
    client.disconnect = AsyncMock()  # returns, having closed nothing

    await link.hang_up(client)

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is closed twice, and a client whose bus is gone is not parked.

    dbus-fast does not raise when told to disconnect a socket that is already
    gone: it logs a warning with a traceback. With the system bus out of
    connections every client is in that state, so asking each one again would
    fill the log exactly when somebody is reading it.
    """
    link, host = _wedged_link(monkeypatch)
    buses: list[_DownBus] = []

    async def _dial_a_dead_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        bus = _DownBus()
        buses.append(bus)
        client._backend._bus = host.bus_of[client] = bus
        client.disconnect = AsyncMock(side_effect=OSError(9, "Bad file descriptor"))
        return client

    _lamp_behind(link).dials_through(_dial_a_dead_one)

    await _polls(link, 4)

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
    """Home Assistant's wrapper around a backend: a link taken, and hung up."""

    def __init__(self, backend: BleakClientBlueZDBus) -> None:
        self._backend: BleakClientBlueZDBus | None = backend

    @property
    def is_connected(self) -> bool:
        return self._backend is not None and self._backend.is_connected

    async def start_notify(self, _uuid: str, _heard: Any) -> None:
        return  # the subscription is not what these tests are about

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


def test_bleak_still_calls_these_what_the_link_calls_them() -> None:
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
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Against bleak's real client: a failed hang-up still closes everything.

    Two ways ``disconnect()`` stops short of the lines that close the bus:
    BlueZ never answers ``Disconnect`` (the incident), and BlueZ answers it
    with an error. The doubles above stand in for bleak; this runs bleak's own
    code, so that a release which renames what the link reaches for fails
    here and not on somebody's host.
    """
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.05)
    bus = _StubBus(answer)
    backend, removed = _bleaks_own_client(bus)
    monitor = backend._disconnect_monitor_event
    client = _HaClient(backend)
    link = _a_link(AsyncMock(return_value=client))
    assert await taken_bare(link) is client  # as a link is: dialled, subscribed

    await link.hang_up(client)

    assert link.client is None
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


async def _on_a_busy_link(
    hass: HomeAssistant,
) -> tuple[GlowriumCoordinator, _GattClient, _BusyBus]:
    """Return a coordinator holding bleak's own client, on a link BlueZ is busy on."""
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    bus = _BusyBus()
    backend, _ = _bleaks_own_client(bus)
    client = _GattClient(backend)
    lamp.dials_through(AsyncMock(return_value=client))
    await taken_bare(link_of(coordinator))  # nothing asked on the bus
    lamp.dials_through(None)  # the next dial is the lamp's own
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
    link_of(coordinator).on_lost(client)


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
    coordinator, client, bus = await _on_a_busy_link(hass)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    exchanges = {
        "the state request": link_of(coordinator).prime_held,
        "the state read": lambda: coordinator._async_read_state(
            turn_over(coordinator, client)
        ),
        "the device-info read": lambda: coordinator._async_read_device_info(
            turn_over(coordinator, client)
        ),
    }

    waiting = asyncio.create_task(exchanges[exchange]())
    await _turned_away(bus, member)
    _bluez_reports_the_link_gone(coordinator, client)
    async with asyncio.timeout(1):
        await waiting  # ends, and with nothing to say about an assertion
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link_of(coordinator).diagnostics()["primed"] is False
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
    coordinator, client, bus = await _on_a_busy_link(hass)
    lamp = lamp_of(coordinator)

    command = asyncio.create_task(coordinator.async_set_power(True))
    await _turned_away(bus, "WriteValue")
    _bluez_reports_the_link_gone(coordinator, client)
    async with asyncio.timeout(1):
        await command  # delivered, and nothing raised
    await hass.async_block_till_done()

    assert lamp.written == [(WRITE_UUID, cbor.encode({KEY_POWER: True}))]
    assert link_of(coordinator).client is lamp.links[0]  # the retry's, a new link
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
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    client = _WorkingClient()
    client.write_gatt_char.side_effect = AssertionError("not about the bus")
    client.read_gatt_char.side_effect = AssertionError("not about the bus")
    lamp.dials_through(AsyncMock(return_value=client))
    await taken_bare(link_of(coordinator))

    with pytest.raises(AssertionError, match="not about the bus"):
        await coordinator.async_set_power(True)
    with pytest.raises(AssertionError, match="not about the bus"):
        await coordinator._async_read_device_info(turn_over(coordinator, client))
    with pytest.raises(AssertionError, match="not about the bus"):
        await link_of(coordinator).prime_held()

    assert link_of(coordinator).client is client  # and nothing was let go of over it


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


def test_the_link_knows_neither_home_assistant_nor_the_protocol() -> None:
    """What the link module imports: the standard library and the Bluetooth one.

    The split (#21) put the link where it can be tested through what it is
    handed. An import of Home Assistant, or of this integration's codec,
    constants or coordinator, would be the two halves growing back together.
    """
    tree = ast.parse(Path(link_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import is one of this integration's own modules.
            imported.add("." if node.level else (node.module or "").split(".")[0])

    assert imported <= {
        "__future__",
        "asyncio",
        "bleak",
        "bleak_retry_connector",
        "collections",
        "contextlib",
        "datetime",
        "enum",
        "logging",
        "time",
        "typing",
    }


def test_every_gatt_call_is_made_where_a_closed_bus_is_a_lost_link() -> None:
    """Each call that goes to the lamp is made under ``_gatt_call``.

    The assertion comes out of whichever call happened to be waiting, so one
    call left outside brings the traceback back for that call alone. This
    reads the integration's source, so a call added later is seen here - and
    a guard given one client around a call made on another guards nothing.

    Since the split (#21) there are three, all in the link's module: a GATT
    call made anywhere else is a client that left it.
    """
    package = Path(coordinator_module.__file__).parent
    bare: list[str] = []
    made_in: list[str] = []
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
                made_in.append(source.name)
                if id(node) not in guarded:
                    bare.append(f"{source.name}:{node.lineno} {node.func.attr}")

    assert bare == []
    # And each is the link's own: the subscription when a client is taken, and
    # the write and the read of a turn at the lamp. The device half makes none.
    assert made_in == ["link.py"] * 3


def test_only_the_link_connects_hangs_up_or_knows_what_the_library_raises() -> None:
    """No client leaves the link's module, and no error of the library does.

    Outside it nothing connects a client, nothing disconnects one, and
    nothing names what the Bluetooth library raises: a lost link is the
    link's own error by the time anything else hears of it (#21). One of the
    library's types is still named outside: the device Home Assistant's
    scanners find, which the dial is handed a lookup for.
    """
    package = Path(coordinator_module.__file__).parent
    found: list[str] = []
    for source in sorted(package.rglob("*.py")):
        if source.name == "link.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            said = None
            if isinstance(node, ast.Import):
                said = [one.name for one in node.names if one.name.startswith("bleak")]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "bleak"
            ):
                said = [one.name for one in node.names if one.name != "BLEDevice"]
            elif isinstance(node, ast.Name) and node.id in (
                "BleakError",
                "establish_connection",
                "_LINK_ERRORS",
            ):
                said = [node.id]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "disconnect"
            ):
                said = ["disconnect()"]
            if said:
                found.append(f"{source.name}:{node.lineno} {', '.join(said)}")

    assert found == []


# What the link offers the device half. A name added here is one more thing
# the coordinator knows of the link; the client and the lock are not on it.
_OF_THE_LINK = frozenset(
    {
        "advertising",
        "begin",
        "diagnostics",
        "halt",
        "in_reach",
        "initial_connect",
        "let_go",
        "log_reach",
        "note_answer",
        "send",
        "shut_down",
        "tick",
    }
)


def test_the_coordinator_holds_no_client_and_reaches_the_link_for_its_offer() -> None:
    """No client is held in the device half, and of the link it knows its offer.

    The names the coordinator kept of the link's - the client, the lock and
    the rest, each a way through to the link for the tests and the bench -
    went as those crossed over (#21, stage 3). The device half is handed a
    turn, so nothing in it names a client; of the link it reaches for what
    the link offers it (``_OF_THE_LINK``) and nothing else; and of a turn,
    nothing that is the turn's own. Nothing else in the integration reaches
    the link at all.
    """
    tree = ast.parse(Path(coordinator_module.__file__).read_text(encoding="utf-8"))
    nodes = [
        node for node in ast.walk(tree) if isinstance(node, ast.Attribute | ast.Name)
    ]

    names_a_client = sorted(
        node.lineno
        for node in nodes
        if isinstance(node, ast.Name) and node.id == "BleakClientWithServiceCache"
    )
    reaches_for = sorted(
        f"{node.lineno} {ast.unparse(node)}"
        for node in nodes
        if isinstance(node, ast.Attribute)
        and (
            # Of the link, what it offers the device half and nothing else.
            (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "_link"
                and node.attr not in _OF_THE_LINK
            )
            # Of a turn, nothing that is the turn's own.
            or (
                isinstance(node.value, ast.Name)
                and node.value.id == "turn"
                and node.attr.startswith("_")
            )
        )
    )
    assert names_a_client == []
    assert reaches_for == []
    # And nothing else in the integration reaches the link at all.
    package = Path(coordinator_module.__file__).parent
    elsewhere = [
        f"{source.name}:{node.lineno}"
        for source in sorted(package.rglob("*.py"))
        if source.name not in ("link.py", "coordinator.py")
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "_link"
    ]
    assert elsewhere == []


def test_no_test_gives_a_link_its_client_by_hand() -> None:
    """A link in a test is taken as the integration takes one.

    Dialled, subscribed to, and then held: through a command, the link's
    connect, or its bare ``open``. A client assigned to a link is a link no
    dial made and nobody subscribed to, with nothing noted of what stands
    behind it - and it was a mock where the scripted lamp stands now (#21,
    stage 3). This reads the tests, as its neighbours read the integration.
    """
    by_hand: list[str] = []
    for source in sorted(Path(__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                name: object = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
            ):
                name = node.args[1].value
            else:
                continue
            if name in ("client", "primed"):
                by_hand.append(f"{source.name}:{node.lineno}")

    assert by_hand == []


def test_a_link_is_hung_up_and_the_entities_told_in_one_place() -> None:
    """Hanging a link up and telling the entities is one method of the link.

    Five paths used to do the two by hand, one after the other, and the rule
    "who lets go of the link tells the listeners" held for as long as nobody
    wrote a sixth and forgot the second line. A stop tells nobody, and a
    command tells them itself when it knows how it ended; neither hangs up
    and tells in two lines of its own.
    """
    by_hand: list[str] = []
    for module in (link_module, coordinator_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for block in ast.walk(function):
                for name in ("body", "orelse", "finalbody"):
                    steps = getattr(block, name, None)
                    if not isinstance(steps, list):
                        continue
                    by_hand.extend(
                        function.name
                        for one, next_one in pairwise(steps)
                        if _calls(one, "hang_up", "_hang_up")
                        and _calls(
                            next_one, "_reach_changed", "_async_notify_listeners"
                        )
                    )

    assert by_hand == ["_drop"]


def _calls(step: ast.AST, *names: str) -> bool:
    """Return whether ``step`` is a bare call of a method by one of ``names``."""
    return (
        isinstance(step, ast.Expr)
        and isinstance(step.value, ast.Call)
        and isinstance(step.value.func, ast.Attribute)
        and step.value.func.attr in names
    )


async def test_a_hang_up_cancelled_half_way_still_closes_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is not an exception, and it leaves the bus open just the same."""
    host = _WedgedBlueZ()
    link = _a_link(host.dial)
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 60)
    client = await taken_bare(link)

    hang_up = link.hang_up(client)
    await asyncio.sleep(0.01)  # into disconnect(), waiting on BlueZ
    hang_up.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hang_up

    assert host.open_buses == 0


# --- A stack that will not hang up is a state, not an event -----------------


_STATE = bytearray.fromhex("a306f508184614f5")  # {power: on, brightness: 70, activated}


class _Clock:
    """The coordinator's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _timed(host: _WedgedBlueZ, clock: _Clock) -> tuple[Any, list[float]]:
    """Return the host's dial with the clock read at each call, and the readings."""
    dialled: list[float] = []

    async def _dial(*args: object, **kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        return await host.dial(*args, **kwargs)

    return _dial, dialled


def _on_a_clock(
    monkeypatch: pytest.MonkeyPatch, *, stack_fault: Any = None
) -> tuple[link_module.Link, _WedgedBlueZ, _Clock, list[float]]:
    """Return a link that backs off, a clock, and the times it dialled."""
    link, host = _wedged_link(monkeypatch, backs_off=True, stack_fault=stack_fault)
    clock = _Clock()
    monkeypatch.setattr(link_module, "monotonic", clock)
    dial, dialled = _timed(host, clock)
    _lamp_behind(link).dials_through(dial)
    return link, host, clock, dialled


def _coordinator_on_a_clock(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[GlowriumCoordinator, _WedgedBlueZ, _Clock, list[float]]:
    """Return the same of a coordinator: for what it makes of the link's word."""
    coordinator, host = _wedged(hass, monkeypatch, backs_off=True)
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)
    monkeypatch.setattr(link_module, "monotonic", clock)
    dial, dialled = _timed(host, clock)
    lamp_of(coordinator).dials_through(dial)
    return coordinator, host, clock, dialled


async def _ticks(
    link: link_module.Link,
    clock: _Clock,
    count: int,
    *,
    hass: HomeAssistant | None = None,
) -> None:
    """Run ``count`` poll ticks thirty seconds apart on the link's clock.

    ``hass`` as for ``_polls``.
    """
    for _ in range(count):
        clock.now += 30
        link.tick()
        await asyncio.sleep(0.04)
    await _done(link, hass)


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in pairwise(times)]


async def test_a_stack_that_will_not_hang_up_is_dialled_less_and_less(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Three unanswered hang-ups in a row, and the poll stops hammering.

    Each dial into a wedged stack is handed the link BlueZ will not let go of,
    learns nothing, and costs the stack a disconnect it cannot honour. So the
    gap doubles, up to a ceiling - and the log says so once, in words that tell
    the owner what only the owner can do about it.
    """
    link, _host, clock, dialled = _on_a_clock(monkeypatch)

    await _ticks(link, clock, 44)

    assert _gaps(dialled) == [30, 30, 60, 120, 240, 300, 300]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "power-cycled" in warnings[0].getMessage()


async def test_one_slow_hang_up_is_not_a_wedged_stack(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BlueZ may just be slow. Two unanswered hang-ups change nothing."""
    link, _host, clock, dialled = _on_a_clock(monkeypatch)

    await _ticks(link, clock, 2)

    assert link.dial_not_before == 0.0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    await _ticks(link, clock, 1)
    assert _gaps(dialled) == [30, 30]
    assert (
        len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    )  # the third


async def test_a_weak_link_is_not_mistaken_for_a_wedged_stack(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Links that answer nothing but hang up properly are just a bad signal.

    At the edge of range a link can die before the first read, again and
    again. BlueZ reports each one gone and every hang-up goes through, and
    that is the difference: nothing is wedged, so nothing backs off.
    """
    link, host, clock, dialled = _on_a_clock(monkeypatch)
    host.released.set()  # BlueZ answers every Disconnect

    await _ticks(link, clock, 8)

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
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    answer: Exception,
) -> None:
    """Only silence counts. An error is BlueZ answering, or the bus itself gone.

    With the system bus out of connections every hang-up ends in "Bad file
    descriptor". Nothing is wedged then, and telling the owner to power-cycle
    the adapter would be sending them to the wrong machine.
    """
    link, host, clock, dialled = _on_a_clock(monkeypatch)

    async def _dial(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        client.disconnect = AsyncMock(side_effect=answer)
        return client

    _lamp_behind(link).dials_through(_dial)

    await _ticks(link, clock, 6)

    assert _gaps(dialled) == [30] * 5
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_proxy_that_times_out_is_not_blamed_on_bluez(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning names BlueZ and its remedy, so it is for BlueZ's client only.

    A lamp behind a Bluetooth proxy has no BlueZ under it. A disconnect that
    times out there is the proxy's affair, and telling its owner to
    power-cycle the host's adapter would send them to the wrong machine.
    """
    link, host = _wedged_link(monkeypatch, behind=_ProxyBackend, backs_off=True)
    clock = _Clock()
    monkeypatch.setattr(link_module, "monotonic", clock)

    await _ticks(link, clock, 6)

    assert len(host.clients) == 6
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.parametrize("out_of_reach", ["bleak rearranged", "the bus refuses"])
async def test_a_stack_is_no_less_stuck_for_a_bus_that_cannot_be_closed(
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
    faults: list[int | None] = []
    link, host = _wedged_link(
        monkeypatch,
        behind=_MovedBackend if rearranged else _Backend,
        backs_off=True,
        stack_fault=faults.append,
    )
    clock = _Clock()
    monkeypatch.setattr(link_module, "monotonic", clock)

    async def _dial_a_stuck_one(*_args: object, **_kwargs: object) -> _WedgedClient:
        client = await host.dial()
        client._backend._bus = host.bus_of[client] = _StuckBus()
        return client

    if not rearranged:
        _lamp_behind(link).dials_through(_dial_a_stuck_one)

    await _ticks(link, clock, 4)

    assert len(host.clients) == 1  # kept, and nothing dialled over it
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "power-cycled" in warnings[0].getMessage()
    assert faults == [3]  # and the repair was called for, with the count


async def test_a_client_with_no_backend_on_record_is_not_blamed_on_bluez(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kept, since there may be a bus behind it - and nobody's to call BlueZ's.

    The warning and the repair name BlueZ and what clears it. With no backend
    on record there is no telling whose client it is that went silent, and
    sending its owner to power-cycle an adapter would be a guess.
    """
    faults: list[int | None] = []
    link, host = _wedged_link(
        monkeypatch, behind=lambda _bus: None, backs_off=True, stack_fault=faults.append
    )
    clock = _Clock()
    monkeypatch.setattr(link_module, "monotonic", clock)

    await _ticks(link, clock, 6)

    assert len(host.clients) == 1  # kept, and nothing dialled over it
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert faults == []  # and no repair was called for


async def test_unanswered_hang_ups_count_only_in_a_row(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two slow hang-ups, one that goes through, one more slow: no wedge.

    A hang-up BlueZ answers breaks the run. And so does the lamp answering
    anything - neither is how a stack behaves that is holding on to a link.
    """
    link, host, clock, _dialled = _on_a_clock(monkeypatch)

    await _ticks(link, clock, 2)
    assert link.stuck_hang_ups == 2
    host.released.set()  # this one BlueZ answers
    await _ticks(link, clock, 1)
    host.released.clear()
    await _ticks(link, clock, 1)

    assert link.stuck_hang_ups == 1
    assert link.dial_not_before == 0.0

    await _ticks(link, clock, 1)
    assert link.stuck_hang_ups == 2
    link.note_answer()  # the lamp says something
    await _ticks(link, clock, 1)

    assert link.stuck_hang_ups == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_wedge_left_alone_for_days_does_not_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap doubles per unanswered hang-up, and the count has no ceiling.

    One dial every five minutes reaches a thousand of them in three and a
    half days, and two to that power does not fit a float: the hang-up
    would end in OverflowError, the backoff would stop moving, and the
    poll would be back to dialling a wedged stack on every tick.
    """
    link, _host, clock, _dialled = _on_a_clock(monkeypatch)
    link.stuck_hang_ups = 5000

    link.note_stuck_hang_up()

    assert link.dial_not_before == (clock.now + link_module._STACK_FAULT_BACKOFF_MAX)


async def test_a_fault_ends_with_the_lamp_and_not_with_a_hang_up(
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
    link, host, clock, dialled = _on_a_clock(monkeypatch)
    await _ticks(link, clock, 3)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    host.released.set()  # hang-ups go through now; the lamp still answers nothing
    await _ticks(link, clock, 6)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    host.released.clear()  # ...and then they stop going through again
    await _ticks(link, clock, 24)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert link.stuck_hang_ups > link_module._STACK_FAULT_AFTER
    host.released.set()

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        client.write_gatt_char = AsyncMock()  # the lamp answers: acknowledged
        return client

    _lamp_behind(link).dials_through(_healthy)
    await _ticks(link, clock, 12)

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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)

    await _ticks(link_of(coordinator), clock, 2, hass=hass)
    assert _stack_issue(hass) is None

    await _ticks(link_of(coordinator), clock, 1, hass=hass)
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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    coordinator.name = name

    await _ticks(link_of(coordinator), clock, 3, hass=hass)

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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    coordinator._entry = SimpleNamespace(
        entry_id="01JENTRY",
        async_create_background_task=lambda _hass, coro, name: hass.async_create_task(
            coro, name
        ),
    )

    await _ticks(link_of(coordinator), clock, 3, hass=hass)

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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    coordinator.name = "Glowrium " + "x" * 200

    await _ticks(link_of(coordinator), clock, 3, hass=hass)

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
    coordinator, host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    await _ticks(link_of(coordinator), clock, 3, hass=hass)
    assert _stack_issue(hass) is not None

    host.released.set()  # hang-ups go through now; the lamp still answers nothing
    await _ticks(link_of(coordinator), clock, 6, hass=hass)
    assert _stack_issue(hass) is not None

    lamp = lamp_of(coordinator)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    lamp.readable(INFO_UUID, b"brand:x;;")
    lamp.dials_through(None)  # the stack has come round: the next dial reaches the lamp
    await _ticks(link_of(coordinator), clock, 12, hass=hass)

    assert _stack_issue(hass) is None


async def test_the_repair_goes_with_the_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An integration that has been unloaded is not watching the stack any more."""
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    await _ticks(link_of(coordinator), clock, 3, hass=hass)
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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    await _ticks(link_of(coordinator), clock, 2, hass=hass)
    assert link_of(coordinator).stuck_hang_ups == 2

    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.3)
    clock.now += 30
    link_of(coordinator).tick()
    await asyncio.sleep(0.05)  # dialled, asked, dropped: the hang-up is in flight

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        await coordinator.async_stop()
        assert _stack_issue(hass) is None
        await asyncio.sleep(0.5)  # ...and it runs out, unanswered
        await hass.async_block_till_done()

    assert link_of(coordinator).stuck_hang_ups == 3  # counted, as any other
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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    await _ticks(link_of(coordinator), clock, 2, hass=hass)
    assert link_of(coordinator).stuck_hang_ups == 2
    await taken_bare(link_of(coordinator))  # a link held when the unload comes

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        await coordinator.async_stop()
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()

    assert link_of(coordinator).stuck_hang_ups == 3
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
    coordinator, _host, clock, _dialled = _coordinator_on_a_clock(hass, monkeypatch)
    entry = SimpleNamespace(
        entry_id="01JENTRY",
        async_create_background_task=lambda _hass, coro, name: hass.async_create_task(
            coro, name
        ),
    )
    coordinator._entry = entry
    if announced_before_it_stopped:
        await _ticks(link_of(coordinator), clock, 3, hass=hass)
        assert _stack_issue(hass) is not None
    else:
        await _ticks(link_of(coordinator), clock, 2, hass=hass)
        await taken_bare(link_of(coordinator))  # a link held when it is stopped
    await coordinator.async_stop()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert link_of(coordinator).stuck_hang_ups == 3
    assert _stack_issue(hass) is None

    successor, _host, successor_clock, _ = _coordinator_on_a_clock(hass, monkeypatch)
    successor._entry = entry
    await _ticks(link_of(successor), successor_clock, 3, hass=hass)
    assert _stack_issue(hass) is not None

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        link_of(coordinator).note_answer()

    assert _stack_issue(hass) is not None  # the successor's, and still standing
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    await successor.async_stop()


async def test_a_wedged_stack_is_survived_without_home_assistant() -> None:
    """The bench has no dashboard to raise a repair on, and counts all the same."""
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")

    for _ in range(link_module._STACK_FAULT_AFTER):
        link_of(coordinator).note_stuck_hang_up()
    link_of(coordinator).note_answer()

    assert link_of(coordinator).stuck_hang_ups == 0


async def test_a_command_is_not_held_back_by_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backoff is for the poll. Somebody pressing a button gets a dial."""
    link, _host, clock, dialled = _on_a_clock(monkeypatch)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    await _ticks(link, clock, 3)
    assert clock.now < link.dial_not_before
    before = len(dialled)

    with pytest.raises(link_module.LinkLostError):  # the stack is still wedged
        await link.send(_a_command, vouch=_unvouched)
    await _settled(link)

    assert len(dialled) > before


async def test_an_advertisement_does_not_dial_through_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lamp advertises all through a wedge, about once a second.

    Each advertisement asks for a reconnect when there is no link. If that
    door stayed open, the backoff would be a thirty-second poll standing
    politely aside for a one-second one.
    """
    link, _host, clock, dialled = _on_a_clock(monkeypatch)
    await _ticks(link, clock, 3)
    before = len(dialled)

    link.advertising(True)
    await asyncio.sleep(0.04)
    await _settled(link)

    assert len(dialled) == before


async def test_the_first_answer_ends_the_backoff(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the lamp answers, the stack is believed again - at once, and aloud.

    After the adapter has been reset the next dial is a real one. Its first
    read ends the episode: the log gets its closing line, and a link lost
    afterwards is redialled on the next tick, not minutes later.
    """
    link, host, clock, dialled = _on_a_clock(monkeypatch)
    await _ticks(link, clock, 3)
    assert clock.now < link.dial_not_before

    # The adapter is power-cycled: BlueZ answers, and the lamp does.
    host.released.set()

    async def _healthy(*_args: object, **_kwargs: object) -> _WedgedClient:
        dialled.append(clock.now)
        client = await host.dial()
        client.write_gatt_char = AsyncMock()  # the lamp answers: acknowledged
        return client

    _lamp_behind(link).dials_through(_healthy)
    await _ticks(link, clock, 2)

    assert link.connected
    assert link.dial_not_before == 0.0
    assert link.stuck_hang_ups == 0  # the next slow one starts from none
    # Said at the level the episode was announced at, or whoever read the
    # warning never learns that it is over.
    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(said) == 2
    assert "answers again" in said[1]

    held = link.client
    link.on_lost(held)  # the lamp drops it, as it does
    already = len(dialled)
    await _ticks(link, clock, 1)
    assert len(dialled) == already + 1


# --- A link that died without BlueZ noticing --------------------------------


async def _holding(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> tuple[GlowriumCoordinator, ScriptedLamp, LampLink, _Clock]:
    """Return a coordinator holding a primed link to a lamp that answers.

    For the checks that are the device half's as much as the link's: what
    the state request makes of a call that failed, what a notification
    counts for, what the answer carries. The link alone is had from ``_held``.

    The link is taken as the poll takes one - dialled, the lamp asked for its
    state and answering - and what that left behind is cleared away: what the
    lamp was written and read, and the mirror. What the lamp is asked from
    here on is the test's, and the mirror holds what the test made it hear.
    The lamp's state can be read as well, as a lamp that is read first is.
    """
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "monotonic", clock)
    monkeypatch.setattr(link_module, "monotonic", clock)
    lamp = ScriptedLamp()
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    lamp.readable(NOTIFY_UUID, bytes(_STATE))
    coordinator = lamp.coordinator(hass)
    await link_of(coordinator).connect()
    assert link_of(coordinator).diagnostics()["primed"]
    lamp.forget()
    nothing_heard(coordinator)
    return coordinator, lamp, lamp.links[-1], clock


async def _held(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[link_module.Link, ScriptedLamp, LampLink, _Clock]:
    """Return a link holding a primed link to a lamp that answers, and nothing else.

    It asks the lamp for its state on a link it has taken and on one that has
    gone silent (``_asks``), as the device half's talks do; the lamp
    acknowledges every write. What taking the link wrote is cleared away:
    what the lamp is asked from here on is the test's.
    """
    clock = _Clock()
    monkeypatch.setattr(link_module, "monotonic", clock)
    lamp = ScriptedLamp()
    link = _a_link(lamp.dial, greet=_asks, probe=_asks)
    await link.connect()
    assert link.diagnostics()["primed"]
    lamp.forget()
    return link, lamp, lamp.links[-1], clock


async def _tick(link: link_module.Link, hass: HomeAssistant | None = None) -> None:
    link.tick()
    await _done(link, hass)


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
    coordinator, lamp, held, clock = await _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1  # a lamp that is read first
    lamp.fails_writes(BleakError(_NOT_CONNECTED))

    assert await coordinator._request_state(
        turn_over(coordinator, held)
    )  # the read did answer
    clock.now += 5
    await _tick(link_of(coordinator), hass)  # inside the grace BlueZ is given
    assert link_of(coordinator).client is held

    clock.now += 30
    await _tick(link_of(coordinator), hass)

    assert link_of(coordinator).client is None
    assert held.hang_ups == 1


async def test_a_lamp_that_speaks_again_is_not_let_go(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request can fail on a link that is perfectly alive.

    Not every failure that is not a refusal means the link has gone: a lamp
    has answered a request with "Unlikely Error" and gone on notifying. What
    it says afterwards settles it, whatever BlueZ called the link before.
    """
    coordinator, lamp, held, clock = await _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    lamp.fails_writes(BleakError("GATT Protocol Error: Unlikely Error"))
    await coordinator._request_state(turn_over(coordinator, held))

    clock.now += 5
    lamp.say(_STATE)  # and it is still talking
    clock.now += 30
    await _tick(link_of(coordinator), hass)

    assert link_of(coordinator).client is held
    assert held.hang_ups == 0


async def test_a_link_bluez_did_report_dropped_is_not_hung_up_twice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case stays as it was: BlueZ reports, and that is the end of it."""
    coordinator, lamp, old, clock = await _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    lamp.fails_writes(BleakError(_NOT_CONNECTED), times=1)
    await coordinator._request_state(turn_over(coordinator, old))
    assert link_of(coordinator).lost is not None

    lamp.lose()  # two seconds later, as usual
    await hass.async_block_till_done()
    # A link taken since, and nothing heard on it yet: the note stands.
    fresh = await taken_bare(link_of(coordinator))
    clock.now += 60
    await _tick(link_of(coordinator), hass)

    assert old.hang_ups == 1
    assert fresh.hang_ups == 0  # the note was about the old client
    assert link_of(coordinator).client is fresh


async def test_a_silent_link_is_asked_whether_it_is_still_there(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held link that has said nothing for five minutes is asked for its state.

    The lamp only speaks when something changes, so a dead link and an idle
    one look the same from here. Asking tells them apart, and the answer is
    the lamp's state, which is worth having anyway. Asked, not read: a read
    would end the very link it was checking.
    """
    coordinator, lamp, held, clock = await _holding(hass, monkeypatch)

    clock.now += link_module._PROBE_INTERVAL - 1
    await _tick(link_of(coordinator), hass)
    assert len(lamp.asked) == 0  # not before its time

    clock.now += 1
    await _tick(link_of(coordinator), hass)

    assert len(lamp.asked) == 1
    assert lamp.read == []
    assert link_of(coordinator).client is held
    assert coordinator.state[KEY_POWER] is True  # and the answer was taken in
    assert link_of(coordinator).last_answer == clock.now

    await _tick(link_of(coordinator), hass)  # it has just answered
    assert len(lamp.asked) == 1

    clock.now += link_module._PROBE_INTERVAL  # and silent again since
    await _tick(link_of(coordinator), hass)
    assert len(lamp.asked) == 2


async def test_a_silent_link_that_does_not_answer_is_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And one that answers nothing is hung up, for the poll to rebuild."""
    coordinator, lamp, held, clock = await _holding(hass, monkeypatch)
    lamp.fails_writes(BleakError(_NOT_CONNECTED))
    lamp.fails_reads(BleakError(_NOT_CONNECTED))

    clock.now += link_module._PROBE_INTERVAL
    await _tick(link_of(coordinator), hass)

    assert link_of(coordinator).client is None
    assert held.hang_ups == 1


async def test_a_question_that_is_never_answered_drops_the_link_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link that keeps the question waiting is as dead as one that says so.

    BlueZ can leave a call without any answer. The deadline then ends the
    wait, and that has to count as the link's answer: left as a mere log
    line, the same question would be put on every poll tick for ever, each
    time holding the lock for as long as the deadline allows.
    """
    monkeypatch.setattr(link_module, "_ASK_TIMEOUT", 0.05)
    link, lamp, held, clock = await _held(monkeypatch)
    lamp.never_acknowledges_a_write()

    clock.now += link_module._PROBE_INTERVAL
    async with asyncio.timeout(2):
        await _tick(link)

    assert link.client is None
    assert held.hang_ups == 1


async def test_a_question_that_could_not_be_put_proves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not getting the lock in time says nothing about the link.

    A command may hold it for its whole budget. The link is fine then, and
    the question simply waits for the next tick.
    """
    monkeypatch.setattr(link_module, "_ASK_TIMEOUT", 0.05)
    link, lamp, held, clock = await _held(monkeypatch)
    may_go = asyncio.Event()
    lamp.acknowledges_when(may_go)
    command = asyncio.create_task(link.send(_a_command, vouch=_unvouched))
    await asyncio.sleep(0)  # somebody is at work on the link

    clock.now += link_module._PROBE_INTERVAL
    await _tick(link)

    assert link.client is held
    assert len(lamp.asked) == 0
    assert held.hang_ups == 0
    may_go.set()
    await command


async def test_a_link_that_is_talking_is_not_asked(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notification is an answer. So is a command that went through."""
    coordinator, lamp, _, clock = await _holding(hass, monkeypatch)

    clock.now += link_module._PROBE_INTERVAL - 10
    lamp.say(_STATE)  # the lamp reports a change
    clock.now += 20
    await _tick(link_of(coordinator), hass)
    assert len(lamp.asked) == 0

    clock.now += link_module._PROBE_INTERVAL - 10
    await coordinator.async_set_power(True)  # a command, answered
    clock.now += 20
    await _tick(link_of(coordinator), hass)
    assert len(lamp.asked) == 0


async def test_a_read_alone_is_not_taken_for_the_lamp_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is read counts where it is taken in, not for having been read.

    A state that was read is a frame from the lamp, and is noted as an answer
    where it is taken in. The device info is the other thing that is read,
    and is not: on BlueZ it is the read this link does not outlive, so a link
    that has just been read is the last one to call alive. Counting every
    read was proposed with the split (#21), and is not done unless it is
    named there first.
    """
    link, lamp, held, clock = await _held(monkeypatch)
    lamp.readable(INFO_UUID, b"brand:x;;")

    clock.now += link_module._PROBE_INTERVAL - 10
    await link_module.Turn(link, held).read(INFO_UUID)  # and nothing taken in
    clock.now += 20
    await _tick(link)

    assert len(lamp.asked) == 1  # silent for five minutes, read or not


async def test_a_write_that_lost_its_link_puts_nothing_in_doubt_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link is put in doubt where the device half says so, and nowhere else.

    It says so in one place, for a state request lost without a refusal; the
    tick then lets go of a link the stack never reported dropped. Doing the
    same for every lost write was proposed with the split (#21), and is not
    done unless it is named there first.
    """
    link, lamp, held, _clock = await _held(monkeypatch)
    lamp.fails_writes(BleakError("Not connected"))

    with pytest.raises(link_module.LinkLostError):
        await link_module.Turn(link, held).write(WRITE_UUID, b"\xa0")

    assert link.lost is None


async def test_a_command_that_gave_up_on_its_link_is_told_of_by_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where a command lets go of a client for good, the link tells of it.

    As it does everywhere else it lets go of one. Whoever handed the command
    over tells the entities how it ended as well; that the lamp may be out of
    reach now is the link's to say, whoever that was.
    """
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp = ScriptedLamp()
    lamp.fails_writes(BleakError("down"))
    told: list[bool] = []
    link = _a_link(lamp.dial, reach_changed=lambda: told.append(link.connected))

    async def _say(turn: link_module.Turn) -> None:
        await turn.write(WRITE_UUID, b"\xa0")

    async def _no() -> bool:
        return False

    with pytest.raises(link_module.LinkLostError):
        await link.send(_say, vouch=_no)

    assert told[-1] is False  # the last they heard: there is no link
    assert link.client is None


async def test_a_refusal_is_not_taken_for_a_link_that_is_going(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp that says no has answered; nothing about it is waiting to drop."""
    coordinator, lamp, held, clock = await _holding(hass, monkeypatch)
    coordinator._state_request_failures = 1
    lamp.fails_writes(BleakError("Insufficient authorization (8)"))

    await coordinator._request_state(turn_over(coordinator, held))
    clock.now += 60
    await _tick(link_of(coordinator), hass)

    assert link_of(coordinator).lost is None
    assert link_of(coordinator).client is held
    assert held.hang_ups == 0


async def test_two_ticks_do_not_ask_the_same_question_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question still waiting for the lock when the answer comes is dropped.

    The poll does not know that the last tick's question is still queued, and
    asks again. The second one looks, once it has the lock, at whether the
    lamp has answered meanwhile.
    """
    link, lamp, _held_link, clock = await _held(monkeypatch)
    # The answer takes a moment: long enough for the second tick's question to
    # queue up behind this one's lock. An answer that came inside the call
    # would be there before the second had looked.
    lamp.answers(after=0.01)
    clock.now += link_module._PROBE_INTERVAL

    link.tick()
    link.tick()
    await _settled(link)

    assert len(lamp.asked) == 1
