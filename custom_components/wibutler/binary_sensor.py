import logging
from homeassistant.components.binary_sensor import BinarySensorEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Wibutler binary sensors from a config entry."""
    hub = hass.data[DOMAIN]["hub"]
    devices = hub.devices

    binary_sensors = []
    for device_id, device in devices.items():
        for component in device.get("components", []):
            name = component.get("name")

            if not name:
                continue

            # Nur `BTN_*`-Komponenten als Taster registrieren
            if not name.startswith("BTN"):
                continue

            # BTNRECON komplett ignorieren (keine Entität anlegen)
            if name == "BTNRECON":
                _LOGGER.debug(
                    "WiButler: BTNRECON von Gerät %s (%s) übersprungen.",
                    device.get("name"),
                    device_id,
                )
                continue

            binary_sensors.append(WibutlerBinarySensor(hub, device, component))

    async_add_entities(binary_sensors, True)


BUTTON_MAPPING = {
    "SWT": ["BTN_0", "BTN_1"],      # Single Rocker Switch
    "SWT_A": ["BTN_A0", "BTN_A1"],  # Left side Rocker
    "SWT_B": ["BTN_B0", "BTN_B1"],  # Right side Rocker
}


class WibutlerBinarySensor(BinarySensorEntity):
    """Representation of a Wibutler button (which acts like a binary sensor)."""

    def __init__(self, hub, device, component):
        """Initialize the binary sensor."""
        self._hub = hub
        self._device = device
        self._device_id = device["id"]
        self._component = component

        self._original_name = component["name"]  # z.B. BTN_0, BTN_A1, ...
        self._attr_name = f"{device['name']} - {component['text']}"
        self._attr_unique_id = f"{device['id']}_{component['name']}"
        self._attr_is_on = False  # Standardmäßig aus

        # Initialen Zustand einmalig aus den Komponenten holen
        self._fetch_state(device.get("components", []))

    @property
    def is_on(self) -> bool:
        """Return true if the button is pressed."""
        return self._attr_is_on

    @property
    def should_poll(self) -> bool:
        """Status kommt per WebSocket, kein Polling nötig."""
        return False

    def _fetch_state(self, components):
        """Holt den neuen Zustand aus Komponenten und setzt den Status."""
        for component in components:
            comp_name = component.get("name")
            if comp_name not in BUTTON_MAPPING:
                continue

            expected_buttons = BUTTON_MAPPING[comp_name]
            new_value = str(component.get("value", ""))

            if not new_value:
                continue

            # Erstes Zeichen = Index (0/1), letztes Zeichen = U/D
            button_index = new_value[0]   # "0" oder "1"
            button_state = new_value[-1]  # "U" oder "D"

            if comp_name == "SWT":
                expected_btn = f"BTN_{button_index}"
            else:
                # SWT_A / SWT_B
                candidate_a = f"BTN_A{button_index}"
                candidate_b = f"BTN_B{button_index}"
                expected_btn = candidate_a if candidate_a in expected_buttons else candidate_b

            if expected_btn == self._original_name:
                new_is_on = button_state == "D"
                if new_is_on != self._attr_is_on:
                    _LOGGER.debug(
                        "WiButler Button %s (%s/%s) Zustand: %s -> %s",
                        self._attr_name,
                        self._device_id,
                        self._original_name,
                        self._attr_is_on,
                        new_is_on,
                    )
                self._attr_is_on = new_is_on

    async def async_added_to_hass(self):
        """Register for WebSocket updates."""
        self._hub.register_listener(self)

    def handle_ws_update(self, device_id, components):
        """Process WebSocket update."""
        if device_id != self._device_id:
            return

        self._fetch_state(components)
        self.async_write_ha_state()
