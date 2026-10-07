"""Tests for setting up and tearing down the Glowrium config entry."""

import asyncio
import importlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from bleak.exc import BleakError
from homeassistant.components.logger.helpers import get_integration_loggers
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_MODEL_ID,
    EVENT_HOMEASSISTANT_STOP,
    EntityCategory,
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
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.glowrium import PLATFORMS, cbor, models
from custom_components.glowrium.const import (
    DOMAIN,
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
)
from custom_components.glowrium.coordinator import (
    GlowriumCoordinator,
    _parse_device_info as _parsed,
)
from custom_components.glowrium.light import GlowriumLight
from custom_components.glowrium.models import GlowriumModel
from custom_components.glowrium.select import GlowriumLightingModeSelect

ADDRESS = "AA:BB:CC:DD:EE:FF"
G7_INFO = b"brand:INLEDCO;pkey:Glowrium-C051;devid:CST-0001;mac:x;version:4;;"


def _entry(**data: str) -> MockConfigEntry:
    """Return a config entry for a lamp at a fixed address."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Glowrium-G7_1234",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, **data},
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

    async def _record(_self: object, started_with: object) -> None:
        # The entry is also what ties the background connect to the entry's
        # lifecycle, so pin that the right one is handed over.
        assert started_with is entry
        seen.append(entry.runtime_data)

    with (
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

    async def _never_connects(_self: object) -> None:
        await asyncio.Event().wait()

    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator"
        "._async_ensure_connected",
        autospec=True,
        side_effect=_never_connects,
    ):
        async with asyncio.timeout(2):
            assert await hass.config_entries.async_setup(entry.entry_id)

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

    async def _never_connects(_self: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator"
        "._async_ensure_connected",
        autospec=True,
        side_effect=_never_connects,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert not cancelled.is_set()  # still running while the entry is loaded
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert cancelled.is_set()


async def _setup_without_bluetooth(
    hass: HomeAssistant, entry: MockConfigEntry | None = None
) -> MockConfigEntry:
    """Set up the entry with the radio stubbed out, and return it."""
    if entry is None:
        entry = _entry()
        entry.add_to_hass(hass)
    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
        new_callable=AsyncMock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    # The stubbed start took neither the entry nor a look at the air.
    # Availability follows advertisement presence; these tests are about the
    # entities, not about that.
    entry.runtime_data._entry = entry
    entry.runtime_data._present = True
    entry.runtime_data._async_notify_listeners()
    await hass.async_block_till_done()
    return entry


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
    short - a container is given ten seconds - never gets that far, and BlueZ
    keeps the link: seen on the real host as a lamp that reads "Connected: yes"
    and answers "Not connected" to everything until the adapter is power-cycled.
    """
    entry = await _setup_without_bluetooth(hass)
    coordinator = entry.runtime_data
    client = MagicMock()
    client.is_connected = True
    client.disconnect = AsyncMock()
    coordinator._client = client

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    client.disconnect.assert_awaited_once()
    assert coordinator._client is None


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


def _naming_itself(info: bytes) -> MagicMock:
    """Return a link on which the device-info string reads as ``info``."""
    client = MagicMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray(info))
    return client


def _device(hass: HomeAssistant) -> dr.DeviceEntry:
    """Return the lamp's entry in the device registry.

    Found through the config entry, the way the integration finds it: from
    Home Assistant 2026.10 a lookup by connection is deprecated.
    """
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return device


def _described(device: dr.DeviceEntry) -> tuple[str | None, ...]:
    """Return what the device page shows of the lamp's own description."""
    return (device.model, device.model_id, device.sw_version, device.serial_number)


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

    await entry.runtime_data._async_read_device_info(_naming_itself(G7_INFO))

    assert _described(_device(hass)) == (
        "Glowrium G7",
        "Glowrium-C051",
        "4",
        "CST-0001",
    )


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
    await entry.runtime_data._async_read_device_info(_naming_itself(G7_INFO))
    assert entry.data[CONF_MODEL_ID] == "Glowrium-C051"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup_without_bluetooth(hass, entry)

    restarted = entry.runtime_data
    assert not restarted.device_info  # nothing read in this session
    assert restarted.model is models.G7
    assert restarted.model_id == "Glowrium-C051"


