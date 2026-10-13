"""Tests of the keeper of a config entry: what Home Assistant is asked for.

The watchers it registers and ends, the two lifetimes of background work,
the home's coordinates, the dial it makes for an entry, the device
registry and the remembered model, and the notice about unconfirmed
presets: when it is raised and when it goes. The notice's words and its
link into the guide are with the setup's tests, in test_init.py. The repair
for a stack that will not hang up is tested where such a stack is built, in
test_bus_lifetime.py.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

from homeassistant.const import CONF_ADDRESS, CONF_MODEL_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.glowrium import (
    cbor,
    keeper as keeper_module,
    link as link_module,
    models,
)
from custom_components.glowrium.const import (
    DOMAIN,
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_CIRCADIAN,
    WRITE_UUID,
)
from custom_components.glowrium.models import GlowriumModel

from .lamp import ScriptedLamp, in_range, link_of
from .shared import (
    ADDRESS,
    G7_INFO,
    _an_entry,
    _described,
    _device,
    _entry,
    _kept_for,
    _notices,
    _reads_what_the_lamp_is,
    _setup_without_bluetooth,
    _slow_to_connect,
    _started,
)


async def test_the_home_sync_location_writes_is_home_assistants(
    hass: HomeAssistant,
) -> None:
    """Kept for a config entry, the home is the one Home Assistant was given."""
    coordinator, lamp = _kept_for(hass, _an_entry())
    hass.config.latitude = 12.3456
    hass.config.longitude = 65.4321

    await coordinator.async_sync_location()

    assert lamp.written == [(WRITE_UUID, cbor.encode({0x0A: 12.3456, 0x0B: 65.4321}))]


async def test_a_start_that_fails_part_way_leaves_no_watcher_behind(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What was begun before the start failed is ended by the stop that follows.

    A setup that fails is stopped like any other (Home Assistant runs what
    the entry was told to run on unload), and it is retried: a watcher that
    stop could not reach would be one more on every retry, each answering
    the lamp's advertisements for a coordinator nobody has.
    """
    ended = MagicMock()
    fake = MagicMock()
    fake.async_register_callback.return_value = ended
    fake.async_track_unavailable.side_effect = RuntimeError("no scanner")
    monkeypatch.setattr(keeper_module, "bluetooth", fake)
    entry = _an_entry()
    coordinator, _lamp = _kept_for(hass, entry)

    with pytest.raises(RuntimeError, match="no scanner"):
        await coordinator.async_start()
    await coordinator.async_stop()

    assert ended.call_count == 1


