"""Tests for setting up and tearing down the Glowrium config entry."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_MODEL_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.icon import async_get_icons
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.glowrium import PLATFORMS, cbor, models
from custom_components.glowrium.const import (
    DOMAIN,
    KEY_BRIGHTNESS,
    KEY_INDICATOR,
    KEY_POWER,
)
from custom_components.glowrium.models import GlowriumModel

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
    await _setup_without_bluetooth(hass)

    domains = {
        state.entity_id.split(".")[0]
        for state in hass.states.async_all()
        if "glowrium" in state.entity_id
    }
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
            State("number.glowrium_g7_1234_ramp_time", "45"),
            State("select.glowrium_g7_1234_lighting_mode", "sunrise_sync"),
            State("light.glowrium_g7_1234", "on"),
        ),
    )
    await _setup_without_bluetooth(hass)

    assert hass.states.get("switch.glowrium_g7_1234_indicator_light").state == "on"
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
    """Return the lamp's entry in the device registry."""
    device = dr.async_get(hass).async_get_device(
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)}
    )
    assert device is not None
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