async def test_a_remembered_model_does_not_stand_in_for_reading_it(
    hass: HomeAssistant,
) -> None:
    """Firmware can change between starts; what was remembered is not re-read."""
    entry = _entry(**{CONF_MODEL_ID: "Glowrium-C051"})
    entry.add_to_hass(hass)
    await _setup_without_bluetooth(hass, entry)
    client = _naming_itself(G7_INFO.replace(b"version:4", b"version:5"))

    await entry.runtime_data._async_read_device_info(client)

    client.read_gatt_char.assert_awaited_once()
    assert _device(hass).sw_version == "5"


async def test_a_model_without_a_profile_still_shows_what_it_is(
    hass: HomeAssistant,
) -> None:
    """An unknown model id is shown as itself, under the family's name."""
    entry = await _setup_without_bluetooth(hass)

    await entry.runtime_data._async_read_device_info(
        _naming_itself(b"brand:INLEDCO;pkey:Glowrium-C064;devid:CST-9;version:2;;")
    )

    assert _described(_device(hass)) == ("Glowrium", "Glowrium-C064", "2", "CST-9")
    assert entry.data[CONF_MODEL_ID] == "Glowrium-C064"


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
    client = MagicMock()
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    await entry.runtime_data._async_read_device_info(client)

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

    await coordinator._async_read_device_info(
        _naming_itself(b"pkey:Glowrium-TEST;version:1;;")
    )
    coordinator._async_notify_listeners()  # as the connect does after the read
    await hass.async_block_till_done()

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
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.disconnect = AsyncMock()
    coordinator._client = client
    hass.config.latitude = 0
    hass.config.longitude = 0
    domain, service, data = _call("button", "press", "_sync_location")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(domain, service, data, blocking=True)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "home_location_not_set"
    # The user's to put right, not a fault of the lamp or the link: Home
    # Assistant shows such a refusal and keeps it out of the log.
    assert isinstance(err.value, ServiceValidationError)
    # What the user is shown is the message, with the lamp named in it - not
    # the key, which is what Home Assistant falls back on for one it lacks.
    assert "Glowrium-G7_1234" in str(err.value)
    client.write_gatt_char.assert_not_awaited()
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


async def test_no_value_under_an_id_makes_an_entity_raise(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever the lamp puts under an id, every entity still says something.

    Each entity reads the mirror its own way and guards for its own types; a
    value none of them expected - a float where a flag belongs, a slot of the
    wrong length, a map - has to come out as "unknown" at worst. One entity
    raising used to keep the update from all the others, and still fills the
    log.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    _coordinate_sensors_from_before(hass, entry)  # every entity, these too
    await _setup_without_bluetooth(hass, entry)
    coordinator = entry.runtime_data
    raised: list[str] = []

    def _told(values: dict[int, Any]) -> None:
        caplog.clear()
        try:
            coordinator._ingest(cbor.encode(values))
        except Exception as err:  # collected, to name them all
            raised.append(f"{values!r}: {err!r}")
        raised.extend(
            f"{values!r}: {record.getMessage()} ({record.exc_info[1]!r})"
            for record in caplog.records
            if record.levelno >= logging.ERROR and record.exc_info
        )

    with caplog.at_level(logging.ERROR):
        for mode in _MODES:
            _told(_WELL | mode)
            for key, well in (_WELL | mode).items():
                for odd in _ODD:
                    _told({key: odd})
                _told({key: well})
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

    def _already_read(coordinator: object, _entry: object) -> None:
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

    await entry.runtime_data._async_read_device_info(
        _naming_itself(b"pkey:Glowrium-C051;;")
    )

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
    select = GlowriumLightingModeSelect(
        GlowriumCoordinator(None, ADDRESS, "Glowrium-G7")
    )

    select._restored = "moonlight"
    assert select.current_option is None

    select._restored = "balance"
    assert select.current_option == "balance"


def test_a_report_of_nothing_is_a_report_and_the_remembered_mode_steps_back() -> None:
    """What is under the id decides, whatever it is - even when it is nothing.

    The remembered mode stands in while the id is absent from the mirror. A
    value that is there and names no preset is the lamp having spoken.
    """
    select = GlowriumLightingModeSelect(
        GlowriumCoordinator(None, ADDRESS, "Glowrium-G7")
    )
    select._restored = "balance"
    assert select.current_option == "balance"

    select._coordinator.state[KEY_LIGHTING_MODE] = None
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

    await entry.runtime_data._async_read_device_info(
        _naming_itself(b"brand:INLEDCO;version:7;;")
    )

    assert _described(_device(hass)) == ("Glowrium G8", "Glowrium-C064", "7", None)
