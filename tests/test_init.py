"""Tests for setting up and tearing down the Glowrium config entry."""

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowrium import cbor
from custom_components.glowrium.const import DOMAIN, KEY_BRIGHTNESS, KEY_POWER


def _entry() -> MockConfigEntry:
    """Return a config entry for a lamp at a fixed address."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Glowrium-G7_1234",
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF"},
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


async def _setup_without_bluetooth(hass: HomeAssistant) -> MockConfigEntry:
    """Set up the entry with the radio stubbed out, and return it."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
        new_callable=AsyncMock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    # Availability follows advertisement presence, which the stubbed start
    # never observed; these tests are about the entities, not about that.
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
