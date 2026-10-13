"""Tests of the link: taking one, keeping it, letting go of it.

The dial and the background connect with their deadlines, a command's way
through the link - its retries, its budget, the bare link it takes - the
first exchange being run or dropped, the hang-up, what is said of reach,
stopping and shutting down. Each takes its link from the scripted lamp,
through a coordinator: what is asserted is the link's to do, whoever built
it. What is said to the lamp is in test_coordinator.py; the bus a client
leaves open, the stack that will not hang up and the checks of a held link
are in test_bus_lifetime.py.
"""

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from bleak_retry_connector import BLEAK_TIMEOUT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.glowrium import (
    cbor,
    keeper as keeper_module,
    link as link_module,
)
from custom_components.glowrium.const import (
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_POWER,
    NOTIFY_UUID,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator

from .lamp import ScriptedLamp, link_of, turn_over
from .shared import _an_entry, _at_a_lamp, _holding_a_link, _kept_for

# What a call receives when its client's bus is closed underneath it. Run against
# dbus-fast with a bus that is shut while a call is waiting for its reply: the
# call ends in EOFError, and with the socket gone, in "Bad file descriptor".
# Neither is a BleakError, and bleak passes both on as they are.
_BUS_CLOSED = (EOFError(), OSError(9, "Bad file descriptor"))


async def test_available_follows_presence_not_connection(hass: HomeAssistant) -> None:
    """Availability tracks presence or a live link, so it does not flap on reconnect."""
    coordinator, lamp = _at_a_lamp(hass)
    assert coordinator.available is False  # neither present nor connected
    link_of(coordinator).begin(present=True)
    assert coordinator.available is True  # advertising -> available
    link_of(coordinator).advertising(False)
    await coordinator.async_set_power(True)  # a link is taken
    assert coordinator.available is True  # connected -> available
    lamp.links[0].is_connected = False
    assert coordinator.available is False  # link dropped and gone -> unavailable


async def test_presence_callbacks_notify(hass: HomeAssistant) -> None:
    """Advertisement/unavailable callbacks flip presence and notify listeners."""
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # the reconnect an advertisement sets off comes to nothing
    updates: list[int] = []
    coordinator.async_add_listener(lambda: updates.append(1))
    link_of(coordinator).advertising(True)
    assert link_of(coordinator).diagnostics()["advertising"] is True
    link_of(coordinator).advertising(False)
    assert link_of(coordinator).diagnostics()["advertising"] is False
    assert updates == [1, 1]  # notified on the present flip and on going away
    await hass.async_block_till_done()  # the reconnect, failing at debug level


@pytest.mark.parametrize("behind_it", ["Home Assistant", "nothing: the bench"])
@pytest.mark.parametrize(
    "failure",
    [BleakError("dropped"), *_BUS_CLOSED],
    ids=["dropped", "eof", "bad-fd"],
)
async def test_write_retries_once_after_a_dropped_link(
    hass: HomeAssistant, behind_it: str, failure: Exception
) -> None:
    """A write that fails once hangs up its link, reconnects, retries and succeeds.

    When the lamp drops the link, the disconnected callback hangs the client
    up at once, which closes its D-Bus connection. A write still waiting for
    its reply on that connection does not get the BleakError a lost link
    usually produces: it gets whatever the bus raised - EOFError, and with
    the socket gone "Bad file descriptor". Caught as nothing in particular,
    that went straight out of the command: no retry, and a bare EOFError
    where the user should read "cannot connect". Without Home Assistant
    behind the coordinator too: a command on a link that drops under it is
    what tools/bench.py exists to exercise, and the retry waits for the
    hang-up before it dials again (0.2.1 scheduled that hang-up on hass).
    """
    with_hass = behind_it == "Home Assistant"
    coordinator, lamp, first = await _holding_a_link(
        hass if with_hass else None, "Glowrium-G7" if with_hass else "bench"
    )
    lamp.fails_writes(failure, times=1)

    await coordinator.async_set_power(True)

    assert len(lamp.written) == 2  # failed, then retried
    assert lamp.dials == 2  # a reconnect happened before the retry
    assert coordinator.state[KEY_POWER] is True
    assert first.hung_up == 1
    assert link_of(coordinator).client is lamp.links[1]


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        pytest.param(BleakError("Not connected"), "Not connected", id="a lost link"),
        pytest.param(EOFError(), "EOFError()", id="eof"),
        pytest.param(
            OSError(9, "Bad file descriptor"),
            "[Errno 9] Bad file descriptor",
            id="bad-fd",
        ),
    ],
)
async def test_write_raises_after_two_failures(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception, said: str
) -> None:
    """A write that keeps failing is reported as a readable HA error.

    Silence is not success: with no confirmation the error still surfaces,
    in the user's words - and when the retry meets the same bus closed under
    it as the first try did, the same words. The error is kept: as what the
    link made of it, and under that as the library raised it.
    """
    coordinator, lamp = _at_a_lamp(hass)
    # Nothing will confirm this write, so do not sit out the whole grace window.
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.fails_writes(failure)

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"
    assert isinstance(err.value.__cause__, link_module.LinkLostError)
    assert str(err.value.__cause__) == said
    assert isinstance(err.value.__cause__.__cause__, type(failure))
    assert len(lamp.written) == 2  # tried twice, then gave up