async def test_starting_watches_for_the_device(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start registers both bluetooth watchers and seeds presence.

    Without the advertisement callback the lamp is never noticed coming back;
    without the unavailable tracker its entities never go unavailable when it
    is unplugged.
    """
    handed_over: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # the test does not run it, but must not leak it
        handed_over.append(name)
        return MagicMock()

    entry = _an_entry()
    entry.async_create_background_task = _background
    # This is about the watchers, not the link.
    coordinator, _lamp = _kept_for(hass, entry)
    calls: list[str] = []
    fake = MagicMock()
    fake.async_register_callback.side_effect = lambda *a, **k: (
        calls.append("advertisement") or (lambda: None)
    )
    fake.async_track_unavailable.side_effect = lambda *a, **k: (
        calls.append("unavailable") or (lambda: None)
    )
    fake.async_address_present.side_effect = lambda *a, **k: calls.append("presence")
    fake.BluetoothCallbackMatcher = MagicMock()
    fake.BluetoothScanningMode = MagicMock()
    monkeypatch.setattr(keeper_module, "bluetooth", fake)

    await coordinator.async_start()
    try:
        assert sorted(calls) == ["advertisement", "presence", "unavailable"]
        # Each for this lamp, by the address its entry was set up with.
        matcher = fake.BluetoothCallbackMatcher.call_args.kwargs
        assert matcher == {"address": "AA:BB:CC:DD:EE:FF", "connectable": True}
        for asked in (fake.async_track_unavailable, fake.async_address_present):
            assert asked.call_args.args[-1] == "AA:BB:CC:DD:EE:FF"
            assert asked.call_args.kwargs == {"connectable": True}
        # The first connect is handed to the entry, which is what cancels it
        # on unload rather than letting it outlive the coordinator.
        assert len(handed_over) == 1
    finally:
        await coordinator.async_stop()


async def test_background_work_is_tied_to_the_entry(hass: HomeAssistant) -> None:
    """Reconnects and priming must die with the entry, like the first connect.

    Tasks created on hass are only awaited at shutdown, so on a reload one
    outlives its coordinator, finishes connecting, and claims the lamp's only
    slot for a coordinator nobody owns - while the replacement cannot connect.
    """
    spawned: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()
        spawned.append(name)
        return MagicMock()

    entry = _an_entry()
    entry.async_create_background_task = _background
    coordinator, _lamp = _kept_for(hass, entry)

    link_of(coordinator).tick()
    assert len(spawned) == 1  # the reconnect went to the entry, not to hass

    await coordinator.async_set_power(True)  # a link a command made, not primed
    link_of(coordinator).tick()
    assert len(spawned) == 2  # and so does the priming


async def test_a_hang_up_is_not_tied_to_the_entry(hass: HomeAssistant) -> None:
    """The one background task that must survive the entry being unloaded: hass's.

    Everything else is created on the entry so that it dies with it (see
    test_background_work_is_tied_to_the_entry): a connect that outlives its
    coordinator claims the lamp's slot for nobody. A hang-up is the opposite
    - cancelled by an unload, it leaves the slot taken and the D-Bus
    connection open. Kept for an entry, it is hass's task to see through;
    the bench keeps its own, and this is the other side of that choice. A
    task hass does not track is one async_block_till_done walks straight
    past, and that call is how Home Assistant - and every test here - lets
    pending work settle before it looks at the result.
    """
    entry = _an_entry()
    coordinator, lamp = _kept_for(hass, entry)
    await coordinator.async_set_indicator(True)  # a link, taken by a command
    link = lamp.links[-1]
    released = asyncio.Event()
    lamp.hangs_up_when(released)
    hass.loop.call_later(0.05, released.set)  # it takes a moment

    link_of(coordinator).hang_up(link)
    await hass.async_block_till_done()

    entry.async_create_background_task.assert_not_called()
    assert link.hung_up == 1


async def test_what_the_watchers_hear_reaches_the_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lamp heard, the lamp gone and the timer each reach the link.

    Home Assistant's watchers are its keeper's, and each is one line that
    hands on what it saw to what the device half gave it for the link: an
    advertisement, the lamp no longer heard, the tick. What the link then
    does is tested on the link itself; this holds the three lines, which
    nothing else did once the tests had crossed the seam - three mutants
    of the stage 2 gate outlived stage 3.
    """
    heard: dict[str, Callable[..., None]] = {}

    def _register(
        _hass: object, callback: Callable[..., None], *_a: object, **_k: object
    ) -> Callable[[], None]:
        heard["advertisement"] = callback
        return lambda: None

    def _track(
        _hass: object, callback: Callable[..., None], *_a: object, **_k: object
    ) -> Callable[[], None]:
        heard["unavailable"] = callback
        return lambda: None

    fake = MagicMock()
    fake.async_register_callback.side_effect = _register
    fake.async_track_unavailable.side_effect = _track
    fake.async_address_present.return_value = False
    monkeypatch.setattr(keeper_module, "bluetooth", fake)
    entry = _an_entry()
    # Run what is handed over: a connect that fails has to end as one.
    entry.async_create_background_task = lambda _hass, coro, name: (
        hass.async_create_task(coro, name)
    )
    coordinator, lamp = _kept_for(hass, entry)
    lamp.out_of_range()  # every dial fails at once, and is counted

    await coordinator.async_start()
    try:
        await hass.async_block_till_done()  # the initial connect, failing
        assert not coordinator.available

        heard["advertisement"](MagicMock(), MagicMock())
        assert coordinator.available  # the link heard the lamp
        await hass.async_block_till_done()  # the reconnect it set off, failing

        heard["unavailable"](MagicMock())
        assert not coordinator.available  # and heard it go

        dials = lamp.dials
        later = dt_util.utcnow() + link_module._RECONNECT_INTERVAL
        async_fire_time_changed(hass, later + timedelta(seconds=1))
        await hass.async_block_till_done()
        assert lamp.dials == dials + 1  # the tick dialled
    finally:
        await coordinator.async_stop()


async def test_a_reconnect_started_while_starting_belongs_to_the_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertisement Home Assistant replays on registration is not an orphan.

    When it already knows the device, Home Assistant calls the advertisement
    callback at once, from inside async_register_callback - and a lamp that
    has been heard is already known, so that is every reload.
    The coordinator took it for the lamp reappearing and started a reconnect
    before it had been handed its entry. With no entry to put it on, the task
    went to hass, where an unload does not reach it.

    Seen on the real integration after two reloads in a row: "Reconnect to ...
    failed" logged ten seconds after the coordinator that started it had been
    unloaded, while its successor's own connects were answered "In Progress".
    On a lamp whose link holds, a connect that outlives its coordinator keeps
    the single slot for nobody.
    """
    handed_over: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # the test does not run it, but must not leak it
        handed_over.append(name)
        return MagicMock()

    entry = _an_entry()
    entry.async_create_background_task = _background
    coordinator, lamp = _kept_for(hass, entry)
    lamp.out_of_range()

    def _register(_hass: object, callback: object, *_a: object) -> object:
        callback(MagicMock(), MagicMock())  # the replayed advertisement
        return lambda: None

    fake = MagicMock()
    fake.async_register_callback.side_effect = _register
    fake.async_track_unavailable.return_value = lambda: None
    fake.async_address_present.return_value = True
    monkeypatch.setattr(keeper_module, "bluetooth", fake)

    await coordinator.async_start()
    try:
        await hass.async_block_till_done()
        assert len(handed_over) == 2
        assert any("reconnect" in name for name in handed_over)
    finally:
        await coordinator.async_stop()


async def test_the_lamp_of_an_entry_is_looked_up_by_its_address(
    hass: HomeAssistant,
) -> None:
    """The dial the setup hands the coordinator asks the scanners for this lamp.

    By the address the entry was set up with, as a device that can be
    connected to - and not by the entry's title, which is what the lamp is
    called and is handed to the same dial beside it, for the log.
    """
    entry = await _setup_without_bluetooth(hass)
    lamp = ScriptedLamp()

    with in_range(lamp) as looked_up:
        await entry.runtime_data.async_set_power(True)

    looked_up.assert_called_once_with(hass, ADDRESS, connectable=True)
    assert lamp.dials == 1


async def test_what_the_lamp_says_about_itself_reaches_the_device_page(
    hass: HomeAssistant,
) -> None:
    """The device-info string is read after the entities exist, and must still land.

    Setup stopped waiting for the first connect in 0.2.0. Since then the
    entities have described the device from a coordinator that had read
    nothing yet, and nobody told the registry when it had: on the real host
    the page showed the model as "Glowrium" and no model id, firmware or
    serial at all - the very fields a bug report is asked to quote.
    """
    entry = await _setup_without_bluetooth(hass)
    assert _device(hass).model_id is None  # nothing read yet

    await _reads_what_the_lamp_is(entry, G7_INFO)

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )


async def test_the_model_is_remembered_from_one_start_to_the_next(
    hass: HomeAssistant,
) -> None:
    """The profile a lamp gets must not depend on the order things happen in.

    Entities are built at setup, the device-info string arrives later, and the
    per-model profile is chosen by what that string says. So the model is
    kept with the config entry once it has been read, and the next start
    knows it before the first entity exists.
    """
    entry = await _setup_without_bluetooth(hass)
    assert entry.runtime_data.model.name == "Glowrium"  # generic until read
    await _reads_what_the_lamp_is(entry, G7_INFO)
    assert entry.data[CONF_MODEL_ID] == "Glowrium-C051"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup_without_bluetooth(hass, entry)

    restarted = entry.runtime_data
    assert not restarted.device_info  # nothing read in this session
    assert restarted.model is models.G7
    assert restarted.model_id == "Glowrium-C051"


async def test_a_model_without_a_profile_still_shows_what_it_is(
    hass: HomeAssistant,
) -> None:
    """An unknown model id is shown as itself, under the family's name."""
    entry = await _setup_without_bluetooth(hass)

    await _reads_what_the_lamp_is(
        entry, b"brand:INLEDCO;pkey:Glowrium-C064;devid:CST-9;version:2;;"
    )

    assert _described(_device(hass)) == ("Glowrium", "Glowrium-C064", "2", "CST-9")
    assert entry.data[CONF_MODEL_ID] == "Glowrium-C064"


async def test_a_string_that_names_no_model_leaves_the_one_on_record(
    hass: HomeAssistant,
) -> None:
    """Nor is a model the lamp did not name this time replaced by a guess.

    With no model id read and none remembered, the profile in use is the
    reference one - which is how the lamp is driven, not what it is.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)},
        model="Glowrium G8",
        model_id="Glowrium-C064",
    )
    await _setup_without_bluetooth(hass, entry)

    await _reads_what_the_lamp_is(entry, b"brand:INLEDCO;version:7;;")

    assert _described(_device(hass)) == ("Glowrium G8", "Glowrium-C064", "7", None)


async def test_a_model_without_a_profile_is_put_in_front_of_its_owner_at_start(
    hass: HomeAssistant,
) -> None:
    """The presets of such a lamp are the G7's, and Home Assistant says so.

    The select offers eight names and writes the G7's index for the one
    chosen, whatever the lamp makes of it; until now only the README said
    that this is a guess. The notice is raised from what the entry
    remembers, before anything is read: the lamp may be out of reach for
    hours. It asks for nothing that can be done from the dialog, links to
    what would settle the question, and is named by the entry and not by the
    lamp's address - Home Assistant lists the ids of open repairs in the
    diagnostics download.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})

    (notice,) = _notices(hass)
    assert notice.translation_key == "presets_unconfirmed"
    assert notice.translation_placeholders == {
        "name": "Glowrium-G7_1234",
        "model": "Glowrium-C064",
    }
    assert notice.severity is ir.IssueSeverity.WARNING
    assert notice.is_fixable is False
    assert notice.learn_more_url is not None
    assert notice.learn_more_url.endswith(
        "/CONTRIBUTING.md#sending-protocol-data-for-a-device"
    )
    assert entry.entry_id in notice.issue_id
    address = ADDRESS.lower()
    assert address not in notice.issue_id.lower()
    assert address.replace(":", "") not in notice.issue_id.lower()


@pytest.mark.parametrize(
    "remembered",
    [
        pytest.param({CONF_MODEL_ID: "Glowrium-C051"}, id="a G7"),
        pytest.param({}, id="a lamp that has never said what it is"),
    ],
)
async def test_a_lamp_with_a_profile_or_one_not_yet_known_is_told_nothing(
    hass: HomeAssistant, remembered: dict[str, str]
) -> None:
    """Not knowing the model is not the same as knowing it has no profile.

    A lamp set up for the first time has no model id until its device-info
    string has been read, and it is driven by the same generic profile
    meanwhile. A G7 in that state must not be told that its presets are a
    guess - nor at any time after.
    """
    await _started(hass, **remembered)

    assert _notices(hass) == []


_G8_INFO = b"brand:INLEDCO;pkey:Glowrium-C064;devid:CST-0009;mac:x;version:2;;"


@pytest.mark.parametrize(
    ("says", "told"),
    [
        pytest.param(_G8_INFO, True, id="a model without a profile"),
        pytest.param(G7_INFO, False, id="a G7"),
    ],
)
async def test_the_notice_comes_in_the_session_the_model_is_first_read_in(
    hass: HomeAssistant, says: bytes, told: bool
) -> None:
    """A lamp set up today is told today, and not after the next restart.

    Nothing is known of its model until its device-info string has been read,
    which is the last thing the first exchange does. What it names is judged
    there and then: a model without a profile raises the notice, and a G7
    raises none - at that point or at any other.
    """
    entry = await _setup_without_bluetooth(hass)
    assert _notices(hass) == []  # nothing is known of the lamp yet

    await _reads_what_the_lamp_is(entry, says)

    assert [notice.translation_placeholders for notice in _notices(hass)] == (
        [{"name": "Glowrium-G7_1234", "model": "Glowrium-C064"}] if told else []
    )


