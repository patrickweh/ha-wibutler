"""Wibutler integration for Home Assistant."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import WibutlerHub
from .const import PLATFORMS
from .rocker import RockerController

_LOGGER = logging.getLogger(__name__)

type WibutlerConfigEntry = ConfigEntry[WibutlerHub]

ROCKER_CONTROLLER_KEY = "rocker_controller"


def _setup_rocker_controller(
    hass: HomeAssistant, entry: WibutlerConfigEntry
) -> None:
    """Create and start rocker controller if bindings exist."""
    options = entry.options or {}
    bindings = options.get("rocker_bindings", [])

    old_controller = getattr(entry, "rocker_controller", None)
    if old_controller:
        old_controller.stop()
        entry.rocker_controller = None

    if bindings:
        dim_duration = options.get("dim_duration", 5.0)
        controller = RockerController(hass, bindings, dim_duration=dim_duration)
        controller.start()
        entry.rocker_controller = controller


async def _async_options_updated(
    hass: HomeAssistant, entry: WibutlerConfigEntry
) -> None:
    """Handle options update - recreate rocker controller."""
    _setup_rocker_controller(hass, entry)


async def async_setup_entry(hass: HomeAssistant, entry: WibutlerConfigEntry) -> bool:
    """Set up Wibutler from a config entry."""
    hub = WibutlerHub(
        hass,
        entry.data["host"],
        entry.data.get("port", 8081),
        entry.data["username"],
        entry.data["password"],
        entry.data.get("verify_ssl", False),
        entry.data.get("use_ssl", False),
    )

    if not await hub.authenticate():
        raise ConfigEntryNotReady("Wibutler hub authentication failed, will retry")

    hub.devices = await hub.get_devices()
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_create_background_task(
        hass, hub.connect_websocket(), "wibutler_websocket"
    )

    _setup_rocker_controller(hass, entry)
    entry.async_on_unload(
        entry.add_update_listener(_async_options_updated)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: WibutlerConfigEntry) -> bool:
    """Unload a config entry."""
    controller = getattr(entry, "rocker_controller", None)
    if controller:
        controller.stop()
        entry.rocker_controller = None

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.close()
    return unload_ok