@pytest.mark.parametrize(
    "held_up_by", ["a dial that never connects", "the lock a background connect holds"]
)
async def test_command_gives_up_instead_of_hanging(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, held_up_by: str
) -> None:
    """An unreachable device fails the command promptly, not after minutes.

    bleak's own retries can keep a connect attempt alive for minutes, which
    made a button in the UI look like it had hung; the command budget caps
    it. A command blocked by a background reconnect gives up too: the
    reconnect holds the link's lock while it dials, so the budget has to
    cover the wait for the lock, not just the write itself.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.dials_when(asyncio.Event())  # a dial that never completes
    connecting = None
    if held_up_by == "the lock a background connect holds":
        connecting = asyncio.create_task(link_of(coordinator).connect())
        await asyncio.sleep(0)  # it holds the lock, dialling
    try:
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    finally:
        if connecting is not None:
            connecting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connecting
    assert err.value.translation_key == "cannot_connect"


async def test_setup_is_not_held_by_a_connect_that_never_finishes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect to an unreachable lamp must not hold the connection lock open.

    Setup awaited this path once. When the reconnect poll held the lock while
    grinding through attempts to a lamp that was out of range, setup waited on
    that lock with no deadline and the entry stayed in "setup in progress"
    forever - never even reaching setup_retry.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    # The other holder: a command dialling a lamp that is never found.
    lamp.dials_when(asyncio.Event())
    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)
    try:
        with pytest.raises(TimeoutError):
            await link_of(coordinator).connect()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


async def test_a_command_connect_reads_nothing(hass: HomeAssistant) -> None:
    """A command's own connect asks for a bare link: it writes, and reads nothing.

    Not even the model: it used to read the device-info string on its way to
    the write when the model was not known yet - a read between a button and
    its lamp, and on BlueZ a link with two seconds to live. Nor does it pay
    for priming first: that cost a device-info read, a state read, the
    batched request and up to 3 s waiting for the activation flag, all before
    the write and all inside the command budget, and on a lamp where the
    connect alone is marginal that is what turned a working command into a
    reported failure. A link that was primed would have had the state
    request written to it first.
    """
    coordinator, lamp = _at_a_lamp(hass)

    await coordinator.async_set_power(True)

    assert lamp.dials == 1
    assert lamp.exchanges == [("write", WRITE_UUID)]  # nothing read, nothing asked
    assert lamp.read == []
    assert len(lamp.written) == 1


async def test_a_first_exchange_on_a_held_link_does_not_wait_for_ever(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exchange the poll makes on a command's link has a ceiling too.

    It takes the lock. A lamp that never acknowledges the state request would
    hold it for good, and every command after that would spend its whole
    budget waiting behind a question nobody is going to answer. The link is
    left as it was - held, and without its first exchange - for the next
    tick.
    """
    monkeypatch.setattr(link_module, "_ASK_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.never_acknowledges_a_write()

    async with asyncio.timeout(2):
        await link_of(coordinator).prime_held()

    assert not link_of(coordinator).lock.locked()
    assert link_of(coordinator).client is link
    assert link_of(coordinator).diagnostics()["primed"] is False


async def test_two_ticks_do_not_make_the_first_exchange_twice(
    hass: HomeAssistant,
) -> None:
    """An exchange still waiting for the lock when the first is over is dropped.

    The tick does not know that the exchange the last tick began is still
    under way, and begins another. Once it has the lock, the second looks at
    whether the link has had its exchange meanwhile.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    # Long enough for the next tick to queue up behind this one.
    lamp.answers({KEY_ACTIVATED: True}, after=0.01)
    lamp.readable(INFO_UUID, b"brand:x;;")

    link_of(coordinator).tick()
    link_of(coordinator).tick()
    await hass.async_block_till_done()

    assert len(lamp.asked) == 1


async def test_a_failed_reconnect_does_not_wedge_reconnection(
    hass: HomeAssistant,
) -> None:
    """One failed attempt must not stop the lamp being retried.

    The poll is the only thing that gets a dropped link back, and it refuses
    to start a second attempt while one is in flight. If a failure left that
    flag set, the lamp would never be reconnected again for the rest of the
    session - on this hardware failures are the normal case, not the rare one.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()

    link_of(coordinator).tick()
    await hass.async_block_till_done()
    assert lamp.dials == 1

    link_of(coordinator).tick()
    await hass.async_block_till_done()
    assert lamp.dials == 2  # and again, and again


async def test_advertisements_do_not_start_a_connect_storm(
    hass: HomeAssistant,
) -> None:
    """Only one connect at a time, however fast the lamp advertises.

    Advertisements arrive about once a second; starting a connect for each
    would pile them onto a device that allows exactly one connection.
    """
    coordinator, lamp = _at_a_lamp(hass)
    release = asyncio.Event()

    async def _hangs(*_a: object, **_kw: object) -> None:
        await release.wait()
        raise BleakError("gone again")

    lamp.dials_through(_hangs)

    for _ in range(5):
        link_of(coordinator).advertising(True)
    link_of(coordinator).tick()  # the poll must not add one either
    await asyncio.sleep(0)
    assert lamp.dials == 1

    release.set()
    await hass.async_block_till_done()


async def test_a_write_on_a_turn_whose_link_is_gone_is_refused_not_dropped(
    hass: HomeAssistant,
) -> None:
    """Writing on a turn whose link has gone must raise, so the retry and the user hear.

    Returning quietly would make every command look like it succeeded while
    nothing reached the lamp. Every write of the device half goes on a turn;
    the raw write the bench once had for this went with #21, stage 3.
    """
    coordinator, _lamp, link = await _holding_a_link(hass)
    turn = turn_over(coordinator, link)
    link.lose()

    with pytest.raises(link_module.LinkLostError):
        await turn.write(WRITE_UUID, cbor.encode({KEY_POWER: True}))


async def test_the_entities_are_told_when_a_link_is_taken(hass: HomeAssistant) -> None:
    """A link is half of what the entities' reach goes by, and they hear of it.

    Availability is "advertising, or a link". A lamp that is linked while it
    is not heard advertising is in reach from the moment the link is held -
    not from the end of the first exchange seconds later, and not from
    whenever the lamp next has something to report.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.never_acknowledges_a_write()  # asked, never answered
    seen: list[bool] = []
    coordinator.async_add_listener(lambda: seen.append(coordinator.available))

    connecting = asyncio.create_task(link_of(coordinator).connect())
    try:
        for _ in range(5):  # dialled, subscribed, waiting on its first question
            await asyncio.sleep(0)
        assert seen == [True]
    finally:
        connecting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connecting


async def test_a_connect_that_waited_behind_a_command_dials_nothing(
    hass: HomeAssistant,
) -> None:
    """A background connect that gets the lock after a command took a link stops.

    Both take the same lock, and the connect looked before it began to wait.
    Dialling on what it saw then would put a second client in place of the
    first, which nothing would ever hang up - on a lamp with one slot.
    """
    coordinator, lamp = _at_a_lamp(hass)
    found = asyncio.Event()
    lamp.dials_when(found)

    command = asyncio.create_task(coordinator.async_set_power(True))
    for _ in range(3):  # the command has the lock, and is dialling
        await asyncio.sleep(0)
    waiting = asyncio.create_task(link_of(coordinator).connect())
    for _ in range(3):  # the connect has looked, and waits for the lock
        await asyncio.sleep(0)
    found.set()  # the link the command made
    await command
    await waiting

    assert lamp.dials == 1  # the connect dialled nothing
    assert link_of(coordinator).client is lamp.links[0]


def _info_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what the link logged at INFO, the level a user reads."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and record.name == link_module.__name__
    ]


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        pytest.param("Glowrium-G7", "Glowrium-G7 (AA:BB:CC:DD:EE:FF)", id="discovered"),
        pytest.param(
            "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)",
            "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)",
            id="picked from the list",
        ),
    ],
)
async def test_going_out_of_reach_and_coming_back_are_each_said_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, name: str, shown: str
) -> None:
    """The log says when the lamp became unreachable, and when it returned.

    Its entities go unavailable and nothing said why or since when. Once
    each way, however many times the same thing is observed in between - by
    the lamp's name, and with its address once: a lamp picked from the list
    has its address in its title already.
    """
    coordinator, lamp = _at_a_lamp(hass, name)
    lamp.out_of_range()  # this is about the log, not about dialling
    link_of(coordinator).begin(present=True)

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        link_of(coordinator).advertising(False)
        link_of(coordinator).advertising(False)
        coordinator._async_notify_listeners()
        (gone,) = _info_lines(caplog)
        assert gone.startswith(f"{shown} is out of reach")

        caplog.clear()
        link_of(coordinator).advertising(True)
        link_of(coordinator).advertising(True)
        coordinator._async_notify_listeners()
        (back,) = _info_lines(caplog)
        assert back == f"{shown} is back in reach"
    await hass.async_block_till_done()  # the dials the advertisements set off


async def test_a_lamp_with_a_link_is_not_out_of_reach_for_being_quiet(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Reach is the advertisement or the link, the same as for the entities.

    A lamp may stop advertising while it is connected. Its entities stay
    available then, and the log must not say otherwise - until the link goes
    as well, which is the moment it really is out of reach.
    """
    coordinator, _lamp, link = await _holding_a_link(hass)
    link_of(coordinator).begin(present=True)

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        link_of(coordinator).advertising(False)
        assert coordinator.available
        assert _info_lines(caplog) == []

        link.lose()
        assert not coordinator.available
        assert len(_info_lines(caplog)) == 1
        assert "out of reach" in _info_lines(caplog)[0]
    await hass.async_block_till_done()  # the hang-up the disconnect started


async def test_a_command_that_fails_says_the_link_is_gone(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed command lets go of the link, and that may be all there was.

    The lamp was connected and no longer advertising. The command fails, the
    link is dropped, and nothing reaches the lamp any more - which the
    entities have to hear there and then, and the log with them. The error
    handed to the caller used to be all that was said: the entities stayed
    available until something else happened to tell them.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)  # and not advertising
    lamp.fails_writes(BleakError("Not connected"))
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    told: list[bool] = []
    coordinator.async_add_listener(lambda: told.append(coordinator.available))
    assert coordinator.available

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_power(True)
        await hass.async_block_till_done()

    assert not coordinator.available
    assert told
    assert told[-1] is False
    assert len(_info_lines(caplog)) == 1
    assert "out of reach" in _info_lines(caplog)[0]


async def test_a_link_dropped_while_it_is_primed_is_said_to_be_gone(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The poll's priming can be what finds a link dead, and drops it."""
    coordinator, lamp, link = await _holding_a_link(hass)  # and not advertising
    lamp.fails_writes(BleakError("Not connected"))
    told: list[bool] = []
    coordinator.async_add_listener(lambda: told.append(coordinator.available))

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        await link_of(coordinator).prime_held()
        await hass.async_block_till_done()

    assert link.hung_up
    assert told == [False]
    assert len(_info_lines(caplog)) == 1
    assert "out of reach" in _info_lines(caplog)[0]


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_a_stopped_coordinator_does_not_say_where_the_lamp_is(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stopped_by: str,
) -> None:
    """A coordinator that was told to stop is not the one to call the lamp gone.

    It let go of its link because it was stopped, not because the lamp went
    anywhere, and it no longer hears advertisements. A command that still
    arrives is refused, and tells the listeners as any command does - which
    must not turn into a line saying that the lamp is out of reach until it
    is heard again. Nobody is listening for it.
    """
    coordinator, _lamp, _link = await _holding_a_link(hass)  # held by its link alone
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        if stopped_by == "an unload":
            await coordinator.async_stop()
        else:
            coordinator.async_shutdown()
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_power(True)
        await hass.async_block_till_done()

    assert not coordinator.available
    assert _info_lines(caplog) == []


@pytest.mark.parametrize("present", [True, False], ids=["advertising", "absent"])
async def test_a_lamp_that_is_advertising_at_start_is_in_reach_from_the_start(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    present: bool,
) -> None:
    """What Home Assistant already knows of the lamp is taken as the watching begins.

    The entities are built right after, and on a weak link the lamp may not
    be heard again for seconds or connected to for minutes. Until then they
    go by what the scanners knew at the start - and there is nothing for the
    log to say. Starting with the lamp unplugged is the first time it is out
    of reach, and that is said: by its name, and with its address once - a
    lamp picked from the list has its address in its title already.
    """
    entry = _an_entry()
    entry.async_create_background_task = lambda _hass, coro, _name: coro.close()
    coordinator, _lamp = _kept_for(
        hass, entry, "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)"
    )
    fake = MagicMock()
    fake.async_register_callback.return_value = lambda: None
    fake.async_track_unavailable.return_value = lambda: None
    fake.async_address_present.return_value = present
    monkeypatch.setattr(keeper_module, "bluetooth", fake)

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        await coordinator.async_start()
    try:
        assert coordinator.available is present
        if present:
            assert _info_lines(caplog) == []
        else:
            (said,) = _info_lines(caplog)
            assert said.startswith(
                "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF) is out of reach"
            )
    finally:
        await coordinator.async_stop()


async def test_a_command_that_never_reached_the_wire_fails_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp the scanner has lost fails the command at once, and as out of range.

    The dial says so without any I/O, so no byte ever left. Waiting the
    grace window for a notification that cannot arrive - there is no link -
    added two seconds to every command an automation sends to a lamp that is
    off or out of range. Of the reasons a command gets no link, this one is
    about range: the scanner no longer has the lamp, which is the radio's
    doing, so the message about range and a proxy is the right one for it,
    and the command goes round for its second attempt as it always did - the
    lamp may be heard again by then.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # nothing held, and the lamp is not in the list
    coordinator._mirror.echo({KEY_POWER: True})  # and the mirror happens to agree
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 30.0)

    async with asyncio.timeout(1):  # nowhere near the grace window
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "cannot_connect"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert lamp.dials == 2


@pytest.mark.parametrize(
    "stranger", ["one an earlier attempt gave up on", "one still being connected"]
)
async def test_an_old_client_disconnecting_does_not_drop_the_live_one(
    hass: HomeAssistant, stranger: str
) -> None:
    """The callback for a lost link checks WHICH client it is being told about.

    A failed write drops its client and the retry establishes another. When
    the OS later notices the first one is gone, bleak fires that client's
    callback - and letting go of the held client unconditionally there
    discarded the live connection instead. The link then read as not
    connected, so the poll opened yet another link to a lamp with a single
    slot, and every attempt failed with "out of connection slots" while the
    working connection sat there unreferenced until the lamp's own churn
    dropped it.

    And only the client the link holds is hung up from the callback. The
    first build of this fix hung up whichever client the callback named, as
    a second chance for a hang-up cut short by its ceiling. On the real lamp
    it lasted minutes: bleak reports a link lost in the middle of a connect
    to the same callback, while establish_connection is still working on
    that client. Disconnecting it there closed the bus underneath bleak's
    own clean-up - "Failed to cancel connection ... Bad file descriptor" on
    every such drop, and a retry that died on a bus that was no longer
    there.
    """
    coordinator, lamp, held = await _holding_a_link(hass)
    if stranger == "one an earlier attempt gave up on":
        lamp.fails_writes(BleakError("dropped"), times=1)
        await coordinator.async_set_power(True)  # retried, on a link of its own
        other, live = lamp.links  # the client an earlier attempt gave up on
        assert other.hung_up == 1
    else:
        live = held
        other = await lamp.dial(MagicMock())  # still inside establish_connection
    hang_ups = other.hang_ups

    link_of(coordinator).on_lost(other)
    await hass.async_block_till_done()

    assert link_of(coordinator).client is live
    assert link_of(coordinator).diagnostics()["connected"] is True
    assert live.hang_ups == 0
    assert other.hang_ups == hang_ups  # not hung up from the callback, nor again

    # The live one going down is still heard.
    live.lose()
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize("hang_up", ["at once", "slowly, past the deadline"])
async def test_a_connect_that_fails_half_way_leaves_no_link_behind(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hang_up: str
) -> None:
    """A connect that cannot finish must hang up, not abandon the link.

    The client is established before notifications are subscribed. If that
    subscription fails, walking away leaves a connected client holding the
    lamp's single slot with nothing referencing it: every later attempt then
    fails for want of a slot until the lamp's own churn drops it. And the
    client of a failed subscription is not disconnected on borrowed time: it
    was disconnected inline, inside the deadline of whatever was connecting,
    and a deadline that ran out during that disconnect cancelled it part-way
    - the bus left open, by the route the hang-up was written to close. The
    connect still waits for the hang-up, so that a retry does not dial over
    it; what the deadline ends now is that wait.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.subscription_fails(BleakError("subscribe failed"))
    released = asyncio.Event()
    if hang_up != "at once":
        lamp.hangs_up_when(released)

    with pytest.raises(BleakError if hang_up == "at once" else TimeoutError):
        await link_of(coordinator).connect()  # the deadline, while it waits

    if hang_up != "at once":
        assert not lamp.links[0].hung_up
        released.set()
        await hass.async_block_till_done()
    assert lamp.links[0].hung_up == 1
    # And nothing is left claiming to be live.
    assert link_of(coordinator).diagnostics()["connected"] is False


async def test_stopping_hangs_up_even_when_the_lock_is_busy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping neither waits out the lock a slow exchange holds nor abandons the link.

    Unload waiting behind a connect is what made reloading the integration
    take the best part of ten seconds. And a busy lock must not mean the
    connection is simply abandoned: dropping the reference does not close a
    BLE link - bleak has no disconnect on garbage collection - so the lamp's
    one slot stays taken and the next coordinator cannot have it.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.never_acknowledges_a_write()  # the exchange on the held link hangs...
    priming = asyncio.create_task(link_of(coordinator).prime_held())
    await asyncio.sleep(0)  # ...holding the lock

    try:
        async with asyncio.timeout(0.4):  # must return, not hang
            await coordinator.async_stop()
    finally:
        priming.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await priming

    assert link.hung_up == 1  # hung up anyway
    assert link_of(coordinator).diagnostics()["connected"] is False


async def test_one_background_connect_at_a_time(hass: HomeAssistant) -> None:
    """A connect still on its way is not joined by another.

    A dial to a lamp on a weak signal outlasts the poll's interval whenever
    it is given the chance, and the lamp advertises about once a second.
    Each of those starting a connect of its own would queue them up behind
    the lock, every one with its own deadline and its own line in the log.
    """
    spawned: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # started, and never done
        spawned.append(name)
        return MagicMock()

    entry = _an_entry()
    entry.async_create_background_task = _background
    coordinator, _lamp = _kept_for(hass, entry)

    link_of(coordinator).tick()
    link_of(coordinator).tick()
    link_of(coordinator).advertising(True)

    assert len(spawned) == 1


def test_no_path_holds_the_lock_longer_than_a_command_will_wait() -> None:
    """The timing constants have to make sense relative to each other.

    Every test that exercises a timeout patches it, so the shipped numbers are
    otherwise unconstrained - and they interact. A background connect holds the
    connection lock; a command waits for that same lock inside its own budget.
    If the holder is allowed longer than the waiter, pressing a switch during a
    background connect reports failure on a perfectly reachable lamp, having
    attempted nothing at all.
    """
    connect = link_module._CONNECT_TIMEOUT
    ask = link_module._ASK_TIMEOUT
    command = link_module._COMMAND_TIMEOUT
    hang_up = link_module._HANG_UP_TIMEOUT
    poll = link_module._RECONNECT_INTERVAL.total_seconds()

    assert connect < command, "a lock holder outlasting the waiter is an inversion"
    # A dial waits for a hang-up still under way, inside the connect's own
    # time. A hang-up allowed as long as the connect would leave no time to
    # dial once it had ended.
    assert hang_up < connect
    assert ask < command
    assert connect >= link_module._STOP_TIMEOUT
    # The library gives one try BLEAK_TIMEOUT before it gives up and tidies up
    # after itself. A ceiling below that cuts even the first try from outside,
    # in the middle of a connect - where, measured on a G7 whose connects take
    # five to ten seconds, they were about to finish. It still covers the wait
    # for the lock and the later tries, so this is a floor and not a promise.
    assert connect >= BLEAK_TIMEOUT
    # Three tries inside one dial: on a weak link a connection is often made
    # and lost within a second or two, and the next try is what gets through.
    # Agreed against that measurement; another number wants another one.
    assert link_module._CONNECT_ATTEMPTS == 3
    # Priming is spawned from the poll and takes the same lock, so it must be
    # finished before the next tick or the ticks pile up on top of each other.
    assert connect < poll
    assert ask < poll
    # A link that died without the stack noticing is found only by asking it,
    # so this is how long the entities can go on showing a lamp that is not
    # there. Five minutes was agreed; another number wants a reason of its own.
    assert link_module._PROBE_INTERVAL == 300
    # A write retry waits for the hang-up of the client it gave up on, inside
    # the command's budget and before it dials. A hang-up allowed as long as
    # the command leaves the retry no time to happen in exactly the case it is
    # for: a link that will not confirm it has closed.
    assert hang_up < command


@pytest.mark.parametrize("path", ["a background connect", "the poll"])
@pytest.mark.parametrize(
    "failure",
    [BleakError("Not connected"), *_BUS_CLOSED],
    ids=["not connected", "eof", "bad-fd"],
)
async def test_a_link_that_answers_nothing_is_dropped(
    hass: HomeAssistant, path: str, failure: Exception
) -> None:
    """A link that cannot even be read is not a working link, and is hung up.

    bleak can report a client as connected while BlueZ answers "Not
    connected" to everything - seen on real hardware: establish_connection
    returned a client whose every operation answered so while still
    reporting itself connected. Keeping it means the link reads as
    connected, so the poll never reconnects and the coordinator is wedged
    until the device's own churn; marking it primed regardless meant one
    failed attempt stopped the poll ever trying again, and the entities sat
    at one of fourteen indefinitely. The link can go under the state request
    or under the read that follows it, and the bus closing under either is
    the same dead link, not an exception with a traceback.

    So it is dropped where it is detected - by the poll's priming of a link
    a command took, or by the connect itself, not a poll tick later:
    observed live, a connect that kept such a client left a command in the
    next thirty seconds writing into it before failing and reconnecting.
    And forgetting a client is not disconnecting it: about two and a half
    hours after each start the system bus refused every new connection from
    Home Assistant's user, Bluetooth included, because bleak opens a D-Bus
    connection per client and closes it only in ``disconnect()``, and every
    client let go of by clearing the reference kept its connection until
    the bus's limit of 256 per user was reached. The hang-up is not waited
    for: both paths run under a deadline and hold the lock, and waiting
    there would keep the lock for as long as BlueZ takes to confirm and let
    the deadline cancel the disconnect part-way - the leak again, by the
    route the fix closes for commands.
    """
    if path == "the poll":
        coordinator, lamp, _link = await _holding_a_link(hass)
    else:
        coordinator, lamp = _at_a_lamp(hass)
    lamp.fails_writes(failure)
    lamp.fails_reads(failure)  # and there is nothing to read
    released = asyncio.Event()
    lamp.hangs_up_when(released)  # a real disconnect is not instant either

    async with asyncio.timeout(1):  # returns with the hang-up pending
        if path == "the poll":
            await link_of(coordinator).prime_held()
        else:
            await link_of(coordinator).connect()

    link = lamp.links[0]
    assert link_of(coordinator).diagnostics()["primed"] is False
    assert link_of(coordinator).diagnostics()["connected"] is False  # let go at once
    assert link.hang_ups == 1
    assert not link_of(coordinator).lock.locked()
    assert not link.hung_up
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1


def _counting_connects(coordinator: GlowriumCoordinator) -> list[int]:
    """Note each time the coordinator sets about getting a link.

    Counted where a link is asked for and not at the dial: a link that is
    refused - the coordinator has stopped, a client would not close - is
    refused before anything is dialled, and how often it was asked for is
    what these tests are about.
    """
    asked: list[int] = []
    link = link_of(coordinator)
    ask = link.open

    async def _counted() -> Any:
        asked.append(1)
        return await ask()

    link.open = _counted
    return asked


@pytest.mark.parametrize("failed", ["the write", "the subscription"])
async def test_a_retry_dials_only_after_the_client_it_gave_up_on_is_hung_up(
    hass: HomeAssistant, failed: str
) -> None:
    """The client a command failed on is closed, and closed before the retry dials.

    Order matters as much as the hang-up. The lamp has one slot: while BlueZ
    still shows the old link as up, a connect is handed that very link - the
    one being closed - and the hang-up then closes it underneath the retry.
    Hanging the client of a failed subscription up in the background lost
    that order: the retry dialled first, which on a link that reports itself
    connected while answering nothing means a second attempt on the link the
    first one had just failed on.
    """
    if failed == "the write":
        coordinator, lamp, _first = await _holding_a_link(hass)
        lamp.fails_writes(BleakError("dropped"), times=1)
    else:
        coordinator, lamp = _at_a_lamp(hass)
        lamp.subscription_fails(BleakError("Not connected"))
    released = asyncio.Event()
    lamp.hangs_up_when(released)  # a real disconnect is not instant either

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0.01)
    first = lamp.links[0]
    assert first.hang_ups == 1  # being hung up...
    assert lamp.dials == 1  # ...and not dialled again before that is through
    lamp.subscription_fails(None)  # the next link subscribes
    released.set()
    await command

    assert first.hung_up == 1
    assert lamp.dials == 2
    second = lamp.links[1]
    assert link_of(coordinator).client is second
    assert second.hang_ups == 0  # the link that worked is kept


@pytest.mark.parametrize("report", ["inside the failing write", "a moment after it"])
async def test_a_failed_command_hangs_up_only_after_the_device_could_confirm(
    hass: HomeAssistant, report: str
) -> None:
    """The last client is closed too - but not before confirmation has listened.

    A client whose write failed is still the channel the confirming report
    arrives on (see test_confirmation_waits_for_a_report_that_arrives_late,
    among the device half's tests).
    Hanging it up the moment the write fails would close the leak and quietly
    take that away: every command whose acknowledgement was lost would be
    reported as failed again.

    So the lamp here does what that window exists for. It acts on the write,
    the acknowledgement is lost, and its report arrives - with the failure,
    or a moment after it - on the link the write failed on, and only if that
    link is still up: a client that has been hung up delivers nothing. And
    being told the command worked is no reason to keep the leak: both
    clients are hung up once it has.
    """
    coordinator, lamp, first = await _holding_a_link(hass)
    still_up_when_reporting: list[bool] = []

    def _report_if_still_connected() -> None:
        second = lamp.links[-1]
        still_up_when_reporting.append(second.hang_ups == 0)
        lamp.say(cbor.encode({KEY_POWER: True}))  # nothing over a hung-up link

    if report == "inside the failing write":
        lamp.fails_writes(
            BleakError("GATT Protocol Error: Unlikely Error"),
            saying=lambda _frame: cbor.encode({KEY_POWER: True}),
        )
    else:
        lamp.fails_writes(BleakError("GATT Protocol Error: Unlikely Error"))
        # Acted on, and not acknowledged: the lamp's report comes a moment after.
        hass.loop.call_later(0.05, _report_if_still_connected)

    await coordinator.async_set_power(True)  # confirmed by the report
    await hass.async_block_till_done()

    if report == "a moment after it":
        assert still_up_when_reporting == [True]  # up while the device could answer
    assert coordinator.state[KEY_POWER] is True
    assert first.hung_up == 1
    assert lamp.links[1].hung_up == 1  # ...and hung up once it had
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize("behind_it", ["Home Assistant", "nothing: the bench"])
async def test_a_link_the_lamp_dropped_is_hung_up_as_well(
    hass: HomeAssistant, behind_it: str
) -> None:
    """The link going down by itself lets go of the client, and closes it.

    The callback for a lost link lets go of the client there and then:
    everything downstream goes by the link's ``connected`` (read here
    through ``link_of(coordinator).diagnostics()``), and a stale client left
    in place would read as connected, so the poll would never reconnect and
    every command would write into a dead handle. And bleak leaves the
    client's D-Bus connection open after the device disconnects; only
    ``disconnect()`` releases it. This was where the quota actually went: on
    the lamp it was found on, at the edge of range, the link came up on
    every poll tick and the lamp dropped it two to ten seconds later - 678
    times in one night - and each time the callback only cleared the
    reference.

    With no Home Assistant behind the coordinator too. tools/bench.py builds
    the real one, keeps it itself, and takes the paths the integration
    takes; when the hang-up was scheduled on a hass that was not there,
    every path that lets go of a client - a link the lamp drops, a connect
    that answers nothing, a write that needs its retry - ended in
    "'NoneType' object has no attribute 'async_create_task'", with the
    client still connected. Found by walking those three paths on a
    coordinator built the way the bench built it; 0.2.1 took all three.
    A hang-up is its keeper's to run since (``Keeper.run_lasting``).
    """
    with_hass = behind_it == "Home Assistant"
    coordinator, _lamp, link = await _holding_a_link(
        hass if with_hass else None, "Glowrium-G7" if with_hass else "bench"
    )
    assert link_of(coordinator).diagnostics()["connected"] is True

    link.lose()
    assert link_of(coordinator).diagnostics()["connected"] is False  # at once
    if with_hass:
        await hass.async_block_till_done()
    else:
        await asyncio.sleep(0)  # nothing to block on without hass; one turn does it

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hung_up == 1


async def test_a_hang_up_outlives_the_deadline_of_whoever_asked_for_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command running out of time must not take the hang-up with it.

    A disconnect that is cancelled part-way has asked BlueZ to drop the link
    and then walked away before closing its own D-Bus connection - the leak
    again, by another route.
    """
    coordinator, lamp, first = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.fails_writes(BleakError("dropped"))
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)  # the deadline ends the wait

    assert not first.hung_up
    assert lamp.dials == 1  # and it never dialled over the old link
    released.set()
    await hass.async_block_till_done()
    assert first.hung_up == 1  # the hang-up itself ran to the end