async def test_the_notice_goes_once_the_model_has_a_profile(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update that brings the model's profile ends what the notice said.

    Its presets are its own from then on. The notice is taken down at the
    next start, by the integration: left to itself it would stand until
    somebody ignored it.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    assert len(_notices(hass)) == 1
    g8 = GlowriumModel(
        pkey="Glowrium-C064", name="Glowrium G8", lighting_modes={"sun_sync": 1}
    )
    monkeypatch.setitem(models.MODELS, g8.pkey, g8)

    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _notices(hass) == []


async def test_the_notice_goes_when_the_lamp_names_a_model_that_has_a_profile(
    hass: HomeAssistant,
) -> None:
    """What the lamp says now outweighs what the entry remembered.

    The notice at start is raised on a remembered model id. When the lamp is
    then read and names a model with a profile, the presets on offer are that
    model's, and the notice is taken down in the same session.
    """
    entry = _entry(**{CONF_MODEL_ID: "Glowrium-C064"})
    entry.add_to_hass(hass)
    lamp = ScriptedLamp()
    lamp.answers({KEY_ACTIVATED: True, KEY_CIRCADIAN: True})
    lamp.readable(INFO_UUID, G7_INFO)
    found = asyncio.Event()
    lamp.dials_when(found)

    with in_range(lamp):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert len(_notices(hass)) == 1  # as remembered, the lamp not reached yet

        coordinator = entry.runtime_data
        read = asyncio.Event()
        coordinator.async_add_listener(
            lambda: read.set() if coordinator.device_info else None
        )
        found.set()
        await asyncio.wait_for(read.wait(), 2)  # told once the exchange is over

    assert entry.data[CONF_MODEL_ID] == "Glowrium-C051"
    assert _notices(hass) == []


async def test_a_notice_that_was_ignored_stays_ignored_through_a_reload(
    hass: HomeAssistant,
) -> None:
    """Its owner has read it once, and a reload does not show it again.

    Home Assistant keeps "ignored" with the repair, so one that is deleted
    and raised afresh comes back as new. An entry is reloaded for reasons
    that have nothing to do with the lamp's model; the notice is left
    standing through that, and raised over itself by the next start.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    (notice,) = _notices(hass)
    ir.async_ignore_issue(hass, DOMAIN, notice.issue_id, True)

    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    (still,) = _notices(hass)
    assert still.issue_id == notice.issue_id
    assert still.dismissed_version is not None
    assert still.active
    # Not kept across a restart of Home Assistant: an entry that is not
    # loaded then does not show it, and one that is raises it again.
    assert still.is_persistent is False


async def test_a_restart_brings_the_notice_back_for_a_loaded_entry_and_as_it_was_left(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Across a restart Home Assistant keeps only that the notice was ignored.

    The integration stops watching as Home Assistant stops, and the notice is
    left standing through that: what is written down of it then is its id and
    whether it was ignored. Read back, it is shown to nobody - an entry that
    is not loaded after the restart says nothing - until the entry starts and
    raises it over what was kept, ignored as its owner left it.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    (notice,) = _notices(hass)
    ir.async_ignore_issue(hass, DOMAIN, notice.issue_id, True)
    # Home Assistant stops: nothing is unloaded, the integration hears the
    # stop, and then the registry writes down what it holds.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    registry = ir.async_get(hass)
    written = [issue.to_json() for issue in registry.issues.values()]

    # The restart: no entry is loaded yet, and the registry reads that back.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass_storage[ir.STORAGE_KEY] = {
        "version": ir.STORAGE_VERSION_MAJOR,
        "minor_version": ir.STORAGE_VERSION_MINOR,
        "key": ir.STORAGE_KEY,
        "data": {"issues": written},
    }
    await registry.async_load()

    kept = registry.async_get_issue(DOMAIN, notice.issue_id)
    assert kept is not None
    assert not kept.active  # nothing is shown for an entry that is not loaded
    assert kept.dismissed_version is not None

    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    (back,) = _notices(hass)
    assert back.issue_id == notice.issue_id
    assert back.active
    assert back.dismissed_version == kept.dismissed_version


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        (
            "Glowrium-![x](http://evil.example/p.png)<img src=x>",
            "Glowrium- x http evil example p png img src x",
        ),
        ("Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)", "Glowrium-G7_DDEEFF"),
    ],
)
async def test_the_notice_shows_the_lamps_name_as_text(
    hass: HomeAssistant, title: str, shown: str
) -> None:
    """The name comes off the air, and a repair is rendered as Markdown.

    As in the repair for a stack fault: what is shown of it is letters,
    digits, spaces, dashes and underscores, and the address that a lamp
    picked from the list is titled with is left out.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MODEL_ID: "Glowrium-C064"},
    )
    entry.add_to_hass(hass)
    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    (notice,) = _notices(hass)
    assert notice.translation_placeholders is not None
    assert notice.translation_placeholders["name"] == shown


