"""Config flow for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
import voluptuous as vol

from . import identity
from .const import DOMAIN, NAME_PREFIX


class GlowriumConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Glowrium."""

    VERSION = 1

    # Set by async_step_bluetooth, which is the only way into the confirm step.
    _discovery_info: BluetoothServiceInfoBleak

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via the manifest's Bluetooth matcher."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a device discovered by Bluetooth.

        The dialog is rendered as Markdown and the name comes off the air,
        so the question is asked with the name as text and nothing more.
        A lamp that advertises no name goes by its address, which is what
        the Bluetooth stack made of it and stays as it is; so does the
        title, which names the entry and is not Markdown.
        """
        address = self._discovery_info.address
        title = self._discovery_info.name or address
        if user_input is not None:
            return self.async_create_entry(title=title, data={CONF_ADDRESS: address})

        self._set_confirm_only()
        self.context["title_placeholders"] = {"name": title}
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": title if title == address else identity.as_text(title)
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address], data={CONF_ADDRESS: address}
            )

        current_addresses = self._async_current_ids(include_ignore=False)
        for discovery_info in async_discovered_service_info(
            self.hass, connectable=True
        ):
            address = discovery_info.address
            name = discovery_info.name
            if (
                address in current_addresses
                or not name
                or not name.startswith(NAME_PREFIX)
            ):
                continue
            self._discovered_devices[address] = f"{name} ({address})"

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(  # type: ignore[arg-type, unused-ignore]
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )
