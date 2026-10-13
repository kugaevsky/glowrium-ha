"""Tests for setting up and tearing down the Glowrium config entry."""

import ast
import asyncio
from collections.abc import Callable, Iterator, Mapping
import contextlib
import datetime
import importlib
import logging
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant.components.logger.helpers import get_integration_loggers
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_MODEL_ID,
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.icon import async_get_icons
from homeassistant.helpers.translation import async_get_translations
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.glowrium import _UNCLOSED, PLATFORMS, cbor, models, protocol
from custom_components.glowrium.const import (
    DOMAIN,
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
    KEY_INDICATOR,
    KEY_LATITUDE,
    KEY_LIGHTING_MODE,
    KEY_LONGITUDE,
    KEY_POWER,
    KEY_RAMP,
    KEY_SCHEDULE,
    KEY_TIMER,
    MODE_CIRCADIAN,
    MODE_SCHEDULE,
    OPERATING_MODES,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import _parse_device_info as _parsed
from custom_components.glowrium.entity import GlowriumEntity
from custom_components.glowrium.keeper import EntryKeeper
from custom_components.glowrium.light import GlowriumLight
from custom_components.glowrium.models import GlowriumModel
from custom_components.glowrium.number import (
    GlowriumRampNumber,
    GlowriumTimerBrightness,
    GlowriumTimerGradual,
)
from custom_components.glowrium.select import GlowriumLightingModeSelect
from custom_components.glowrium.time import GlowriumTimerEnd, GlowriumTimerStart

from .conftest import ROOT, anchors
from .lamp import ScriptedLamp, in_range, link_of
from .shared import (
    ADDRESS,
    G7_INFO,
    _described,
    _device,
    _entry,
    _notices,
    _reads_what_the_lamp_is,
    _setup_without_bluetooth,
    _started,
)


async def test_coordinator_is_reachable_before_it_is_started(
    hass: HomeAssistant,
) -> None:
    """The entry owns the coordinator before async_start registers anything.

    async_start registers the bluetooth callbacks and the reconnect poll. A
    setup cancelled part-way through it - which a lamp on a weak signal makes
    routine - would otherwise leave a coordinator that is running but that
    async_unload_entry cannot reach, one more of them competing for the single
    BLE connection on every cancelled attempt.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    seen: list[object] = []

    async def _record(_self: object) -> None:
        seen.append(entry.runtime_data)

    with (
        # The entry is what ties the background connect to the entry's
        # lifecycle, and it is the keeper that has it: pin that the keeper
        # is built for this one, and before anything is started.
        patch("custom_components.glowrium.EntryKeeper", wraps=EntryKeeper) as kept,
        patch(
            "custom_components.glowrium.GlowriumCoordinator.async_start",
            autospec=True,
            side_effect=_record,
        ),
        patch(
            "custom_components.glowrium.GlowriumCoordinator.async_stop",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert len(seen) == 1
    assert seen[0] is entry.runtime_data  # already published when start ran
    kept.assert_called_once_with(hass, entry)


async def test_unload_stops_the_coordinator(hass: HomeAssistant) -> None:
    """Unloading cancels the watchers and drops the link."""
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.glowrium.GlowriumCoordinator.async_start",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.glowrium.GlowriumCoordinator.async_stop",
            new_callable=AsyncMock,
        ) as stop,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    stop.assert_awaited_once()


@contextlib.contextmanager
def _a_lamp_that_never_connects(
    cancelled: asyncio.Event | None = None,
) -> Iterator[asyncio.Event]:
    """Put a lamp in range whose connect never returns; yield "it is being dialled".

    The integration's own setup hands the coordinator the dial of a config
    entry, so what stands in for the lamp is what that dial is made of: Home
    Assistant's lookup of
    the device, and the library's connect. ``cancelled`` is set if the connect
    is cancelled while it waits.
    """
    dialling = asyncio.Event()

    async def _never_connects(*_args: object, **_kwargs: object) -> None:
        dialling.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cancelled is not None:
                cancelled.set()
            raise

    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=object(),
        ),
        patch(
            "custom_components.glowrium.link.establish_connection",
            side_effect=_never_connects,
        ),
    ):
        yield dialling


async def test_setup_does_not_wait_for_the_connection(hass: HomeAssistant) -> None:
    """Setting up the entry must not wait for the lamp to be reachable.

    Evidence, twice on the same lamp: `Setup of config entry ... cancelled`
    followed by setup_error, because async_setup_entry awaited the initial
    connect and a reload landed inside that window. Waiting is not needed -
    entity availability follows advertisement presence, not the GATT link - so
    setup must return whether or not the lamp answers.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    with _a_lamp_that_never_connects() as dialling:
        async with asyncio.timeout(2):
            assert await hass.config_entries.async_setup(entry.entry_id)
        # And the connect is on its way, not over: this is a setup that
        # returned beside it, not one that had nothing to wait for.
        await asyncio.wait_for(dialling.wait(), 1)

    assert entry.state is ConfigEntryState.LOADED


async def test_the_background_connect_is_cancelled_on_unload(
    hass: HomeAssistant,
) -> None:
    """The connect setup no longer waits for must not outlive the entry.

    Moving it off the setup path is only safe if it dies with the entry;
    otherwise a reload leaves the old connect running against the same lamp,
    which is the coordinator leak in different clothes.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    cancelled = asyncio.Event()

    with _a_lamp_that_never_connects(cancelled) as dialling:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(dialling.wait(), 1)
        assert not cancelled.is_set()  # still running while the entry is loaded
        # By the unload, and so at once: left alone, a connect is ended by
        # its own ceiling many seconds later, and that would read the same.
        async with asyncio.timeout(2):
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    assert cancelled.is_set()


async def test_a_reload_hands_on_what_would_not_close(hass: HomeAssistant) -> None:
    """What could be neither hung up nor closed is held for the lamp.

    Not for one coordinator: a reload makes another, and that one must not
    dial over a client the first could not let go of. A second lamp has
    nothing to do with it.
    """
    entry = await _setup_without_bluetooth(hass)
    first = entry.runtime_data
    stuck = object()
    hass.data[_UNCLOSED][ADDRESS].keep(stuck, None)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup_without_bluetooth(hass, entry)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Glowrium-G7_5678",
        unique_id="11:22:33:44:55:66",
        data={CONF_ADDRESS: "11:22:33:44:55:66"},
    )
    other.add_to_hass(hass)
    await _setup_without_bluetooth(hass, other)

    assert entry.runtime_data is not first
    assert _would_not_close(entry) == 1
    assert _would_not_close(other) == 0


async def test_a_lamp_set_up_again_is_still_the_lamp_whose_client_would_not_close(
    hass: HomeAssistant,
) -> None:
    """By the lamp's address, not by the entry.

    A lamp removed and set up again gets a new entry. It is the same lamp,
    and the client nothing could close is still holding its bus.
    """
    entry = await _setup_without_bluetooth(hass)
    hass.data[_UNCLOSED][ADDRESS].keep(object(), None)

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    again = _entry()
    again.add_to_hass(hass)
    await _setup_without_bluetooth(hass, again)

    assert again.entry_id != entry.entry_id
    assert _would_not_close(again) == 1


def _would_not_close(entry: MockConfigEntry) -> int:
    """Return how many clients the entry's coordinator says would not close."""
    return entry.runtime_data.diagnostics()["link"]["clients_that_would_not_close"]


async def test_every_platform_produces_entities(hass: HomeAssistant) -> None:
    """Each platform in PLATFORMS actually contributes entities.

    Nothing in the suite looked at entities at all: removing Platform.LIGHT
    from the list - so the lamp has no light entity, the one thing the
    integration exists for - left every test passing.
    """
    entry = await _setup_without_bluetooth(hass)

    # Asked of the registry, not of the states: an entity that starts
    # disabled is registered and has no state.
    registered = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    domains = {one.domain for one in registered}
    # And what is not disabled is there to be seen, on every platform.
    shown = {one.domain for one in registered if hass.states.get(one.entity_id)}
    assert shown == domains - {"sensor"}
    assert domains == {
        "binary_sensor",
        "button",
        "light",
        "number",
        "select",
        "sensor",
        "switch",
        "time",
    }


def _registered(hass: HomeAssistant, domain: str, unique: str) -> er.RegistryEntry:
    """Return what the entity registry holds for one of the lamp's entities."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{ADDRESS}_{unique}")
    assert entity_id is not None
    registered = registry.async_get(entity_id)
    assert registered is not None
    return registered


def _coordinate_sensors_from_before(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Register the two coordinate sensors as an older installation has them.

    Enabled, that is: they start disabled only where they are new.
    """
    for which in ("latitude", "longitude"):
        er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, f"{ADDRESS}_{which}", config_entry=entry
        )