@pytest.mark.parametrize(
    "claimed",
    [
        pytest.param("Glowrium-C051,devid:CST-0001", id="the serial glued to it"),
        pytest.param("Glowrium-C064 [x](http://evil.example)", id="Markdown in it"),
    ],
)
async def test_a_model_id_that_is_not_shaped_like_one_is_not_printed(
    hass: HomeAssistant, claimed: str
) -> None:
    """A lamp that separates its fields differently hands over one long field.

    Its model id is then whatever the parser made of the device-info string,
    the serial number or the address inside it. It names no profile, so the
    notice is raised all the same - in words that do without the model id,
    which goes into a text only when it is one from end to end.
    """
    await _started(hass, **{CONF_MODEL_ID: claimed})

    (notice,) = _notices(hass)
    assert notice.translation_key == "presets_unconfirmed_unnamed_model"
    assert notice.translation_placeholders == {"name": "Glowrium-G7_1234"}


def _open_repairs(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Return every repair of the integration: what it says, and under which id."""
    return sorted(
        (issue.translation_key, issue.issue_id)
        for (domain, _id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    )


async def test_the_notice_and_the_repair_for_a_stack_fault_leave_each_other_alone(
    hass: HomeAssistant,
) -> None:
    """Two things can be said of one lamp, and neither is the other's to end.

    The repair for a stack fault is raised and taken down by what the link
    sees; the notice by what is known of the model. Raising or clearing the
    one leaves the other as it was, its owner's having ignored it included.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    (notice,) = _notices(hass)
    ir.async_ignore_issue(hass, DOMAIN, notice.issue_id, True)
    (ignored,) = _notices(hass)
    link = link_of(entry.runtime_data)

    for _ in range(3):  # three hang-ups in a row left unanswered: a stack fault
        link.note_stuck_hang_up()

    assert [key for key, _id in _open_repairs(hass)] == [
        "bluetooth_stack_stuck",
        "presets_unconfirmed",
    ]
    assert _notices(hass) == [ignored]

    link.note_answer()  # the lamp answers: the stack has let go

    assert [key for key, _id in _open_repairs(hass)] == ["presets_unconfirmed"]
    assert _notices(hass) == [ignored]


async def test_one_lamps_notice_is_not_anothers(hass: HomeAssistant) -> None:
    """Each entry has a notice of its own, and takes only its own away.

    Two lamps of a model without a profile are two notices, named by their
    entries. Removing one lamp leaves what is said of the other - its notice,
    and the repair for a stack fault if it has one.
    """
    first = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Glowrium-G8_5678",
        unique_id="11:22:33:44:55:66",
        data={CONF_ADDRESS: "11:22:33:44:55:66", CONF_MODEL_ID: "Glowrium-C064"},
    )
    other.add_to_hass(hass)
    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_setup(other.entry_id)
        await hass.async_block_till_done()
    for _ in range(3):
        link_of(other.runtime_data).note_stuck_hang_up()
    assert len(_notices(hass)) == 2
    assert len(_open_repairs(hass)) == 3

    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    left = _open_repairs(hass)
    assert [key for key, _id in left] == [
        "bluetooth_stack_stuck",
        "presets_unconfirmed",
    ]
    assert all(other.entry_id in issue_id for _key, issue_id in left)