async def test_a_hang_up_that_fails_or_hangs_troubles_nobody(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hanging up is cleanup: it is bounded, and its failures stay in the log.

    bleak's disconnect passes on whatever the bus raised and ends on an
    assertion, and against a wedged BlueZ it can wait indefinitely. None of
    that may reach the path that gave the link up, or outlive the test - and
    none of it may vanish either: the log line says why a bus had to be closed
    by hand (tests/test_bus_lifetime.py is about the closing itself).
    """
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.05)
    caplog.set_level(logging.DEBUG, logger=link_module.__name__)
    for failure in (BleakError("gone"), OSError(9, "Bad file descriptor"), EOFError()):
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_hang_ups(failure)
        caplog.clear()

        await link_of(coordinator).hang_up(link)  # the failure does not come out

        assert link.hang_ups == 1
        assert link_of(coordinator).diagnostics()["connected"] is False
        assert f"failed: {failure!r}" in caplog.text

    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.hangs_up_when(asyncio.Event())  # never
    caplog.clear()
    async with asyncio.timeout(1):
        await link_of(coordinator).hang_up(link)  # ends at the ceiling, not never
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert "failed: TimeoutError()" in caplog.text


async def test_stopping_does_not_cut_the_hang_up_short(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnect that hangs costs the stop one ceiling, and is not cut short by it.

    Measured on the real integration: reloading took 6.1 s, all of it in
    unload, because a first bounded disconnect timed out and a fallback then
    timed out again. The lock and the hang-up are separate problems and need
    separate deadlines - the lock is best-effort, the disconnect is tried
    once. And unload stops waiting at its ceiling; the disconnect itself
    carries on. Stopping disconnected under its own ceiling of three seconds,
    and on the real integration that ceiling has fired. Run against bleak
    3.0.2 with a bus that never confirms: a disconnect cancelled at that
    ceiling has asked BlueZ to drop the link and returns with the client's
    D-Bus connection still open. That is the leak this fix is about, taken
    on every reload that meets a slow link - and a reload is what one
    reaches for when Bluetooth misbehaves.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    async with asyncio.timeout(0.4):  # comfortably under two ceilings plus slack
        await coordinator.async_stop()

    assert link.hang_ups == 1
    assert not link.hung_up
    assert link_of(coordinator).diagnostics()["connected"] is False
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1  # the disconnect ran to the end, after the unload


async def test_stopping_is_not_broken_by_what_the_bus_raises(
    hass: HomeAssistant,
) -> None:
    """A dead bus must not make stopping raise.

    With the bus's quota spent, bleak's calls end in EOFError and "Bad file
    descriptor" - neither a BleakError. Stopping caught only BleakError and
    TimeoutError and let these through. Home Assistant runs the unload callback
    as a task and does not pass its exception on, so the reload still went
    through - with an unretrieved exception left in the log, in exactly the
    state where reloading is the remedy and the log is what gets read.
    """
    for failure in (OSError(9, "Bad file descriptor"), EOFError()):
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_hang_ups(failure)

        await coordinator.async_stop()

        assert link.hang_ups == 1
        assert link_of(coordinator).diagnostics()["connected"] is False


# The two ways a connect is started in the background, and what each is called
# in the log.
_BACKGROUND_CONNECTS = (
    ("reconnect", "Reconnect to"),
    ("initial_connect", "Initial connect to"),
)


async def _never_returns(*_args: object, **_kwargs: object) -> None:
    """Stand in for a call the deadline finds still waiting."""
    await asyncio.Event().wait()


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
async def test_a_deadline_that_falls_on_a_held_link_leaves_it_and_calls_it_held(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
) -> None:
    """A connect that got its link and ran out of time after it has not failed.

    Seen on the G7's host under load (2026-10-06): the connect went through
    late, the ceiling ran out during the first exchange, and the log said
    "Reconnect to ... failed" of a link the coordinator went on holding. The
    link is not let go of - the poll primes it on its next tick, without
    another dial - and the log says that it is held, so whoever reads it is
    not sent looking for a lamp out of range.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.never_acknowledges_a_write()
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()

    client = lamp.links[0]
    assert link_of(coordinator).client is client  # taken, and not let go of
    assert client.hang_ups == 0
    assert f"{named} AA:BB:CC:DD:EE:FF ran out of time" in caplog.text
    assert "the link is held (not primed yet)" in caplog.text
    assert "failed" not in caplog.text

    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    link_of(coordinator).tick()
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["primed"] is True
    assert lamp.dials == 1  # the same link, primed; nothing was redialled


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
@pytest.mark.parametrize(
    "ended_by", ["the link's own no", "a write that lost its link", "a refusal"]
)
async def test_a_background_connect_ends_in_the_log_whatever_ended_it(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
    ended_by: str,
) -> None:
    """Nothing the link itself raises gets out of a connect made in the background.

    Its errors are its own since the split (#21), no longer the Bluetooth
    library's, and a background connect is a task nobody waits for: an error
    that got out of it would be an ERROR in the log - "Task exception was
    never retrieved" - for a lamp that had only gone out of reach. It ends
    as a line of its own whichever of them ended it: the link's "no" to a
    new client, a write of the first exchange that lost its link, or one the
    lamp refused.
    """
    kept = link_module.Unclosed()
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass, unclosed=kept)
    if ended_by == "the link's own no":
        kept.keep(object(), None)  # a client that would not close
    else:
        failure = BleakError(
            "Not connected"
            if ended_by == "a write that lost its link"
            else "Insufficient authorization (8)"
        )
        # A lamp that reports itself not activated: the exchange has to write.
        lamp.answers({KEY_ACTIVATED: False})
        lamp.fails_writes(failure, of=WRITE_UUID)
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()  # and nothing is raised

    assert f"{named} AA:BB:CC:DD:EE:FF failed: " in caplog.text