async def test_an_action_aimed_at_the_lamps_room_reaches_the_light_and_no_setting(
    hass: HomeAssistant,
) -> None:
    """An action aimed at a room - "turn on everything here" - means the light.

    The indicator, daylight saving time and Sync location carried no category,
    so Home Assistant took them for the lamp's main controls, and an action
    aimed at the room reached them: every switch in the room on meant the
    daylight-saving flag set, and the lamp's own program an hour out, with
    nothing to say why.
    """
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    room = ar.async_get(hass).async_create("Greenhouse")
    dr.async_get(hass).async_update_device(_device(hass).id, area_id=room.id)
    await hass.async_block_till_done()
    coordinator.async_set_dst = AsyncMock()
    coordinator.async_set_indicator = AsyncMock()
    coordinator.async_sync_location = AsyncMock()
    coordinator.async_set_light_state = AsyncMock()

    # The room, and the lamp as a whole: both are ways of not naming an entity.
    for target in ({"area_id": room.id}, {"device_id": _device(hass).id}):
        for domain, service in (
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("button", "press"),
            ("light", "turn_on"),
        ):
            await hass.services.async_call(
                domain, service, {}, target=target, blocking=True
            )

    coordinator.async_set_dst.assert_not_awaited()
    coordinator.async_set_indicator.assert_not_awaited()
    coordinator.async_sync_location.assert_not_awaited()
    assert coordinator.async_set_light_state.await_count == 2  # the light, each time


@pytest.mark.parametrize(
    ("domain", "unique"),
    [("switch", "indicator"), ("switch", "dst"), ("button", "sync_location")],
)
async def test_a_setting_of_the_lamp_is_registered_as_a_setting(
    hass: HomeAssistant, domain: str, unique: str
) -> None:
    """Which is what puts it under Configuration, and out of a room-wide action."""
    await _setup_without_bluetooth(hass)

    assert _registered(hass, domain, unique).entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize(
    ("domain", "unique"),
    [("switch", "indicator"), ("switch", "dst"), ("button", "sync_location")],
)
async def test_a_setting_registered_before_it_was_one_becomes_one(
    hass: HomeAssistant, domain: str, unique: str
) -> None:
    """An installation from before is the one this is for.

    Its registry holds the three with no category. Home Assistant takes the
    category from the entity each time it registers, so the upgrade alone
    moves them - nobody has to remove the lamp and set it up again.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    before = er.async_get(hass).async_get_or_create(
        domain, DOMAIN, f"{ADDRESS}_{unique}", config_entry=entry
    )
    assert before.entity_category is None

    await _setup_without_bluetooth(hass, entry)

    assert _registered(hass, domain, unique).entity_category is EntityCategory.CONFIG


def _in_mode(mode: str) -> dict[int, bool]:
    """Return the two mode flags as the lamp reports them in ``mode``."""
    return {KEY_CIRCADIAN: mode == MODE_CIRCADIAN, KEY_SCHEDULE: mode == MODE_SCHEDULE}


_OF_ONE_MODE = [
    ("select", "lighting_mode", GlowriumLightingModeSelect, MODE_CIRCADIAN),
    ("number", "ramp", GlowriumRampNumber, MODE_CIRCADIAN),
    ("number", "schedule_gradual", GlowriumTimerGradual, MODE_SCHEDULE),
    ("number", "schedule_brightness", GlowriumTimerBrightness, MODE_SCHEDULE),
    ("time", "schedule_start", GlowriumTimerStart, MODE_SCHEDULE),
    ("time", "schedule_end", GlowriumTimerEnd, MODE_SCHEDULE),
]


@pytest.mark.parametrize(
    ("domain", "unique", "cls", "its_mode"),
    _OF_ONE_MODE,
    ids=[unique for _domain, unique, _cls, _mode in _OF_ONE_MODE],
)
async def test_an_entity_of_one_mode_is_unavailable_in_another(
    hass: HomeAssistant,
    domain: str,
    unique: str,
    cls: type[GlowriumEntity],
    its_mode: str,
) -> None:
    """An entity of one operating mode hides in another, and in none while unknown.

    The ramp and the lighting mode are of the circadian curve, the rest of
    the schedule. Each is unavailable while the lamp is in another mode and
    available while the mode is not yet known, so that a lamp not yet read
    keeps its page whole; out of reach it is unavailable whatever the mode.
    Which mode an entity is for is declared on it and judged in one place,
    the base entity's: no platform writes "available, and the mode allows
    it" out for itself, which four did, each in its own words.
    """
    assert cls.available is GlowriumEntity.available  # judged in the one place
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    entity_id = _registered(hass, domain, unique).entity_id

    def _unavailable() -> bool:
        return hass.states.get(entity_id).state == STATE_UNAVAILABLE

    assert not _unavailable()  # the mode not known yet: nothing hides
    for mode in OPERATING_MODES:
        coordinator._ingest(cbor.encode(_in_mode(mode)))
        await hass.async_block_till_done()
        assert _unavailable() is (mode != its_mode), mode
    coordinator._ingest(cbor.encode(_in_mode(its_mode)))
    link_of(coordinator).advertising(False)  # out of reach, in its own mode
    await hass.async_block_till_done()
    assert _unavailable()


async def test_the_coordinate_sensors_start_disabled(hass: HomeAssistant) -> None:
    """Where the lamp is, is not put into states and history unasked.

    After Sync location the lamp holds the home's position, and the two
    sensors show it. Few ever look; whoever wants to enables them.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(cbor.encode({KEY_LATITUDE: 12.5, KEY_LONGITUDE: 65.5}))
    await hass.async_block_till_done()

    for which in ("latitude", "longitude"):
        registered = _registered(hass, "sensor", which)  # so it can be enabled
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(registered.entity_id) is None


async def test_coordinate_sensors_an_installation_has_already_are_left_alone(
    hass: HomeAssistant,
) -> None:
    """Starting disabled is for a lamp set up from now on; nothing is taken away."""
    entry = _entry()
    entry.add_to_hass(hass)
    _coordinate_sensors_from_before(hass, entry)
    await _setup_without_bluetooth(hass, entry)

    entry.runtime_data._ingest(cbor.encode({KEY_LATITUDE: 12.5, KEY_LONGITUDE: 65.5}))
    await hass.async_block_till_done()

    shown = {}
    for which in ("latitude", "longitude"):
        registered = _registered(hass, "sensor", which)
        assert registered.disabled_by is None
        shown[which] = hass.states.get(registered.entity_id).state
    assert shown == {"latitude": "12.5", "longitude": "65.5"}


async def test_the_light_shows_what_the_device_reported(hass: HomeAssistant) -> None:
    """A device report reaches the light entity, brightness and all.

    This is the whole path - notification, coordinator state, listener,
    entity - and no test had ever walked it end to end.
    """
    entry = await _setup_without_bluetooth(hass)
    light = next(
        state.entity_id
        for state in hass.states.async_all("light")
        if "glowrium" in state.entity_id
    )
    assert hass.states.get(light).state == "unknown"  # nothing read yet

    entry.runtime_data._ingest(cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: 50}))
    await hass.async_block_till_done()

    reported = hass.states.get(light)
    assert reported.state == "on"
    assert reported.attributes["brightness"] == 128  # 50 % of 255, rounded


@pytest.mark.parametrize(
    ("key", "unique_id", "reported", "shown"),
    [
        (KEY_DST, "dst", bytes.fromhex("0100000e10"), "on"),
        (KEY_DST, "dst", bytes.fromhex("0000000e10"), "off"),
        (KEY_INDICATOR, "indicator", True, "on"),
        (KEY_INDICATOR, "indicator", False, "off"),
    ],
)
async def test_a_switch_shows_what_the_lamp_reports(
    hass: HomeAssistant, key: int, unique_id: str, reported: object, shown: str
) -> None:
    """Each switch follows the lamp: a flag as it is, the DST slot by its first byte.

    The commands of both were tested, and what they show was not - the DST
    switch's reading was run by a test about something else, and stopped
    being run when that test was rewritten.
    """
    entry = await _setup_without_bluetooth(hass)
    switch = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{ADDRESS}_{unique_id}"
    )
    assert switch is not None
    assert hass.states.get(switch).state == "unknown"  # nothing read yet

    entry.runtime_data._ingest(cbor.encode({key: reported}))
    await hass.async_block_till_done()

    assert hass.states.get(switch).state == shown


async def test_turning_the_light_on_reaches_the_lamp(hass: HomeAssistant) -> None:
    """The service call is carried through to a write, not swallowed."""
    entry = await _setup_without_bluetooth(hass)
    light = next(
        state.entity_id
        for state in hass.states.async_all("light")
        if "glowrium" in state.entity_id
    )
    coordinator = entry.runtime_data
    coordinator.async_set_light_state = AsyncMock()

    await hass.services.async_call(
        "light", "turn_on", {"entity_id": light, "brightness": 255}, blocking=True
    )

    coordinator.async_set_light_state.assert_awaited_once()
    assert coordinator.async_set_light_state.await_args.args[0] is True


async def test_a_setup_that_fails_later_still_stops_the_coordinator(
    hass: HomeAssistant,
) -> None:
    """A coordinator that was started must be stopped even if setup then fails.

    Home Assistant does not call async_unload_entry for an entry that never
    reached LOADED (config_entries.py: it returns as soon as the state is not
    LOADED), so an entry that fails after async_start would otherwise keep its
    bluetooth callbacks and its 30 s poll registered forever - and every retry
    adds another one, all competing for the lamp's single connection.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.glowrium.coordinator.GlowriumCoordinator.async_stop",
            new_callable=AsyncMock,
        ) as stop,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform blew up"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    stop.assert_awaited_once()


async def test_settings_survive_a_restart_but_the_light_does_not(
    hass: HomeAssistant,
) -> None:
    """Settings show their last value again; the light still admits it is unknown.

    A device page where every control reads `unknown` is unusable, and the
    lamp's settings do not change while Home Assistant is down - so showing the
    last value read is a better answer than none. The light is deliberately not
    restored: a lamp reported `on` while it is physically off is the confident
    lie this integration used to tell, and automations reasoned from it.
    """
    mock_restore_cache(
        hass,
        (
            State("switch.glowrium_g7_1234_indicator_light", "on"),
            State("switch.glowrium_g7_1234_daylight_saving_time", "on"),
            State("number.glowrium_g7_1234_ramp_time", "45"),
            State("select.glowrium_g7_1234_lighting_mode", "sunrise_sync"),
            State("light.glowrium_g7_1234", "on"),
        ),
    )
    await _setup_without_bluetooth(hass)

    assert hass.states.get("switch.glowrium_g7_1234_indicator_light").state == "on"
    assert hass.states.get("switch.glowrium_g7_1234_daylight_saving_time").state == "on"
    assert hass.states.get("number.glowrium_g7_1234_ramp_time").state == "45.0"
    assert (
        hass.states.get("select.glowrium_g7_1234_lighting_mode").state == "sunrise_sync"
    )
    assert hass.states.get("light.glowrium_g7_1234").state == "unknown"


async def test_a_report_from_the_lamp_overrides_what_was_restored(
    hass: HomeAssistant,
) -> None:
    """The remembered value is a stand-in, not a preference."""
    mock_restore_cache(hass, (State("switch.glowrium_g7_1234_indicator_light", "on"),))
    entry = await _setup_without_bluetooth(hass)
    assert hass.states.get("switch.glowrium_g7_1234_indicator_light").state == "on"

    entry.runtime_data._ingest(cbor.encode({KEY_INDICATOR: False}))
    await hass.async_block_till_done()

    assert hass.states.get("switch.glowrium_g7_1234_indicator_light").state == "off"


async def test_stopping_home_assistant_hangs_up_the_lamp(hass: HomeAssistant) -> None:
    """Home Assistant stopping is not an unload, and has to be listened for.

    On shutdown Home Assistant does not unload its config entries - it only
    cancels a pending setup retry - so the callback that stops the coordinator
    on unload never runs. Nothing then hangs the lamp's link up but the Python
    process on its way out, at the very end of a clean stop. A stop that is cut
    short - a container is given ten seconds - never gets that far, and the
    link stays at the controller with no process behind it, on a lamp that has
    one link to give.
    """
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    lamp = ScriptedLamp()
    with in_range(lamp):
        await coordinator.async_set_power(True)  # a link is held

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert lamp.links[0].hung_up == 1
    assert link_of(coordinator).diagnostics()["connected"] is False


async def test_the_stop_listener_goes_with_the_entry(hass: HomeAssistant) -> None:
    """An entry that has been unloaded no longer answers Home Assistant stopping.

    The listener is registered on the bus, not on the entry, so it has to be
    taken off by hand. Left there, every reload would add one more, each
    holding on to a coordinator that was stopped long ago.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.glowrium.coordinator.GlowriumCoordinator.async_shutdown",
            autospec=True,
        ) as shutdown,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    shutdown.assert_not_called()


async def test_an_entry_unloaded_after_the_stop_is_no_error_to_home_assistant(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Dropping the stop listener once the stop has come is nothing to log.

    A listener registered to be called once takes itself off the bus when its
    event comes. Taken off a second time, with the entry, it has Home
    Assistant log an error about a listener it does not know. Home Assistant
    unloads nothing on its way down, so that is an entry reloaded while it
    stops, or one whose setup fails after the stop has come - a setup that
    fails runs the same callbacks - and every test here that fires the stop,
    when its entry is unloaded behind it.
    """
    entry = await _setup_without_bluetooth(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_restart_does_not_wipe_what_the_registry_holds(
    hass: HomeAssistant,
) -> None:
    """Not knowing yet is not the same as knowing there is nothing.

    A field left out of the device description is left alone in the registry;
    a field given as None replaces what was there. Every start described the
    lamp before reading it, with None for whatever it had not read, and so
    took back what an earlier session had learned.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)},
        manufacturer="INLEDCO",
        model="Glowrium G7",
        model_id="Glowrium-C051",
        sw_version="4",
        serial_number="CST-0001",
    )

    await _setup_without_bluetooth(hass, entry)

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )


async def test_a_remembered_model_does_not_stand_in_for_reading_it(
    hass: HomeAssistant,
) -> None:
    """Firmware can change between starts; what was remembered is not re-read."""
    entry = _entry(**{CONF_MODEL_ID: "Glowrium-C051"})
    entry.add_to_hass(hass)
    await _setup_without_bluetooth(hass, entry)

    lamp = await _reads_what_the_lamp_is(
        entry, G7_INFO.replace(b"version:4", b"version:5")
    )

    assert lamp.read == [INFO_UUID]
    assert _device(hass).sw_version == "5"


async def test_a_read_that_fails_leaves_the_device_page_alone(
    hass: HomeAssistant,
) -> None:
    """A link lost before the read says nothing about the lamp."""
    entry = _entry(**{CONF_MODEL_ID: "Glowrium-C051"})
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)},
        model="Glowrium G7",
        model_id="Glowrium-C051",
        sw_version="4",
        serial_number="CST-0001",
    )
    await _setup_without_bluetooth(hass, entry)

    lamp = await _reads_what_the_lamp_is(entry, None)

    assert lamp.read == [INFO_UUID]  # asked for, and not given

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )
    assert entry.data[CONF_MODEL_ID] == "Glowrium-C051"


async def test_the_presets_offered_are_the_ones_of_the_lamp_that_answered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model's own presets take effect, which they could not before.

    The preset list was fixed when the entity was built - before any lamp had
    said which model it was - so it was always the reference list, and a
    profile added to models.py for another model changed nothing.
    """
    other = GlowriumModel(
        pkey="Glowrium-TEST",
        name="Glowrium Test",
        lighting_modes={"dawn": 3, "dusk": 4},
    )
    monkeypatch.setitem(models.MODELS, other.pkey, other)
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    select = "select.glowrium_g7_1234_lighting_mode"
    assert "sun_sync" in hass.states.get(select).attributes["options"]

    await _reads_what_the_lamp_is(entry, b"pkey:Glowrium-TEST;version:1;;")
    await hass.async_block_till_done()  # the connect told the entities itself

    assert hass.states.get(select).attributes["options"] == ["dawn", "dusk"]
    coordinator.async_set_lighting_mode = AsyncMock()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "dusk"},
        blocking=True,
    )
    coordinator.async_set_lighting_mode.assert_awaited_once_with(4)


async def test_the_light_takes_its_icon_from_the_icon_file(hass: HomeAssistant) -> None:
    """The light's icon is named by a key and kept in icons.json, like the rest.

    It was set in code from the model's profile - at a moment when the model
    was never known, so it was never set at all: the real host showed none.
    Kept with the other icons it does not depend on what has been read, and
    it stays out of the entity's state.
    """
    await _setup_without_bluetooth(hass)
    light = "light.glowrium_g7_1234"

    registered = er.async_get(hass).async_get(light)
    assert registered is not None
    assert registered.translation_key == "lamp"
    icons = await async_get_icons(hass, "entity", integrations=[DOMAIN])
    assert icons[DOMAIN]["light"]["lamp"]["default"] == "mdi:lightbulb-group"

    state = hass.states.get(light)
    assert "icon" not in state.attributes
    assert state.name == "Glowrium-G7_1234"  # still named after the device


async def test_a_lighting_mode_is_a_key_and_its_name_is_a_translation(
    hass: HomeAssistant,
) -> None:
    """What an automation stores is a key; what a person reads is its name.

    The options used to be the presets' English names, so the name was also
    the value: it could not be translated, and it could not be corrected
    without breaking every automation that had it written down.
    """
    await _setup_without_bluetooth(hass)

    options = hass.states.get("select.glowrium_g7_1234_lighting_mode").attributes[
        "options"
    ]

    assert options == [
        "sun_sync",
        "before_sunrise",
        "sunrise_sync",
        "sunset_sync",
        "after_sunset",
        "two_phase",
        "balance",
        "enhanced_two_phase",
    ]


async def test_a_preset_remembered_by_its_old_name_is_still_recognised(
    hass: HomeAssistant,
) -> None:
    """The value kept from before the change is the preset's old name.

    It is the only record of the mode on a lamp that never reports it, so it
    is read as the preset it names rather than dropped as an unknown option.
    """
    mock_restore_cache(
        hass, (State("select.glowrium_g7_1234_lighting_mode", "Enhanced Two-Phase"),)
    )
    await _setup_without_bluetooth(hass)

    state = hass.states.get("select.glowrium_g7_1234_lighting_mode")
    assert state.state == "enhanced_two_phase"


@pytest.mark.parametrize("platform", PLATFORMS, ids=str)
def test_every_platform_says_how_many_calls_it_takes_at_once(platform: str) -> None:
    """Each platform states its limit on parallel calls instead of inheriting one.

    None is needed: the coordinator puts every command through one lock, and
    nothing here is polled. Saying so is what keeps Home Assistant's default
    for the platform from deciding it.
    """
    module = importlib.import_module(f"custom_components.glowrium.{platform}")

    assert module.PARALLEL_UPDATES == 0


async def test_debug_logging_takes_the_bluetooth_libraries_with_it(
    hass: HomeAssistant,
) -> None:
    """Enabling debug logging has to switch on what a link problem is read from.

    The button on the integration's page raises the level of the loggers the
    manifest names. It named none, so it gave the integration's own lines and
    nothing of what happened underneath them - the connect attempts, the
    reads, the disconnect - which is where every link problem here was
    actually found, and had to be switched on by hand.
    """
    assert await get_integration_loggers(hass, DOMAIN) >= {
        "custom_components.glowrium",
        "bleak",
        "bleak_retry_connector",
    }


def _call(domain: str, service: str, entity: str, **data: object) -> tuple:
    """Describe a service call on one of the lamp's entities."""
    return domain, service, {"entity_id": f"{domain}.glowrium_g7_1234{entity}", **data}


@pytest.mark.parametrize(
    ("call", "method", "args"),
    [
        (_call("light", "turn_off", ""), "async_set_light_state", (False,)),
        (
            _call("switch", "turn_on", "_indicator_light"),
            "async_set_indicator",
            (True,),
        ),
        (
            _call("switch", "turn_off", "_indicator_light"),
            "async_set_indicator",
            (False,),
        ),
        (
            _call("switch", "turn_on", "_daylight_saving_time"),
            "async_set_dst",
            (True,),
        ),
        (
            _call("switch", "turn_off", "_daylight_saving_time"),
            "async_set_dst",
            (False,),
        ),
        (_call("button", "press", "_sync_location"), "async_sync_location", ()),
        (
            _call("select", "select_option", "_operating_mode", option="schedule"),
            "async_set_operating_mode",
            ("schedule",),
        ),
        (
            _call("select", "select_option", "_lighting_mode", option="sunset_sync"),
            "async_set_lighting_mode",
            (9,),
        ),
        (
            _call("number", "set_value", "_ramp_time", value=45),
            "async_set_ramp",
            (45,),
        ),
        (
            _call("number", "set_value", "_schedule_gradual", value=15),
            "async_set_timer_gradual",
            (15,),
        ),
        (
            _call("number", "set_value", "_schedule_brightness", value=80),
            "async_set_timer_brightness",
            (80,),
        ),
        (
            _call("time", "set_value", "_schedule_start", time="06:30:00"),
            "async_set_timer_start",
            (6, 30),
        ),
        (
            _call("time", "set_value", "_schedule_end", time="21:05:00"),
            "async_set_timer_end",
            (21, 5),
        ),
    ],
)
async def test_every_control_reaches_the_command_it_stands_for(
    hass: HomeAssistant,
    call: tuple[str, str, dict[str, object]],
    method: str,
    args: tuple[object, ...],
) -> None:
    """Each control on the device page ends in the right command, rightly put.

    The light's switching on was walked from the service call to the write.
    The other twelve controls were not: an entity wired to the wrong command,
    or handing a schedule's hour over as its minute, passed everything.
    """
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    setattr(coordinator, method, AsyncMock())
    domain, service, data = call

    await hass.services.async_call(domain, service, data, blocking=True)

    getattr(coordinator, method).assert_awaited_once_with(*args)


async def test_syncing_a_home_that_was_never_set_writes_nothing_and_says_why(
    hass: HomeAssistant,
) -> None:
    """Zero and zero is what Home Assistant holds when it was given no home.

    It holds both coordinates as numbers, always, so the check for a missing
    one never fired: the press went through, the lamp was told it stands where
    the equator meets the prime meridian, and its Circadian program followed
    the sun of that place - with nothing to say so but two zeros on the
    diagnostic sensors.
    """
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    lamp = ScriptedLamp()
    hass.config.latitude = 0
    hass.config.longitude = 0
    domain, service, data = _call("button", "press", "_sync_location")

    with in_range(lamp), pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(domain, service, data, blocking=True)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "home_location_not_set"
    # The user's to put right, not a fault of the lamp or the link: Home
    # Assistant shows such a refusal and keeps it out of the log.
    assert isinstance(err.value, ServiceValidationError)
    # What the user is shown is the message, with the lamp named in it - not
    # the key, which is what Home Assistant falls back on for one it lacks.
    assert "Glowrium-G7_1234" in str(err.value)
    assert lamp.dials == 0  # nothing was dialled, let alone written
    assert lamp.written == []
    assert KEY_LATITUDE not in coordinator.state
    assert KEY_LONGITUDE not in coordinator.state


async def test_a_value_remembered_in_a_shape_it_no_longer_has_is_let_go(
    hass: HomeAssistant,
) -> None:
    """What was kept from the last run is a stand-in, and may be unreadable.

    A number that is no number and a time that is no time are shown as
    unknown, the same as having remembered nothing - not as an error on
    every state write.
    """
    mock_restore_cache(
        hass,
        (
            State("number.glowrium_g7_1234_ramp_time", "soon"),
            State("time.glowrium_g7_1234_schedule_start", "morning"),
            State("time.glowrium_g7_1234_schedule_end", "18:30:00"),
        ),
    )
    await _setup_without_bluetooth(hass)

    assert hass.states.get("number.glowrium_g7_1234_ramp_time").state == "unknown"
    assert hass.states.get("time.glowrium_g7_1234_schedule_start").state == "unknown"
    assert hass.states.get("time.glowrium_g7_1234_schedule_end").state == "18:30:00"


# --- The two numbers that hold a length of time ---

_RAMP = "number.glowrium_g7_1234_ramp_time"
_FADE = "number.glowrium_g7_1234_schedule_gradual"

# What the lamp reports of each when it is 45 minutes. It counts both in
# seconds, two bytes, and 2700 of them are 0a8c: a ramp, and the last two
# bytes of a schedule's slot - whose brightness, the byte before, is 70 %.
_A_RAMP_OF_45: dict[int, Any] = {KEY_LIGHTING_MODE: 5, KEY_RAMP: bytes.fromhex("0a8c")}
_A_FADE_OF_45: dict[int, Any] = {KEY_TIMER: bytes.fromhex("01000000061e1200460a8c")}

_each_length_of_time = pytest.mark.parametrize(
    ("number", "report"),
    [
        pytest.param(_RAMP, _A_RAMP_OF_45, id="the ramp"),
        pytest.param(_FADE, _A_FADE_OF_45, id="the schedule's fade"),
    ],
)


def _choose_unit(hass: HomeAssistant, entity_id: str, unit: str) -> None:
    """Have one of the lamp's numbers shown in ``unit``, as its settings offer."""
    er.async_get(hass).async_update_entity_options(
        entity_id, "number", {"unit_of_measurement": unit}
    )


def _state_and_unit(hass: HomeAssistant, entity_id: str) -> tuple[str, str]:
    """Return what an entity shows, and the unit it shows it in."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state, state.attributes["unit_of_measurement"]


@_each_length_of_time
async def test_a_number_shown_in_another_unit_is_remembered_in_its_own(
    hass: HomeAssistant, number: str, report: dict[int, Any]
) -> None:
    """What a number remembers is its minutes, whatever unit they were shown in.

    A length of time can be shown in a unit chosen for the entity, and the
    state Home Assistant keeps for it is then in that unit: 45 minutes shown
    in seconds are kept as 2700. Read back as the number's own minutes, that
    is a ramp of 2700 minutes until the lamp speaks. Beside the state a
    number keeps its value in its own unit, and that is what it remembers.

    An entry set up again starts with nothing read, as Home Assistant does
    after a restart, and what stood in the first time is kept for the second.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(cbor.encode(report))
    _choose_unit(hass, number, UnitOfTime.SECONDS)
    await hass.async_block_till_done()
    assert _state_and_unit(hass, number) == ("2700.0", "s")

    for _ in range(2):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await _setup_without_bluetooth(hass, entry)

        assert _state_and_unit(hass, number) == ("2700.0", "s")


