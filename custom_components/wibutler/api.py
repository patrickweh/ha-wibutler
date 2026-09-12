"""API client for Wibutler hub."""

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

_LOGGER = logging.getLogger(__name__)

WEBSOCKET_RETRY_BASE = 5
WEBSOCKET_RETRY_MAX = 300


class WibutlerHub:
    """Manages communication with the Wibutler API including WebSockets."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool = False,
        use_ssl: bool = False,
    ) -> None:
        """Initialize Wibutler API connection."""
        self.hass = hass
        self.host = host
        self.port = port
        self.verify_ssl = verify_ssl
        self.use_ssl = use_ssl
        self.username = username
        self.password = password
        self.token: str | None = None
        self.listeners: list = []
        self.devices: dict[str, Any] = {}
        self.available = True
        self._stop_event = asyncio.Event()

        self.schema = "https" if self.use_ssl else "http"

        parsed = urlparse(self.host)
        self.base_host = parsed.hostname if parsed.scheme else self.host

        self.session = async_create_clientsession(
            hass, verify_ssl=self.verify_ssl
        )

    async def authenticate(self) -> bool:
        """Authenticate with the Wibutler API and store the token."""
        url = f"{self.schema}://{self.base_host}:{self.port}/api/login"
        payload = {"username": self.username, "password": self.password}

        try:
            async with self.session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    # ponytail: hub answers 200 with an empty body when its login service hangs
                    self.token = data.get("sessionToken") if data else None
                    if not self.token:
                        _LOGGER.error("API response contains no token: %r", data)
                        return False
                    _LOGGER.debug("Successfully authenticated with Wibutler")
                    self._set_available(True)
                    return True
                _LOGGER.error(
                    "Authentication failed (%s): %s",
                    response.status,
                    await response.text(),
                )
        except aiohttp.ClientError as err:
            _LOGGER.error("Connection error with Wibutler API: %s", err)
            self._set_available(False)
        return False

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        *,
        _retry: bool = True,
    ) -> dict[str, Any] | None:
        """Send a request to the Wibutler API."""
        if not self.token:
            if not await self.authenticate():
                return None

        url = f"{self.schema}://{self.base_host}:{self.port}/api/{endpoint}"
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.request(
                method, url, headers=headers, json=data
            ) as response:
                if response.status in (200, 201):
                    self._set_available(True)
                    ctype = response.headers.get("Content-Type", "")
                    if "application/json" in ctype:
                        return await response.json()
                    text = await response.text()
                    return {"raw": text}
                if response.status == 401 and _retry:
                    _LOGGER.warning("Token expired, re-authenticating")
                    self.token = None
                    if await self.authenticate():
                        return await self._request(
                            method, endpoint, data, _retry=False
                        )
                    return None
                _LOGGER.error(
                    "API error (%s): %s",
                    response.status,
                    await response.text(),
                )
        except aiohttp.ClientError as err:
            _LOGGER.error("API request error: %s", err)
            self._set_available(False)
        return None

    async def get_devices(self) -> dict[str, Any]:
        """Fetch the device list from the Wibutler API."""
        response = await self._request("GET", "devices")
        if isinstance(response, dict):
            return response.get("devices", {})
        _LOGGER.error("Expected dict response, got: %s", type(response))
        return {}

    async def connect_websocket(self) -> None:
        """Connect to the WebSocket with auto-reconnect."""
        retry_delay = WEBSOCKET_RETRY_BASE

        while not self._stop_event.is_set():
            if not self.token and not await self.authenticate():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=retry_delay
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                continue

            ws_protocol = "wss" if self.schema == "https" else "ws"
            ws_url = (
                f"{ws_protocol}://{self.base_host}:{self.port}"
                f"/api/stream/{self.token}"
            )
            _LOGGER.debug("Connecting to WebSocket: %s", ws_url)

            try:
                async with self.session.ws_connect(ws_url) as ws:
                    retry_delay = WEBSOCKET_RETRY_BASE
                    self._set_available(True)
                    _LOGGER.debug("WebSocket connected")
                    await self._refresh_all_states()

                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if (
                                    "data" in data
                                    and "components" in data["data"]
                                ):
                                    self._handle_ws_message(
                                        data["data"]["id"],
                                        data["data"]["components"],
                                    )
                            except json.JSONDecodeError:
                                _LOGGER.error(
                                    "Error parsing WebSocket message: %s",
                                    msg.data,
                                )
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.error(
                                "WebSocket error: %s", ws.exception()
                            )
                            break
            except aiohttp.ClientError as err:
                _LOGGER.warning(
                    "WebSocket disconnected, reconnecting in %ss: %s",
                    retry_delay,
                    err,
                )
                self._set_available(False)

            if self._stop_event.is_set():
                break

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=retry_delay
                )
            except asyncio.TimeoutError:
                pass
            retry_delay = min(retry_delay * 2, WEBSOCKET_RETRY_MAX)

    async def _refresh_all_states(self) -> None:
        """Fetch all device states via REST and dispatch to listeners."""
        _LOGGER.debug("Refreshing all device states after reconnect")
        devices = await self.get_devices()
        for device_id, device in devices.items():
            components = device.get("components", [])
            if not components:
                continue
            for listener in self.listeners:
                if getattr(listener, "_device_id", None) == device_id:
                    listener.handle_ws_update(device_id, components)

    def _handle_ws_message(
        self, device_id: str, components: list[dict[str, Any]]
    ) -> None:
        """Process WebSocket messages and notify relevant entities."""
        for listener in self.listeners:
            if getattr(listener, "_device_id", None) == device_id:
                listener.handle_ws_update(device_id, components)

    def _set_available(self, available: bool) -> None:
        """Update availability and notify all listeners."""
        if self.available == available:
            return
        self.available = available
        for listener in self.listeners:
            if hasattr(listener, "async_write_ha_state"):
                listener.async_write_ha_state()

    def register_listener(self, entity: Any) -> None:
        """Register an entity for WebSocket updates."""
        self.listeners.append(entity)

    def remove_listener(self, entity: Any) -> None:
        """Remove an entity from WebSocket updates."""
        try:
            self.listeners.remove(entity)
        except ValueError:
            pass

    async def close(self) -> None:
        """Signal WebSocket to stop."""
        self._stop_event.set()
