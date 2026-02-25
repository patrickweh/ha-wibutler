import logging
from homeassistant.components.light import (
    LightEntity,
    ATTR_BRIGHTNESS,
    SUPPORT_BRIGHTNESS,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BRIGHTNESS_SCALE = 255 / 100  # Prozent ↔ HA-Skala
MIN_PERCENT = 10              # alles < 10 % = AUS


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Wibutler lights from a config entry."""
    hub = hass.data[DOMAIN]["hub"]
    devices = hub.devices

    lights = []

    for device_id, device in devices.items():
        dev_type = device.get("type")
        name = device.get("name") or ""
        name_low = name.lower()
        components = device.get("components", [])
        comp_names = {c.get("name") for c in components}

        # Debug ins Log, damit man sieht, was ankommt
        _LOGGER.debug(
            "WiButler-Gerät: id=%s name=%s type=%s components=%s",
            device_id,
            name,
            dev_type,
            comp_names,
        )

        # 1) Taster und Reconnect-Geräte NICHT als Light behandeln
        if "taster" in name_low or "reconnect" in name_low:
            _LOGGER.debug(
                "WiButler: Gerät %s als Light übersprungen (Name-Filter).", name
            )
            continue

        # 2) bekannte Typen + generische Erkennung:
        # alles, was einen SWT- und/oder BRI_LVL-Kanal hat,
        # wird als Light behandelt
        if dev_type in ("DimminActuators", "DimmingActuators") or \
           "BRI_LVL" in comp_names or "SWT" in comp_names:
            lights.append(WibutlerLight(hub, device))

    async_add_entities(lights, True)


class WibutlerLight(LightEntity):
    """Representation of a Wibutler dimmable light."""

    def __init__(self, hub, device):
        self._hub = hub
        self._device = device
        self._device_id = device["id"]
        self._attr_name = device["name"]
        self._attr_unique_id = f"{device['id']}_{device['name']}"
        self._is_on = False
        self._brightness_pct = 0
        self._last_brightness_pct = 100
        self._fetch_state(device.get("components", []))

    # --- Eigenschaften ---
    @property
    def supported_features(self):
        return SUPPORT_BRIGHTNESS

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        if self._brightness_pct < MIN_PERCENT:
            return 0
        return int(self._brightness_pct * BRIGHTNESS_SCALE)

    # --- Schalten ---
    async def async_turn_on(self, **kwargs):
        brightness_pct = self._last_brightness_pct

        if ATTR_BRIGHTNESS in kwargs:
            brightness_ha = kwargs[ATTR_BRIGHTNESS]
            brightness_pct = int(brightness_ha / BRIGHTNESS_SCALE)
            brightness_pct = max(0, min(100, brightness_pct))

        # wenn kleiner als MIN_PERCENT → direkt ausschalten
        if brightness_pct < MIN_PERCENT:
            await self.async_turn_off()
            return

        # SWT → ON
        data_swt = {"value": "ON", "type": "switch"}
        url_swt = f"devices/{self._device_id}/components/SWT"
        resp_swt = await self._hub._request("PATCH", url_swt, data_swt)

        # BRI_LVL → Prozent mit type "numeric"
        data_bri = {"type": "numeric", "value": str(brightness_pct)}
        url_bri = f"devices/{self._device_id}/components/BRI_LVL"
        resp_bri = await self._hub._request("PATCH", url_bri, data_bri)

        if resp_swt and resp_bri:
            self._is_on = True
            self._brightness_pct = brightness_pct
            self._last_brightness_pct = brightness_pct
            _LOGGER.info("💡 Light %s auf %d %% gesetzt", self._attr_name, brightness_pct)
            self.async_write_ha_state()
        else:
            _LOGGER.error("❌ Fehler beim Einschalten/Dimmen von %s", self._attr_name)

    async def async_turn_off(self, **kwargs):
        if self._brightness_pct >= MIN_PERCENT:
            self._last_brightness_pct = self._brightness_pct

        data = {"value": "OFF", "type": "switch"}
        url = f"devices/{self._device_id}/components/SWT"

        response = await self._hub._request("PATCH", url, data)

        if response:
            _LOGGER.info(
                "💡 Light %s ausgeschaltet (letzte Helligkeit %d %%)",
                self._attr_name,
                self._last_brightness_pct,
            )
            self._is_on = False
            self._brightness_pct = 0
            self.async_write_ha_state()
        else:
            _LOGGER.error("❌ Fehler beim Ausschalten von %s", self._attr_name)

    # --- Initialer & WS-Status ---
    def _fetch_state(self, components):
        for component in components:
            if component.get("name") == "STATE":
                value = component.get("value")
                self._is_on = value != "0"

            if component.get("name") == "BRI_LVL":
                value = component.get("value")
                try:
                    pct = int(value)
                    if pct < MIN_PERCENT:
                        self._brightness_pct = 0
                        self._is_on = False
                    else:
                        self._brightness_pct = pct
                        self._last_brightness_pct = pct
                except (TypeError, ValueError):
                    self._brightness_pct = 0
                    self._is_on = False

            if component.get("name") == "SWT":
                value = component.get("value")
                if value in ("0", "OFF"):
                    self._is_on = False
                elif self._brightness_pct >= MIN_PERCENT:
                    self._is_on = True

    async def async_added_to_hass(self):
        self._hub.register_listener(self)

    def handle_ws_update(self, device_id, components):
        self._fetch_state(components)
        self.async_write_ha_state()