@pytest.mark.parametrize(
    ("number", "report", "figure"),
    [
        pytest.param(_RAMP, _A_RAMP_OF_45, "45", id="the ramp"),
        pytest.param(_FADE, _A_FADE_OF_45, "45", id="the schedule's fade"),
        pytest.param(
            "number.glowrium_g7_1234_schedule_brightness",
            _A_FADE_OF_45,
            "70",
            id="the schedule's brightness",
        ),
    ],
)
async def test_a_number_comes_back_as_the_figure_it_showed(
    hass: HomeAssistant, number: str, report: dict[int, Any], figure: str
) -> None:
    """A lamp that reports what a number remembered changes nothing.

    Read back from its state, a number that had shown 45 came back as 45.0
    and went to 45 again when the lamp reported: a change of state after a
    restart, in a setting nobody had touched, for an automation to take for
    one. The value kept in the number's own unit is the one it had.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(cbor.encode(report))
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup_without_bluetooth(hass, entry)
    shown = [hass.states.get(number).state]

    @callback
    def _changed(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        assert new is not None
        shown.append(new.state)

    async_track_state_change_event(hass, number, _changed)
    entry.runtime_data._ingest(cbor.encode(report))
    await hass.async_block_till_done()

    assert shown == [figure]


@_each_length_of_time
async def test_a_number_that_showed_no_value_remembers_none(
    hass: HomeAssistant, number: str, report: dict[int, Any]
) -> None:
    """A number that showed nothing before a restart shows nothing after it.

    When a remembered value stands in has not changed, only where a number
    reads it from. A number keeps its value beside its state whether or not
    the state shows it, and in Manual neither of the two shows anything: each
    is for a mode the lamp is not in. Such a setting has never come back
    after a restart, and does not now for having a value on record.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(
        cbor.encode({KEY_CIRCADIAN: False, KEY_SCHEDULE: False, **report})
    )
    await hass.async_block_till_done()
    assert hass.states.get(number).state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup_without_bluetooth(hass, entry)

    assert hass.states.get(number).state == "unknown"


@pytest.mark.parametrize("stored", ["soon", None, True], ids=repr)
async def test_a_value_stored_beside_the_state_that_is_no_number_is_let_go(
    hass: HomeAssistant, stored: object
) -> None:
    """What a number stored in its own unit is a stand-in too, and may be unreadable.

    It comes out of a file. Something there that is no number is shown as
    unknown, the same as having remembered nothing - and the state beside it
    is not read in its place, being in whatever unit the number was shown in.
    """
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(_RAMP, "45"),
                {
                    "native_max_value": 90,
                    "native_min_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "min",
                    "native_value": stored,
                },
            ),
        ),
    )
    await _setup_without_bluetooth(hass)

    assert hass.states.get(_RAMP).state == "unknown"


# What the lamp has reported before either number is set: a ramp of 30 minutes
# and a schedule that fades over 10. A command is refused until what it is
# written together with has been read.
_WHAT_A_COMMAND_BUILDS_ON: dict[int, Any] = {
    KEY_LIGHTING_MODE: 5,
    KEY_RAMP: bytes.fromhex("0708"),
    KEY_TIMER: bytes.fromhex("01000000061e1200460258"),
}


async def _set_to(hass: HomeAssistant, number: str, value: float) -> dict[int, str]:
    """Set one of the two numbers through its service; return its bytes as written.

    In hex, under the id they went out under: the ramp's two bytes of
    seconds, or the whole slot of the schedule, whose last two are its fade's.
    """
    lamp = ScriptedLamp()
    with in_range(lamp):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number, "value": value}, blocking=True
        )
    [(uuid, payload)] = lamp.written
    assert uuid == WRITE_UUID
    return {
        key: sent.hex()
        for key, sent in cbor.decode(payload).items()
        if key in (KEY_RAMP, KEY_TIMER)
    }


@pytest.mark.parametrize(
    ("number", "name", "icon", "written"),
    [
        (_RAMP, "Ramp time", "mdi:tunnel", {KEY_RAMP: "0a8c"}),
        (
            _FADE,
            "Schedule gradual",
            "mdi:transition",
            {KEY_TIMER: "01000000061e1200460a8c"},
        ),
    ],
    ids=["the ramp", "the schedule's fade"],
)
async def test_a_number_of_minutes_is_a_duration_and_is_otherwise_what_it_was(
    hass: HomeAssistant, number: str, name: str, icon: str, written: dict[int, str]
) -> None:
    """The ramp and the schedule's fade say what their minutes are: a duration.

    Home Assistant has a device class for a length of time, and a name
    ("Duration"), an icon and a converter between units of time come with it.
    The first two are not to be seen, and the third does nothing to a number
    left in its own unit. Each keeps the name its translation gives it, the
    icon its key has in the icon file, its minutes, its limits and its step -
    and what it is set to still reaches the lamp as that many minutes, which
    the lamp counts in seconds: 45 of them are 2700, two bytes, 0a8c.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(cbor.encode(_WHAT_A_COMMAND_BUILDS_ON))

    sent = await _set_to(hass, number, 45)

    assert sent == written
    state = hass.states.get(number)
    assert state.state == "45"
    assert state.name == f"Glowrium-G7_1234 {name}"
    registered = er.async_get(hass).async_get(number)
    assert registered is not None
    icons = await async_get_icons(hass, "entity", integrations=[DOMAIN])
    assert icons[DOMAIN]["number"][registered.translation_key]["default"] == icon
    assert "icon" not in state.attributes
    described = {
        "device_class": "duration",
        "unit_of_measurement": "min",
        "min": 0,
        "max": 90,
        "step": 1,
        "mode": "box",
    }
    assert {said: state.attributes.get(said) for said in described} == described


@pytest.mark.parametrize(
    ("number", "unit", "value", "written"),
    [
        pytest.param(_RAMP, "min", 45.7, {KEY_RAMP: "0ac8"}, id="45.7 minutes"),
        pytest.param(
            _RAMP, "s", 2720, {KEY_RAMP: "0a8c"}, id="45 minutes and 20 seconds"
        ),
        pytest.param(
            _RAMP, "s", 2740, {KEY_RAMP: "0ac8"}, id="45 minutes and 40 seconds"
        ),
        pytest.param(_RAMP, "s", 2670, {KEY_RAMP: "0a8c"}, id="44 minutes and a half"),
        pytest.param(
            _RAMP, "h", 65 / 60, {KEY_RAMP: "0f3c"}, id="65 minutes, set as hours"
        ),
        pytest.param(
            _RAMP, "h", 1.025, {KEY_RAMP: "0e88"}, id="61 and a half, set as hours"
        ),
        pytest.param(
            _FADE,
            "s",
            2740,
            {KEY_TIMER: "01000000061e1200460ac8"},
            id="a fade of 45 minutes and 40 seconds",
        ),
    ],
)
async def test_a_part_of_a_minute_is_written_as_the_nearest_minute(
    hass: HomeAssistant, number: str, unit: str, value: float, written: dict[int, str]
) -> None:
    """Whatever a number is set to goes to the lamp as the nearest whole minute.

    An action can set 45.7 minutes, and in seconds a number can be set to any
    second: what the entity is handed is then minutes with a part, which was
    cut off without anybody having decided it should be. It is rounded now -
    2720 seconds are 45 minutes (0a8c), 2740 are 46 (0ac8) - and half a
    minute goes up. Out of another unit a value arrives a hair off as well:
    65 minutes (0f3c) set as hours are a hair under 65 and were cut to 64,
    and 61 and a half are a hair under the half that makes them 62 (0e88).
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(cbor.encode(_WHAT_A_COMMAND_BUILDS_ON))
    _choose_unit(hass, number, unit)
    await hass.async_block_till_done()

    sent = await _set_to(hass, number, value)

    assert sent == written


@pytest.mark.parametrize(
    ("ramp", "shown"),
    [("0708", "0.0"), ("0a8c", "1.0"), ("1518", "2.0")],
    ids=["30 minutes", "45 minutes", "90 minutes"],
)
async def test_minutes_shown_in_hours_are_shown_as_whole_hours(
    hass: HomeAssistant, ramp: str, shown: str
) -> None:
    """Minutes are the unit the two are meant to be read in, and this is why.

    The number platform shows a value in a chosen unit to as many decimals as
    the value itself is written with, and has no precision to be given. The
    two report whole minutes, so in hours they show whole hours: half an hour
    as none, and the limit of an hour and a half as two. The README says so;
    a Home Assistant in which this fails has changed what the README says.
    """
    entry = await _setup_without_bluetooth(hass)
    entry.runtime_data._ingest(
        cbor.encode({**_A_RAMP_OF_45, KEY_RAMP: bytes.fromhex(ramp)})
    )
    _choose_unit(hass, _RAMP, UnitOfTime.HOURS)
    await hass.async_block_till_done()

    assert _state_and_unit(hass, _RAMP) == (shown, "h")
    assert hass.states.get(_RAMP).attributes["max"] == 2


async def test_the_brightness_of_the_schedule_is_given_no_device_class(
    hass: HomeAssistant,
) -> None:
    """A percentage of the lamp's own output is none of the classes on offer.

    The third number sits beside the two durations and is built the same way,
    so the class of its neighbours is one line away from being its own.
    """
    await _setup_without_bluetooth(hass)

    state = hass.states.get("number.glowrium_g7_1234_schedule_brightness")

    assert "device_class" not in state.attributes
    assert state.attributes["unit_of_measurement"] == "%"


