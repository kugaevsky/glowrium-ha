"""Tests for the Glowrium config flow."""

from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.glowrium.const import DOMAIN

GLOWRIUM_ADDRESS = "AA:BB:CC:DD:EE:FF"
GLOWRIUM_NAME = "Glowrium-G7_1234"


def _service_info() -> BluetoothServiceInfoBleak:
    """Fabricate a discovered-device record without a real Bluetooth stack."""
    device = BLEDevice(GLOWRIUM_ADDRESS, GLOWRIUM_NAME, details=None)
    advertisement = AdvertisementData(
        local_name=GLOWRIUM_NAME,
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