async def test_a_deadline_that_falls_after_priming_says_the_link_is_primed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The device-info read comes last, and can be what the deadline cuts.

    The state has arrived by then and the link is primed; the poll has nothing
    to add to it, so the line must not promise that it will.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    lamp.never_answers_a_read()
    caplog.set_level(logging.DEBUG)

    await link_of(coordinator).reconnect()

    assert link_of(coordinator).diagnostics()["primed"] is True
    assert "the link is held (primed)" in caplog.text
    assert "failed" not in caplog.text


async def test_a_deadline_spent_waiting_behind_a_command_that_connected_is_no_failure(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A command can take the link while the background connect waits its turn.

    The connect then runs out of time without having dialled at all, and the
    lamp is connected all the same - by the command, which does not prime.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    found = asyncio.Event()
    lamp.dials_when(found)
    lamp.never_acknowledges_a_write()
    caplog.set_level(logging.DEBUG)

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # the command's turn: it has the lock, and dials
    waiting = asyncio.create_task(link_of(coordinator).reconnect())
    await asyncio.sleep(0)  # ...and the connect waits behind it
    found.set()  # the link the command made, unprimed; its write never returns
    await waiting
    command.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await command

    assert lamp.dials == 1
    assert "the link is held (not primed yet)" in caplog.text
    assert "failed" not in caplog.text


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
async def test_a_connect_that_gets_no_link_is_still_called_a_failed_connect(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
) -> None:
    """The deadline running out with nothing held is the failure it always was."""
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.dials_through(_never_returns)
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert f"{named} AA:BB:CC:DD:EE:FF failed" in caplog.text
    assert "the link is held" not in caplog.text


async def test_a_connect_that_fails_on_a_held_link_keeps_the_error_it_failed_with(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only running out of time is reworded: an error says what it was."""
    coordinator, lamp = _at_a_lamp(hass)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    coordinator._async_sync_clock_if_needed = AsyncMock(
        side_effect=BleakError("the clock would not be set")
    )
    caplog.set_level(logging.DEBUG)

    await link_of(coordinator).reconnect()

    assert link_of(coordinator).client is lamp.links[0]  # held all the same
    assert "Reconnect to AA:BB:CC:DD:EE:FF failed: the clock would not" in caplog.text
    assert "the link is held" not in caplog.text


@pytest.mark.parametrize(
    ("error", "said"),
    [
        pytest.param(TimeoutError(), "TimeoutError()", id="a deadline"),
        pytest.param(EOFError(), "EOFError()", id="a bus closed under a call"),
        pytest.param(BleakError("Not connected"), "Not connected", id="a lost link"),
        pytest.param(
            OSError(9, "Bad file descriptor"),
            "[Errno 9] Bad file descriptor",
            id="bad-fd",
        ),
    ],
)
@pytest.mark.parametrize(
    "where", ["a reconnect", "an initial connect", "the state read", "the priming"]
)
async def test_an_error_goes_into_the_log_by_what_it_says_or_else_by_what_it_is(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    where: str,
    error: Exception,
    said: str,
) -> None:
    """Every line about a lost link names what lost it: its words, or else its name.

    The two errors with nothing to say are the two commonest on a weak link.
    Running out of time is the commonest way for a connect to fail, and
    ``TimeoutError`` carries no message: one night on the G7's host left 290
    lines that ended in "failed: " with nothing after it (2026-10-08). Not
    the connect alone: priming that ran out its deadline and a read whose
    bus was closed under it said "failed: " and stopped there too. And
    neither background connect lets the error out: they run as tasks nobody
    awaits, so what escapes one is not handled by anybody - it ends up in
    the log as an exception with a traceback, on every poll tick for as long
    as the bus stays the way it is.
    """
    assert link_module._reason(error) == said
    caplog.set_level(logging.DEBUG)
    if where == "the state read":
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_reads(error)
        assert not await coordinator._async_read_state(turn_over(coordinator, link))
        line = f"AA:BB:CC:DD:EE:FF state read failed: {said}"
    elif where == "the priming":
        coordinator, lamp, _link = await _holding_a_link(hass)
        coordinator._request_state = AsyncMock(side_effect=error)
        await link_of(coordinator).prime_held()
        line = f"Priming state of AA:BB:CC:DD:EE:FF failed: {said}"
    else:
        coordinator, lamp = _at_a_lamp(hass)
        lamp.dials_through(AsyncMock(side_effect=error))
        if where == "a reconnect":
            await link_of(coordinator).reconnect()  # and nothing is raised
            line = f"Reconnect to AA:BB:CC:DD:EE:FF failed: {said}"
        else:
            await link_of(coordinator).initial_connect()  # nor here
            line = f"Initial connect to AA:BB:CC:DD:EE:FF failed: {said}"
    assert line in caplog.text


async def test_hanging_up_a_client_we_gave_up_on_leaves_the_one_we_hold(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hang-up after the confirmation window must not drop a newer link.

    A command that failed twice lets go of its client at once but hangs it up
    only after the confirmation window, which is spent outside the lock. A
    second command queued behind it takes the lock in that window and
    establishes a link of its own. When the first command then hangs up the
    client it gave up on, the coordinator must still hold the second one:
    forgetting it would leak that client's D-Bus connection - the very thing
    the hang-up exists to prevent - and leave the lamp's single slot taken by
    nobody.
    """
    coordinator, lamp, _first = await _holding_a_link(hass)
    # Twice: on the link held, and on the one the retry makes. The command fails.
    lamp.fails_writes(BleakError("Unlikely Error"), times=2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.2)

    failing = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # it has failed twice and now waits for a report
    second = lamp.links[1]
    # Let go of at once, not after the wait.
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert second.hang_ups == 0

    await coordinator.async_set_brightness(40)  # queued behind it; connects
    third = lamp.links[2]  # the next command's link works
    assert link_of(coordinator).client is third

    with pytest.raises(HomeAssistantError):
        await failing
    await hass.async_block_till_done()

    assert second.hung_up == 1  # the abandoned client is closed
    assert link_of(coordinator).client is third  # ...and the live one is still ours
    assert third.hang_ups == 0


async def test_a_connect_cancelled_half_way_is_hung_up_but_not_waited_for(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that cancels the subscription still gets the client hung up.

    And is not kept waiting for it. The connect holds the lock, and whoever set
    the deadline has already stopped waiting: staying on here for as long as
    BlueZ takes to close the link would hold the lock past the deadline for
    nobody's benefit.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.subscribes_when(asyncio.Event())  # never
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    async with asyncio.timeout(0.5):
        with pytest.raises(TimeoutError):
            await link_of(coordinator).connect()

    link = lamp.links[0]
    assert link.hang_ups == 1
    assert not link_of(coordinator).lock.locked()
    assert not link.hung_up
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_priming_that_meets_a_closed_bus_only_logs_it(
    hass: HomeAssistant, failure: Exception
) -> None:
    """The same for the priming the poll does on a link a command made.

    Here the link goes after the read has answered, under the bring-up write.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_ACTIVATED: False}))
    lamp.fails_writes(failure)

    await link_of(coordinator).prime_held()

    assert link_of(coordinator).diagnostics()["primed"] is False


async def test_stopping_lets_go_of_a_link_made_while_it_was_stopping(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command in flight must not leave a stopped coordinator holding a link.

    Stopping took whatever client it held at that instant and was done. A
    command still running - its write had failed and its retry was dialling -
    then finished its connect and committed the new link to a coordinator
    that no longer watches anything and that nobody will stop again. On a
    lamp whose link holds, that keeps the single slot for good: the
    coordinator a reload puts in its place cannot connect.
    """
    coordinator, lamp, _first = await _holding_a_link(hass)
    lamp.fails_writes(BleakError("dropped"), times=1)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    answered = asyncio.Event()
    lamp.dials_when(answered)

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # its write has failed; the retry is dialling
    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)
    answered.set()  # ...and the lamp picks up, after the stop began

    with pytest.raises(HomeAssistantError):
        await command  # the coordinator was stopped under it
    await stopping
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert lamp.links[1].hung_up == 1


async def test_stopping_that_is_cancelled_still_lets_go_of_the_link(
    hass: HomeAssistant,
) -> None:
    """Cancelled while it waits for the lock, stopping still hangs up.

    It took the client first and waited afterwards, so a cancellation in that
    wait dropped the only reference to a connected client - the leak, and the
    lamp's slot held by nobody.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.never_acknowledges_a_write()
    command = asyncio.create_task(coordinator.async_set_power(True))  # in flight
    await asyncio.sleep(0)  # it holds the lock, in its write
    try:
        stopping = asyncio.create_task(coordinator.async_stop())
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hung_up == 1


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_a_command_refused_because_it_has_stopped_says_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, stopped_by: str
) -> None:
    """A stopped coordinator takes no new link at all, says so, and says so at once.

    The command is refused because the integration is being reloaded or
    Home Assistant is going down. The radio did nothing, and "the device may
    be out of range ... a Bluetooth proxy near the device usually fixes
    this" is advice for something else: a stopped coordinator does not send
    its user off to buy a Bluetooth proxy. It is said as what it is - and
    once: a link that is refused on purpose is not asked for a second time.
    And at once: the deadline around the command is what tells a refusal
    from a command that never got the lock - stopping takes the lock, and
    one that failed to give it back would end the same way fifteen seconds
    later, for a different reason.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    asked = _counting_connects(coordinator)
    if stopped_by == "an unload":
        await coordinator.async_stop()
    else:
        coordinator.async_shutdown()
    assert not link_of(coordinator).lock.locked()

    async with asyncio.timeout(1):
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "not_running"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert len(asked) == 1
    assert lamp.dials == 1  # the link it was stopped with; none since
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize(
    "meanwhile", ["the stack says nothing", "the stack reports it lost"]
)
async def test_a_command_that_runs_out_of_time_inside_a_write_lets_go_of_the_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    """A write that has not come back by the deadline is a link not to keep.

    The caller is told the command failed. Left held, the link gets the next
    command too, which waits just as long and fails the same way - until the
    probe, minutes later, finds it dead. Let go of, it is hung up, and the
    next command dials. The stack may report the link gone while the write
    is still waiting: then it has been let go of already, by the callback,
    and the deadline finds nothing of it left to take - one hang-up, not a
    second one for a client that is no longer the coordinator's.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.never_acknowledges_a_write()
    if meanwhile == "the stack reports it lost":
        hass.loop.call_later(0.01, link.lose)  # dropped, while the write waits

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "cannot_connect"
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hang_ups == 1
    assert link.hung_up == 1


async def test_shutting_down_does_not_hold_home_assistant_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On shutdown the hang-up is fired at once, and given a short ceiling.

    Home Assistant waits for whatever its stop listeners start, and a
    container is killed ten seconds after it is told to stop. What matters
    then is that BlueZ is asked to drop the link straight away - not that the
    lock is free, and not that a link which will not confirm it has closed is
    waited on for the ten seconds a hang-up is normally allowed: that would
    spend the whole grace period, and the rest of Home Assistant's shutdown
    with it. The bus is not worth waiting for either; the process is leaving.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.hangs_up_when(asyncio.Event())  # never
    lamp.never_acknowledges_a_write()
    command = asyncio.create_task(coordinator.async_set_power(True))  # in flight
    await asyncio.sleep(0)  # it holds the lock: not waited for
    try:
        coordinator.async_shutdown()

        assert link_of(coordinator).diagnostics()["connected"] is False
        assert link.hang_ups == 1  # asked to drop it, already
        async with asyncio.timeout(1):  # nowhere near _HANG_UP_TIMEOUT
            await hass.async_block_till_done()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


async def test_shutting_down_says_so_in_the_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The one line that shows, afterwards, that the stop was answered.

    Home Assistant is on its way out when this runs, so nothing else will say
    whether the coordinator held a link at that moment and let go of it. On a
    host where the phantom link has been seen, that is the first question.
    """
    caplog.set_level(logging.DEBUG, logger=link_module.__name__)
    coordinator, _lamp, _link = await _holding_a_link(hass)

    coordinator.async_shutdown()
    await hass.async_block_till_done()
    assert "Home Assistant is stopping: hanging up" in caplog.text

    caplog.clear()
    idle, _idle_lamp = _at_a_lamp(hass)

    idle.async_shutdown()
    assert "Home Assistant is stopping: no link held" in caplog.text


async def test_stopping_with_no_link_does_not_wait_for_a_busy_lock(
    hass: HomeAssistant,
) -> None:
    """With nothing held, unload has nothing to wait for.

    A command that is still dialling holds the lock and holds no link. Once
    the coordinator is stopped that command is refused the link it is dialling
    for, so nothing can turn up while unload waits - and waiting anyway put
    back the reload that takes seconds for no reason, which a ceiling on that
    wait had been introduced to remove.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.dials_when(asyncio.Event())  # a command, dialling - for as long as it takes
    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)
    try:
        async with asyncio.timeout(0.5):  # _STOP_TIMEOUT is three seconds
            await coordinator.async_stop()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


async def test_a_hang_up_started_after_the_stop_began_is_as_short(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hang-up Home Assistant will wait for gets the short ceiling.

    Not only the one for the link held when the stop was announced. A connect
    cancelled by the stop, or a command finishing after it, lets go of a
    client later - and Home Assistant waits for whatever starts after it began
    to stop. Left under the usual ten seconds, one link that will not confirm
    it has closed spends the whole grace period a container is given.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.hangs_up_when(asyncio.Event())  # never
    late = await lamp.dial(MagicMock())  # a client let go of after the stop began

    coordinator.async_shutdown()
    link_of(coordinator).hang_up(late)

    async with asyncio.timeout(1):  # nowhere near _HANG_UP_TIMEOUT
        await hass.async_block_till_done()
    assert late.hang_ups == 1


async def test_stopping_gives_the_lock_back_however_it_ends(
    hass: HomeAssistant,
) -> None:
    """Cancelled while it waits for the hang-up, stopping still releases the lock.

    It takes the lock so that the hang-up does not race a write in flight. A
    lock kept by a coordinator that has stopped would hold up nothing that
    matters - but one kept by a stop that was cancelled and may be tried again
    would hold up that.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)  # it holds the lock and waits for the hang-up
    assert link_of(coordinator).lock.locked()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert not link_of(coordinator).lock.locked()
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1  # and the hang-up was not cancelled with it


@pytest.mark.parametrize("asked_by", ["a command", "a background connect"])
async def test_a_link_subscribed_while_the_coordinator_stopped_is_not_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, asked_by: str
) -> None:
    """The check for a stop comes after the subscription, not before it.

    The subscription is the last thing a connect waits for before it commits
    the link. A stop that comes and goes during that wait finds nothing held
    and returns; checked any earlier, the connect would then commit its link
    to a coordinator that has already been stopped. A command whose own
    connect was on its way is told why - not sent round a second time to be
    refused at the door instead.
    """
    coordinator, lamp = _at_a_lamp(hass)
    subscribed = asyncio.Event()
    lamp.subscribes_when(subscribed)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    asked = _counting_connects(coordinator)

    if asked_by == "a command":
        connecting = asyncio.create_task(coordinator.async_set_power(True))
    else:
        connecting = asyncio.create_task(link_of(coordinator).connect())
    await asyncio.sleep(0)  # dialled, and now waiting to be subscribed
    await coordinator.async_stop()  # nothing held: over at once
    subscribed.set()

    if asked_by == "a command":
        with pytest.raises(HomeAssistantError) as err:
            await connecting
        assert err.value.translation_key == "not_running"
    else:
        with pytest.raises(link_module.NoNewLinkError):
            await connecting
    await hass.async_block_till_done()

    assert len(asked) == 1
    assert lamp.dials == 1
    assert lamp.links[0].hung_up == 1
    assert link_of(coordinator).diagnostics()["connected"] is False