async def test_a_brightness_that_is_not_a_number_is_not_a_brightness(
    hass: HomeAssistant,
) -> None:
    """The light says it is on and leaves out a level it cannot read."""
    entry = await _setup_without_bluetooth(hass)

    entry.runtime_data._ingest(cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: b"\x46"}))
    await hass.async_block_till_done()

    light = hass.states.get("light.glowrium_g7_1234")
    assert light.state == "on"
    assert light.attributes["brightness"] is None


@pytest.mark.parametrize(
    "reported", [float("inf"), float("-inf"), float("nan"), 150, -1, 2**40, 100.5, True]
)
async def test_a_brightness_that_is_no_percentage_is_not_a_brightness(
    hass: HomeAssistant, reported: float
) -> None:
    """A level is a number from 0 to 100, or it is not shown.

    ``inf`` and ``nan`` are what a lamp - or whatever answers at its address -
    can put in a float, and rounding either raises; 150 would be shown as a
    brightness Home Assistant has no such thing as.
    """
    entry = await _setup_without_bluetooth(hass)

    entry.runtime_data._ingest(cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: reported}))
    await hass.async_block_till_done()

    light = hass.states.get("light.glowrium_g7_1234")
    assert light.state == "on"
    assert light.attributes["brightness"] is None


@pytest.mark.parametrize(("reported", "shown"), [(70.0, 178), (12.5, 32), (0, 0)])
async def test_a_level_that_is_not_a_whole_number_is_still_a_level(
    hass: HomeAssistant, reported: float, shown: int
) -> None:
    """A lamp that reports 70.0 has a brightness, as it had before.

    The G7 reports whole numbers. Nothing says every model does, and a light
    that lost its level over the kind of number would be a poor exchange for
    one that no longer raises.
    """
    entry = await _setup_without_bluetooth(hass)

    entry.runtime_data._ingest(cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: reported}))
    await hass.async_block_till_done()

    assert hass.states.get("light.glowrium_g7_1234").attributes["brightness"] == shown


# A lamp as it reports when all is well; its three modes are below.
_WELL: dict[int, Any] = {
    KEY_POWER: True,
    KEY_BRIGHTNESS: 70,
    KEY_LATITUDE: 12.5,
    KEY_LONGITUDE: 65.5,
    KEY_TIMER: bytes.fromhex("01000000061e1200460258"),
    KEY_ACTIVATED: True,
    KEY_INDICATOR: True,
    KEY_LIGHTING_MODE: 5,
    KEY_RAMP: bytes.fromhex("0708"),
    KEY_DST: bytes.fromhex("0000000e10"),
}
_MODES: tuple[dict[int, Any], ...] = (
    {KEY_CIRCADIAN: False, KEY_SCHEDULE: False},
    {KEY_CIRCADIAN: True, KEY_SCHEDULE: False},
    {KEY_CIRCADIAN: False, KEY_SCHEDULE: True},
)
# Everything the decoder can hand over under an id: the shape of a frame is
# checked, what sits under an id is not.
_ODD: tuple[Any, ...] = (
    float("inf"),
    float("-inf"),
    float("nan"),
    1.5,
    -1,
    101,
    255,
    2**64 - 1,
    -(2**63),
    True,
    False,
    "",
    "on",
    b"",
    b"\x01",
    b"\xff" * 2,
    b"\xff" * 5,
    b"\xff" * 7,
    b"\xff" * 11,
    b"\x00" * 11,
    b"\xff" * 40,
    [],
    [1, 2],
    {},
    {1: 2},
)
# And null, alone and held by an array or a map. As the bytes of each: the
# encoder these tests write frames with has no null to write.
_NULLS: tuple[bytes, ...] = (
    bytes.fromhex("f6"),
    bytes.fromhex("81f6"),
    bytes.fromhex("8181f6"),
    bytes.fromhex("a101f6"),
)
# What each entity shows a reading of. The lighting mode is not among them:
# an index that can be used is still unknown where the model has no such preset.
_SHOWS: dict[str, Callable[[Mapping[int, Any]], Any]] = {
    "light.glowrium_g7_1234": protocol.power,
    "binary_sensor.glowrium_g7_1234_activated": protocol.activated,
    "switch.glowrium_g7_1234_indicator_light": protocol.indicator,
    "switch.glowrium_g7_1234_daylight_saving_time": protocol.dst_enabled,
    "select.glowrium_g7_1234_operating_mode": protocol.operating_mode,
    "number.glowrium_g7_1234_ramp_time": protocol.ramp_minutes,
    "number.glowrium_g7_1234_schedule_brightness": protocol.schedule_brightness,
    "number.glowrium_g7_1234_schedule_gradual": protocol.schedule_gradual_minutes,
    "time.glowrium_g7_1234_schedule_start": protocol.schedule_start,
    "time.glowrium_g7_1234_schedule_end": protocol.schedule_end,
    "sensor.glowrium_aa_bb_cc_dd_ee_ff_latitude": protocol.latitude,
    "sensor.glowrium_aa_bb_cc_dd_ee_ff_longitude": protocol.longitude,
}


def _says(shown: str, value: Any) -> bool:
    """Tell whether the state ``shown`` is what ``value`` means."""
    if isinstance(value, bool):
        return shown == ("on" if value else "off")
    if isinstance(value, (int, float)):
        # A coordinate is shown to five places; nothing else is rounded.
        return shown not in ("unknown", "unavailable") and (
            float(shown) == pytest.approx(value, abs=1e-5)
        )
    if isinstance(value, datetime.time):
        return shown == value.isoformat()
    return shown == value


def _shown_otherwise(hass: HomeAssistant, reported: Mapping[int, Any]) -> list[str]:
    """Name each entity that does not show what was read of its property.

    Unknown for what cannot be used; for a value, the state that value
    means. An entity of another mode than the lamp is in is unavailable, and
    says nothing either way.
    """
    wrong = []
    for entity, read in _SHOWS.items():
        shown = hass.states.get(entity).state
        reading = read(reported)
        if shown == "unavailable":
            continue
        if reading is None or reading is protocol.UNUSABLE:
            as_read = shown == "unknown"
        else:
            as_read = _says(shown, reading)
        if not as_read:
            wrong.append(f"{entity} shows {shown!r} for {reading!r}")
    select = hass.states.get("select.glowrium_g7_1234_lighting_mode").state
    if protocol.lighting_mode(reported) is protocol.UNUSABLE and select not in (
        "unknown",
        "unavailable",
    ):
        wrong.append(f"the lighting mode shows {select!r} for no index")
    return wrong


async def test_no_value_under_an_id_makes_an_entity_raise(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever the lamp puts under an id, every entity still says something.

    And what it says is unknown, or what the value means: a value none of
    them expected - a float where a flag belongs, a slot of the wrong
    length, a map, a null - comes out as "unknown", and one that is what
    its id means comes out as that. One entity raising used to keep the
    update from all the others, and still fills the log.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    _coordinate_sensors_from_before(hass, entry)  # every entity, these too
    await _setup_without_bluetooth(hass, entry)
    coordinator = entry.runtime_data
    raised: list[str] = []

    def _told(frame: bytes) -> None:
        caplog.clear()
        try:
            coordinator._ingest(frame)
        except Exception as err:  # collected, to name them all
            raised.append(f"{frame.hex()}: {err!r}")
        raised.extend(
            f"{frame.hex()}: {record.getMessage()} ({record.exc_info[1]!r})"
            for record in caplog.records
            if record.levelno >= logging.ERROR and record.exc_info
        )
        raised.extend(
            f"{frame.hex()}: {wrong}"
            for wrong in _shown_otherwise(hass, coordinator.state)
        )

    with caplog.at_level(logging.ERROR):
        for mode in _MODES:
            _told(cbor.encode(_WELL | mode))
            for key, well in (_WELL | mode).items():
                for odd in _ODD:
                    _told(cbor.encode({key: odd}))
                for null in _NULLS:
                    _told(b"\xa1" + cbor.encode(key) + null)
                _told(cbor.encode({key: well}))
    await hass.async_block_till_done()

    assert not raised, "\n".join(sorted(set(raised)))


async def test_an_entity_that_raises_is_named_and_the_others_still_follow(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The log says which entity failed, and the device page shows the rest.

    Which entity is told first is not fixed, so the others following is seen
    here only when the failing one happens to come first; that every listener
    is told whatever the ones before it did is pinned on the coordinator.
    """
    entry = await _setup_without_bluetooth(hass)
    bug = PropertyMock(side_effect=RuntimeError("a bug in the light"))

    with patch.object(GlowriumLight, "is_on", bug), caplog.at_level(logging.ERROR):
        entry.runtime_data._ingest(cbor.encode({KEY_INDICATOR: True}))
        entry.runtime_data._ingest(cbor.encode({KEY_INDICATOR: False}))
        await hass.async_block_till_done()

    assert hass.states.get("switch.glowrium_g7_1234_indicator_light").state == "off"
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(failures) == 1  # two reports, one fault: said once
    assert "light.glowrium_g7_1234 failed" in failures[0].getMessage()


async def test_an_entity_built_after_the_lamp_was_read_describes_it_in_full(
    hass: HomeAssistant,
) -> None:
    """What is already known when an entity is built goes into its description.

    A platform set up late - a reload of one, an entity added afterwards -
    is built by a coordinator that has read the lamp, and should not wait
    for the registry to be told a second time.
    """
    entry = _entry()
    entry.add_to_hass(hass)

    def _already_read(coordinator: object) -> None:
        coordinator.device_info = _parsed(G7_INFO)

    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
        autospec=True,
        side_effect=_already_read,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )


async def test_what_the_lamp_left_out_this_time_is_left_as_it_was(
    hass: HomeAssistant,
) -> None:
    """A device-info string without a field says nothing about that field.

    The registry is told what was read. A firmware the lamp did not mention
    this time is not thereby unknown, and the one on record stays.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)},
        sw_version="4",
        serial_number="CST-0001",
    )
    await _setup_without_bluetooth(hass, entry)

    await _reads_what_the_lamp_is(entry, b"pkey:Glowrium-C051;;")

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )


