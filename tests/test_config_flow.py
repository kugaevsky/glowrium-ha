"""Tests for the Glowrium config flow."""

import json
from pathlib import Path
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowrium.const import DOMAIN

GLOWRIUM_ADDRESS = "AA:BB:CC:DD:EE:FF"
GLOWRIUM_NAME = "Glowrium-G7_1234"

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "glowrium"
# What the form that lists the lamps is given to say, in strings.json and in
# each language. Read here and not in a test: a test runs in the event loop.
FORM_WORDS = {
    path.name: json.loads(path.read_text())["config"]["step"]["user"]
    for path in (
        INTEGRATION / "strings.json",
        *sorted(INTEGRATION.glob("translations/*.json")),
    )
}


def _service_info(
    address: str = GLOWRIUM_ADDRESS, name: str | None = GLOWRIUM_NAME
) -> BluetoothServiceInfoBleak:
    """Fabricate a discovered-device record without a real Bluetooth stack."""
    device = BLEDevice(address, name, details=None)
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        device, advertisement, "local", 0.0, connectable=True
    )


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """The user flow aborts when no Glowrium devices were discovered."""
    with patch(
        "custom_components.glowrium.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The user flow lists Glowrium devices and creates a config entry."""
    with (
        patch(
            "custom_components.glowrium.config_flow.async_discovered_service_info",
            return_value=[_service_info()],
        ),
        patch(
            "custom_components.glowrium.async_setup_entry", return_value=True
        ) as mock_setup_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_ADDRESS: GLOWRIUM_ADDRESS}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{GLOWRIUM_NAME} ({GLOWRIUM_ADDRESS})"
    assert result["data"] == {CONF_ADDRESS: GLOWRIUM_ADDRESS}
    assert result["result"].unique_id == GLOWRIUM_ADDRESS
    assert len(mock_setup_entry.mock_calls) == 1


def _configured(hass: HomeAssistant) -> MockConfigEntry:
    """Put an entry for the lamp in place, as if it had been set up before."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=GLOWRIUM_NAME,
        unique_id=GLOWRIUM_ADDRESS,
        data={CONF_ADDRESS: GLOWRIUM_ADDRESS},
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_discovered_lamp_is_confirmed_and_set_up(hass: HomeAssistant) -> None:
    """Discovery is the way most lamps arrive, and it was not tested at all.

    Home Assistant hands over the advertisement, the user is shown the lamp by
    name and asked to confirm, and the entry is keyed by the lamp's address.
    """
    with patch(
        "custom_components.glowrium.async_setup_entry", return_value=True
    ) as mock_setup_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_service_info()
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"
        assert result["description_placeholders"] == {"name": GLOWRIUM_NAME}
        # The name is what tells two discovered lamps apart in the list.
        progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert progress[0]["context"]["title_placeholders"] == {"name": GLOWRIUM_NAME}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == GLOWRIUM_NAME
    assert result["data"] == {CONF_ADDRESS: GLOWRIUM_ADDRESS}
    assert result["result"].unique_id == GLOWRIUM_ADDRESS
    assert len(mock_setup_entry.mock_calls) == 1


async def test_a_lamp_that_advertises_no_name_is_shown_by_its_address(
    hass: HomeAssistant,
) -> None:
    """The entry needs a title even when the advertisement carries none."""
    with patch("custom_components.glowrium.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=_service_info(name=None),
        )
        assert result["description_placeholders"] == {"name": GLOWRIUM_ADDRESS}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == GLOWRIUM_ADDRESS


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Glowrium [free lamps](https://lamps.example)", None),
        ("Glowrium ![](https://lamps.example/seen.png)", None),
        ("Glowrium <img src=//lamps.example/seen>", None),
        ("Glowrium www.lamps.example", "Glowrium www lamps example"),
        ("Glowrium **G7** `x`", "Glowrium G7 x"),
        ("Glowrium-G7_1234 " + "A" * 200, None),
        ("Glowrium Лампа-7", "Glowrium Лампа-7"),
    ],
)
async def test_the_dialog_shows_a_discovered_lamps_name_as_text_and_nothing_more(
    hass: HomeAssistant, name: str, shown: str | None
) -> None:
    """The lamp's name comes off the air, and the dialog is rendered as Markdown.

    Whatever advertises a name beginning with "Glowrium" is offered for set-up,
    and the confirmation asks about it by that name. Put in as it is, the name
    could carry a link or an image - which the browser would fetch - into a
    question Home Assistant itself is asking. So it goes in as the repair for
    a stuck Bluetooth stack already puts it: letters, digits, spaces, dashes
    and underscores, and no more of it than it takes to recognise the lamp.

    The entry is still titled with the name as advertised: a title is shown as
    text, and it is what the lamp is recognised by afterwards.
    """
    with patch("custom_components.glowrium.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_service_info(name=name)
        )
        in_dialog = result["description_placeholders"]["name"]
        assert not set(in_dialog) & set("[]()!<>`*#|~\\:/=\"'.")
        assert in_dialog.strip() == in_dialog
        assert in_dialog.startswith("Glowrium")
        assert len(in_dialog) <= 48
        if shown is not None:
            assert in_dialog == shown
        # The same name stands in the list of discovered devices.
        progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert progress[0]["context"]["title_placeholders"] == {"name": in_dialog}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )
        await hass.async_block_till_done()

    assert result["title"] == name


async def test_a_lamp_already_set_up_is_not_discovered_again(
    hass: HomeAssistant,
) -> None:
    """The lamp advertises all the time; one entry per address is the limit."""
    _configured(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_service_info()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_the_list_offers_only_lamps_that_are_not_set_up(
    hass: HomeAssistant,
) -> None:
    """Picking by hand lists Glowrium lamps, and only the ones still free.

    Whatever else is advertising nearby is not offered, nor is a device with
    no name, nor the lamp that already has an entry.
    """
    _configured(hass)
    other = "11:22:33:44:55:66"
    with patch(
        "custom_components.glowrium.config_flow.async_discovered_service_info",
        return_value=[
            _service_info(),  # already set up
            _service_info(other, "Glowrium-G8_5678"),
            _service_info("22:22:22:22:22:22", "Some other lamp"),
            _service_info("33:33:33:33:33:33", None),
        ],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    offered = result["data_schema"].schema[CONF_ADDRESS].container
    assert offered == {other: f"Glowrium-G8_5678 ({other})"}


async def test_every_field_of_the_form_is_described_in_every_language(
    hass: HomeAssistant,
) -> None:
    """The list of lamps says what its lines are, whatever the language.

    Its one field was labelled "Device" and nothing more, which leaves
    someone with two lamps in range to work out what a line is made of. The
    fields are asked of the form itself, so that one added to it later has
    to be labelled and described as well - and described in the language,
    not in the English left where a translation was meant to go.
    """
    with patch(
        "custom_components.glowrium.config_flow.async_discovered_service_info",
        return_value=[_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    fields = [str(field) for field in result["data_schema"].schema]
    assert fields == [CONF_ADDRESS]

    assert len(FORM_WORDS) == 7  # strings.json and the six languages
    for field in fields:
        for file, words in FORM_WORDS.items():
            assert words["data"][field].strip(), file
            assert words.get("data_description", {}).get(field, "").strip(), file
        # Six wordings in seven files: en.json is strings.json word for word.
        described = {w["data_description"][field] for w in FORM_WORDS.values()}
        assert len(described) == 6


async def test_nothing_is_offered_when_the_only_lamp_is_set_up(
    hass: HomeAssistant,
) -> None:
    """With every lamp in range already configured there is nothing to pick."""
    _configured(hass)
    with patch(
        "custom_components.glowrium.config_flow.async_discovered_service_info",
        return_value=[_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_a_lamp_set_up_while_the_list_was_open_is_not_added_twice(
    hass: HomeAssistant,
) -> None:
    """The check for a duplicate is made when the choice is submitted.

    The list is built when the form opens. Discovery can set the same lamp up
    in the meantime, and the entry is keyed by address either way.
    """
    with patch(
        "custom_components.glowrium.config_flow.async_discovered_service_info",
        return_value=[_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM

    _configured(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_ADDRESS: GLOWRIUM_ADDRESS}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
