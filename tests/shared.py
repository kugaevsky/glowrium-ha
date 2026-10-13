"""What more than one file of tests arranges the same way.

A coordinator at a scripted lamp, with or without a link; an entry set up
with the radio stubbed out, and what is read off it. Each keeps the name
it had where it was written: the tests that use it were moved as they
are.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowrium import link as link_module
from custom_components.glowrium.const import (
    DOMAIN,
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_CIRCADIAN,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator
from custom_components.glowrium.keeper import EntryKeeper

from .lamp import LampLink, ScriptedLamp, in_range, link_of, nothing_heard


def _at_a_lamp(
    hass: HomeAssistant | None, name: str = "Glowrium-G7"
) -> tuple[GlowriumCoordinator, ScriptedLamp]:
    """Return a coordinator at a scripted lamp in range, with no link yet.

    A command dials the lamp bare - the link and the command's own write,
    nothing else - so what the lamp has been written is the command and only
    that (``lamp.written``). The link is taken on the first command, and the
    entities are told of it then.
    """
    lamp = ScriptedLamp()
    return lamp.coordinator(hass, name), lamp


def _an_entry() -> MagicMock:
    """Return what stands for the config entry of the scripted lamp.

    With what its keeper reads of an entry as it is built: the address of
    the lamp it is for.
    """
    entry = MagicMock()
    entry.data = {CONF_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    return entry


def _kept_for(
    hass: HomeAssistant, entry: MagicMock, name: str = "Glowrium-G7"
) -> tuple[GlowriumCoordinator, ScriptedLamp]:
    """Return a coordinator at a scripted lamp in range, kept for ``entry``.

    Kept as Home Assistant keeps one: what the device half asks to have
    started goes where the keeper of a config entry puts it, which is what
    a test of the entry's part in it looks at.
    """
    lamp = ScriptedLamp()
    return lamp.coordinator(hass, name, keeper=EntryKeeper(hass, entry)), lamp


async def _holding_a_link(
    hass: HomeAssistant | None, name: str = "Glowrium-G7"
) -> tuple[GlowriumCoordinator, ScriptedLamp, LampLink]:
    """Return a coordinator holding a bare link to a scripted lamp, and that link.

    Taken the way a command takes one: nothing asked of the lamp and nothing
    read, as on a link no background connect has run on. The command's own
    write and its echo are cleared away, so the mirror reads as unread and
    ``lamp.written`` starts empty - what is written next is the test's.
    """
    coordinator, lamp = _at_a_lamp(hass, name)
    await coordinator.async_set_indicator(True)
    lamp.forget()
    nothing_heard(coordinator)  # the command's echo gone; a mirror forgets nothing
    return coordinator, lamp, lamp.links[-1]


def _info_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what the link logged at INFO, the level a user reads."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and record.name == link_module.__name__
    ]


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
    # The stubbed start took no look at the air. Availability follows
    # advertisement presence; these tests are about the entities, not about
    # that.
    link_of(entry.runtime_data).begin(present=True)
    entry.runtime_data._async_notify_listeners()
    await hass.async_block_till_done()
    return entry


async def _reads_what_the_lamp_is(
    entry: MockConfigEntry, info: bytes | None
) -> ScriptedLamp:
    """Connect to a lamp whose device-info string reads as ``info``; return the lamp.

    As the integration reads it: last, in the first exchange on a link the
    poll has dialled - the lamp, a G7 in its circadian mode, answers the
    state request first. ``None`` is a lamp that gives nothing to read.
    """
    lamp = ScriptedLamp()
    lamp.answers({KEY_ACTIVATED: True, KEY_CIRCADIAN: True})
    if info is not None:
        lamp.readable(INFO_UUID, info)
    with in_range(lamp):
        await link_of(entry.runtime_data).connect()
    return lamp


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


def _slow_to_connect() -> ScriptedLamp:
    """Return a lamp in range whose connect never comes: nothing is read of it."""
    lamp = ScriptedLamp()
    lamp.dials_when(asyncio.Event())
    return lamp


async def _started(hass: HomeAssistant, **data: str) -> MockConfigEntry:
    """Set an entry up as the integration starts one, by a lamp slow to connect.

    The start is the real one - the entry taken, the watchers put up, the
    first connect on its way - and nothing is ever read: what is known of the
    lamp is what the entry remembers.
    """
    entry = _entry(**data)
    entry.add_to_hass(hass)
    with in_range(_slow_to_connect()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _notices(hass: HomeAssistant) -> list[ir.IssueEntry]:
    """Return the repairs that say a lamp's presets are unconfirmed."""
    return [
        issue
        for (domain, _id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key.startswith("presets_unconfirmed")
    ]