async def test_a_remembered_mode_the_lamp_does_not_have_is_not_shown(
    hass: HomeAssistant,
) -> None:
    """A value kept from an earlier run is shown only if it is still an option."""
    mock_restore_cache(
        hass, (State("select.glowrium_g7_1234_lighting_mode", "moonlight"),)
    )
    await _setup_without_bluetooth(hass)

    assert hass.states.get("select.glowrium_g7_1234_lighting_mode").state == "unknown"


def test_the_select_itself_does_not_offer_a_mode_the_lamp_does_not_have() -> None:
    """The entity says so itself, rather than leaving it to Home Assistant.

    Home Assistant drops a current option that is not among the options, so
    the state above reads the same either way. What the entity answers is
    asked here directly.
    """
    select = GlowriumLightingModeSelect(ScriptedLamp().coordinator(None))

    select._restored = "moonlight"
    assert select.current_option is None

    select._restored = "balance"
    assert select.current_option == "balance"


def test_a_report_of_nothing_is_a_report_and_the_remembered_mode_steps_back() -> None:
    """What is under the id decides, whatever it is - even when it is nothing.

    The remembered mode stands in while the id is absent from the mirror. A
    value that is there and names no preset is the lamp having spoken.
    """
    select = GlowriumLightingModeSelect(ScriptedLamp().coordinator(None))
    select._restored = "balance"
    assert select.current_option == "balance"

    select._coordinator._mirror.echo({KEY_LIGHTING_MODE: None})
    assert select.current_option is None


async def test_the_lighting_mode_shown_is_the_one_the_lamp_reports(
    hass: HomeAssistant,
) -> None:
    """The select follows the lamp: the index it reports is shown as its key.

    A value remembered from before stands in only until the lamp says
    something.
    """
    mock_restore_cache(
        hass, (State("select.glowrium_g7_1234_lighting_mode", "balance"),)
    )
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    select = "select.glowrium_g7_1234_lighting_mode"
    assert hass.states.get(select).state == "balance"

    coordinator._ingest(cbor.encode({KEY_LIGHTING_MODE: 5}))
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "sunrise_sync"

    coordinator._ingest(cbor.encode({KEY_LIGHTING_MODE: 32}))
    await hass.async_block_till_done()
    assert hass.states.get(select).state == "enhanced_two_phase"


@pytest.mark.parametrize(
    "model_id",
    [
        pytest.param("Glowrium-C051", id="a model with a profile"),
        pytest.param("Glowrium-C064", id="a model without one"),
    ],
)
async def test_an_index_the_model_does_not_have_is_not_the_remembered_mode(
    hass: HomeAssistant, model_id: str
) -> None:
    """The lamp has spoken, and what it said is no preset known here: unknown.

    The remembered mode stands in while nothing has been read. Falling back on
    it for an index that maps to nothing showed whatever the select had shown
    before the last restart - and Home Assistant recorded the change to it,
    for an automation to act on. With the presets of a model that has no
    profile of its own, which are a guess, that is the likelier case.
    """
    select = "select.glowrium_g7_1234_lighting_mode"
    mock_restore_cache(hass, (State(select, "balance"),))
    entry = _entry(**{CONF_MODEL_ID: model_id})
    entry.add_to_hass(hass)
    await _setup_without_bluetooth(hass, entry)
    coordinator = entry.runtime_data
    assert hass.states.get(select).state == "balance"  # nothing read yet
    shown: list[str] = []

    @callback
    def _changed(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        assert new is not None
        shown.append(new.state)

    async_track_state_change_event(hass, select, _changed)

    coordinator._ingest(cbor.encode({KEY_LIGHTING_MODE: 5}))
    await hass.async_block_till_done()
    coordinator._ingest(cbor.encode({KEY_LIGHTING_MODE: 7}))  # no preset has it
    await hass.async_block_till_done()

    assert hass.states.get(select).state == "unknown"
    assert shown == ["sunrise_sync", "unknown"]  # and never back to "balance"


# --- A model without a profile: its owner is told ------------------------------


async def test_the_notice_links_to_a_heading_the_guide_has(
    hass: HomeAssistant,
) -> None:
    """Where the notice sends its reader is a place that is there.

    The link names a heading of the contributor guide by the anchor GitHub
    makes of it. A heading reworded leaves the link opening the top of the
    page, with nothing to say that it used to open somewhere.
    """
    await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    (notice,) = _notices(hass)

    assert notice.learn_more_url is not None
    page, _, anchor = notice.learn_more_url.partition("#")
    assert page.endswith("/CONTRIBUTING.md")
    assert anchor in anchors((ROOT / "CONTRIBUTING.md").read_text())


async def test_a_choice_is_written_for_a_model_without_a_profile_as_it_was(
    hass: HomeAssistant,
) -> None:
    """The notice says what the presets are, and changes nothing about them.

    A lamp whose model has no profile is offered the G7's eight presets by
    their keys, and choosing one writes the G7's index for it: the frame is
    the one captured from the vendor app making the same choice on a G7.
    """
    entry = _entry(**{CONF_MODEL_ID: "Glowrium-C064"})
    entry.add_to_hass(hass)
    await _setup_without_bluetooth(hass, entry)
    select = "select.glowrium_g7_1234_lighting_mode"

    assert hass.states.get(select).attributes["options"] == [
        "sun_sync",
        "before_sunrise",
        "sunrise_sync",
        "sunset_sync",
        "after_sunset",
        "two_phase",
        "balance",
        "enhanced_two_phase",
    ]
    lamp = ScriptedLamp()
    with in_range(lamp):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select, "option": "sunrise_sync"},
            blocking=True,
        )

    assert lamp.written == [
        (WRITE_UUID, bytes.fromhex("a4182b05182c4202d0182f420e10183242001e"))
    ]


async def test_the_notice_goes_with_the_entry(hass: HomeAssistant) -> None:
    """A lamp that is no longer set up has nothing left to be said about.

    Home Assistant does not take an integration's repairs away with its
    entry, and by then there is no coordinator left to do it.
    """
    entry = await _started(hass, **{CONF_MODEL_ID: "Glowrium-C064"})
    assert len(_notices(hass)) == 1

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _notices(hass) == []


@pytest.mark.parametrize(
    "claimed",
    [
        pytest.param("Glowrium-C064", id="a model id"),
        pytest.param("Glowrium-C051,devid:CST-0001", id="not shaped like one"),
    ],
)
async def test_the_notice_has_words_for_what_it_brings(
    hass: HomeAssistant, claimed: str
) -> None:
    """The text Home Assistant serves for the notice takes what the notice gives.

    A repair raised under a key that has no text shows its owner the key, and
    a placeholder the text names and the repair does not bring is shown as a
    brace. The words are asked of Home Assistant, as the frontend asks.
    """
    await _started(hass, **{CONF_MODEL_ID: claimed})
    (notice,) = _notices(hass)

    words = await async_get_translations(hass, "en", "issues", {DOMAIN})
    key = f"component.{DOMAIN}.issues.{notice.translation_key}"
    text = words[f"{key}.title"] + words[f"{key}.description"]

    assert notice.translation_placeholders is not None
    assert set(re.findall(r"{(\w+)}", text)) == set(notice.translation_placeholders)


# --- What cannot be used is unknown: not a guess, and not what was remembered ---

_TWELVE = bytes.fromhex("0100000006001200640000") + b"\x00"  # a schedule, and a byte
_LATITUDE = "sensor.glowrium_aa_bb_cc_dd_ee_ff_latitude"
_LONGITUDE = "sensor.glowrium_aa_bb_cc_dd_ee_ff_longitude"
# What each entity showed before the restart, where it remembers anything.
_BEFORE = {
    "select.glowrium_g7_1234_operating_mode": "schedule",
    "switch.glowrium_g7_1234_indicator_light": "on",
    "select.glowrium_g7_1234_lighting_mode": "balance",
    "number.glowrium_g7_1234_ramp_time": "45",
    "switch.glowrium_g7_1234_daylight_saving_time": "on",
    "number.glowrium_g7_1234_schedule_brightness": "55",
    "time.glowrium_g7_1234_schedule_start": "07:30:00",
    "time.glowrium_g7_1234_schedule_end": "19:45:00",
    "number.glowrium_g7_1234_schedule_gradual": "15",
}