async def test_a_coordinator_that_was_stopped_raises_no_notice(
    hass: HomeAssistant,
) -> None:
    """What a stopped coordinator learns of the model is not its to say.

    The device info is read last on a link, and Home Assistant can stop
    while the answer to that read is on its way: the coordinator hangs up at
    once, and the exchange is not cancelled with it. A notice raised then
    would have nobody left to answer for it. What was read is kept all the
    same - with the entry, from which the next start raises the notice, and
    in the device registry, where the device page reads it after the restart.
    Withholding the notice is easy to turn into withholding everything.
    """
    entry = await _setup_without_bluetooth(hass)
    lamp = ScriptedLamp()
    lamp.answers({KEY_ACTIVATED: True, KEY_CIRCADIAN: True})
    lamp.readable(INFO_UUID, _G8_INFO)
    answered = asyncio.Event()
    lamp.answers_a_read_when(answered)

    with in_range(lamp):
        connecting = asyncio.create_task(link_of(entry.runtime_data).connect())
        await asyncio.sleep(0.01)
        assert lamp.read == [INFO_UUID]  # the exchange is at its last step
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        answered.set()
        await connecting

    assert entry.data[CONF_MODEL_ID] == "Glowrium-C064"  # read, and kept
    assert _described(_device(hass)) == (
        "Glowrium",  # the name of a model nobody has a profile for
        "Glowrium-C064",
        "2",
        "CST-0009",
    )
    assert _notices(hass) == []