async def _every_entity(hass: HomeAssistant) -> MockConfigEntry:
    """Set the entry up with every entity enabled, the coordinate sensors too."""
    entry = _entry()
    entry.add_to_hass(hass)
    _coordinate_sensors_from_before(hass, entry)
    await _setup_without_bluetooth(hass, entry)
    return entry


@pytest.mark.parametrize(
    ("entity", "reported"),
    [
        pytest.param(
            "light.glowrium_g7_1234", {KEY_POWER: "on"}, id="text for the power"
        ),
        pytest.param(
            "select.glowrium_g7_1234_operating_mode",
            {KEY_CIRCADIAN: b"\x00", KEY_SCHEDULE: False},
            id="a zero byte for the Circadian flag",
        ),
        pytest.param(
            "switch.glowrium_g7_1234_indicator_light",
            {KEY_INDICATOR: "on"},
            id="text for the indicator",
        ),
        pytest.param(
            "binary_sensor.glowrium_g7_1234_activated",
            {KEY_ACTIVATED: "yes"},
            id="text for the activation",
        ),
        pytest.param(
            "select.glowrium_g7_1234_lighting_mode",
            {KEY_LIGHTING_MODE: True},
            id="a flag for the lighting mode",
        ),
        pytest.param(
            "number.glowrium_g7_1234_ramp_time",
            {KEY_RAMP: b"\xff" * 5},
            id="five bytes for the ramp",
        ),
        pytest.param(
            "switch.glowrium_g7_1234_daylight_saving_time",
            {KEY_DST: b"\x01"},
            id="one byte for daylight saving",
        ),
        pytest.param(
            "number.glowrium_g7_1234_schedule_brightness",
            {KEY_TIMER: b"\xff" * 40},
            id="forty bytes for the schedule",
        ),
        pytest.param(
            "time.glowrium_g7_1234_schedule_start",
            {KEY_TIMER: _TWELVE},
            id="twelve bytes for the schedule, its start",
        ),
        pytest.param(
            "time.glowrium_g7_1234_schedule_end",
            {KEY_TIMER: _TWELVE},
            id="twelve bytes for the schedule, its end",
        ),
        pytest.param(
            "number.glowrium_g7_1234_schedule_gradual",
            {KEY_TIMER: _TWELVE},
            id="twelve bytes for the schedule, its fade",
        ),
        pytest.param(_LATITUDE, {KEY_LATITUDE: float("inf")}, id="no latitude"),
        pytest.param(_LONGITUDE, {KEY_LONGITUDE: 500.0}, id="500 degrees"),
    ],
)
@pytest.mark.parametrize(
    "remembers", [False, True], ids=["nothing remembered", "a value remembered"]
)
async def test_a_value_that_cannot_be_used_is_shown_as_unknown(
    hass: HomeAssistant, entity: str, reported: dict[int, Any], remembers: bool
) -> None:
    """What a lamp reports under an id is not always what the id means.

    Each of these was shown as something: text as "on", a zero byte as the
    Circadian mode, a flag as the first preset, five bytes of ``ff`` as 1092
    minutes, forty of them as 255 %, twelve bytes as the schedule in the
    first eleven, ``inf`` as a latitude. And a setting with a value from
    before the restart showed that one, as if the lamp had said nothing. It
    has said something, and what it said cannot be used: that is unknown.
    """
    if remembers and entity in _BEFORE:
        mock_restore_cache(hass, (State(entity, _BEFORE[entity]),))
    entry = await _every_entity(hass)

    entry.runtime_data._ingest(cbor.encode(reported))
    await hass.async_block_till_done()

    assert hass.states.get(entity).state == "unknown"


@pytest.mark.parametrize(
    ("entity", "usable", "shown", "unusable"),
    [
        pytest.param(
            "number.glowrium_g7_1234_ramp_time",
            {KEY_RAMP: bytes.fromhex("0708")},
            "30",
            {KEY_RAMP: b"\xff" * 5},
            id="the ramp",
        ),
        pytest.param(
            "switch.glowrium_g7_1234_daylight_saving_time",
            {KEY_DST: bytes.fromhex("0000000e10")},
            "off",
            {KEY_DST: b"\x01"},
            id="daylight saving",
        ),
        pytest.param(
            "switch.glowrium_g7_1234_indicator_light",
            {KEY_INDICATOR: False},
            "off",
            {KEY_INDICATOR: "on"},
            id="the indicator",
        ),
        pytest.param(
            "time.glowrium_g7_1234_schedule_start",
            {KEY_TIMER: bytes.fromhex("01000000061e1200460258")},
            "06:30:00",
            {KEY_TIMER: _TWELVE},
            id="the schedule's start",
        ),
        pytest.param(
            "time.glowrium_g7_1234_schedule_end",
            {KEY_TIMER: bytes.fromhex("01000000061e1200460258")},
            "18:00:00",
            {KEY_TIMER: _TWELVE},
            id="the schedule's end",
        ),
        pytest.param(
            "number.glowrium_g7_1234_schedule_gradual",
            {KEY_TIMER: bytes.fromhex("01000000061e1200460258")},
            "10",
            {KEY_TIMER: _TWELVE},
            id="the schedule's fade",
        ),
        pytest.param(
            "number.glowrium_g7_1234_schedule_brightness",
            {KEY_TIMER: bytes.fromhex("01000000061e1200460258")},
            "70",
            {KEY_TIMER: _TWELVE},
            id="the schedule's brightness",
        ),
        pytest.param(
            "select.glowrium_g7_1234_operating_mode",
            {KEY_CIRCADIAN: True, KEY_SCHEDULE: False},
            "circadian",
            {KEY_CIRCADIAN: "on"},
            id="the operating mode",
        ),
    ],
)
async def test_a_setting_shows_what_was_remembered_only_until_the_lamp_speaks(
    hass: HomeAssistant,
    entity: str,
    usable: dict[int, Any],
    shown: str,
    unusable: dict[int, Any],
) -> None:
    """Remembered, then reported, then reported as something that cannot be used.

    The value from before the restart stands in while the lamp has said
    nothing. A report replaces it. A later report that cannot be used is
    unknown - and not a step back to what was remembered, which Home
    Assistant would record as the setting changing to a value nobody set.
    """
    mock_restore_cache(hass, (State(entity, _BEFORE[entity]),))
    entry = await _every_entity(hass)
    seen = [hass.states.get(entity).state]

    for report in (usable, unusable):
        entry.runtime_data._ingest(cbor.encode(report))
        await hass.async_block_till_done()
        seen.append(hass.states.get(entity).state)

    assert (
        float(seen[0]) == float(_BEFORE[entity])
        if shown.isdigit()
        else (seen[0] == _BEFORE[entity])
    )
    assert seen[1:] == [shown, "unknown"]


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({KEY_CIRCADIAN: "on", KEY_SCHEDULE: False}, id="text"),
        pytest.param({KEY_CIRCADIAN: False, KEY_SCHEDULE: b"\x00"}, id="a byte"),
        pytest.param({KEY_CIRCADIAN: 1}, id="a number, and one flag only"),
    ],
)
async def test_nothing_goes_unavailable_for_a_mode_that_cannot_be_used(
    hass: HomeAssistant, flags: dict[int, Any]
) -> None:
    """A mode that cannot be read is a mode that is not known, for availability.

    The entities that belong to one mode are unavailable while the lamp is in
    another, and available while its mode is not known - so that nothing
    collapses before the lamp has spoken. A mode flag that is no flag leaves
    the mode as unknown as that: the settings of both modes stay where they
    are, and only the mode itself reads unknown.
    """
    entry = await _every_entity(hass)

    entry.runtime_data._ingest(cbor.encode(flags))
    await hass.async_block_till_done()

    assert hass.states.get("select.glowrium_g7_1234_operating_mode").state == (
        "unknown"
    )
    for of_a_mode in (
        "select.glowrium_g7_1234_lighting_mode",
        "number.glowrium_g7_1234_ramp_time",
        "time.glowrium_g7_1234_schedule_start",
        "time.glowrium_g7_1234_schedule_end",
        "number.glowrium_g7_1234_schedule_gradual",
        "number.glowrium_g7_1234_schedule_brightness",
    ):
        assert hass.states.get(of_a_mode).state != "unavailable", of_a_mode


_PLATFORMS_READ = sorted(
    path
    for path in (ROOT / "custom_components" / "glowrium").glob("*.py")
    if path.stem in {platform.value for platform in PLATFORMS}
)


@pytest.mark.parametrize("path", _PLATFORMS_READ, ids=lambda path: path.stem)
def test_no_platform_reads_the_state_mirror_itself(path: Path) -> None:
    """An entity asks the coordinator what a property is, never what is under an id.

    The mirror keeps what the lamp said, as it said it. What that is - a
    flag, a level, a slot, or nothing that can be used - is answered in one
    place, and an entity that looked under an id for itself would be a
    second place, with an answer of its own.
    """
    reads = [
        node.lineno
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == "state"
    ]

    assert reads == [], f"{path.name} reads .state at lines {reads}"
