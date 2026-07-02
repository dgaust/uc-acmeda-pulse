"""
Purpose-built async client for the Rollease Acmeda Pulse Hub v2.

Written specifically for this integration instead of depending on aiopulse2,
so the driver has full control over the behaviours that caused trouble:

- **The websocket is the single source of truth for state and control.** It
  connects to ``wss://<host>:443/rpc`` and polls ``shadow`` every few seconds;
  every response carries the full roller state. Commands (move / stop) are
  sent over this same socket. Connectivity (``connected``) reflects this
  socket only.
- **Roller names are fetched out-of-band and never gate anything.** The hub
  does not send names over the websocket (the official app fetches them from
  the cloud); the only local source is the port-1487 "serial" channel, which
  allows a single connection at a time and can fail. Name resolution runs as a
  best-effort background task - if it fails, state and control are unaffected
  and rollers simply keep their id as a name until the next attempt.
- **Robust reconnection.** The run loop reconnects automatically; ``connected``
  and the update callbacks track the real socket state.

The public surface (``PulseHub``, ``Roller``, ``MovingAction``,
``NotRunningException``) mirrors the small slice of aiopulse2 this project used,
so it is a drop-in replacement.

Protocol reference: https://github.com/sillyfrog/aiopulse2/wiki

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging
import re
import ssl
import time
from enum import Enum
from typing import Any, Callable

import websockets

_LOG = logging.getLogger(__name__)

WS_PORT = 443
SERIAL_PORT = 1487
# Idle poll interval (matches the official app's ~3s status polling), and the
# faster interval used while any roller is moving so the cover position updates
# smoothly in the UI.
POLL_INTERVAL_S = 3.0
POLL_INTERVAL_MOVING_S = 0.5
RECONNECT_DELAY_S = 10.0
WS_OPEN_TIMEOUT_S = 10.0
NAME_FETCH_ATTEMPTS = 5
NAME_FETCH_RETRY_DELAY_S = 8.0
SERIAL_READ_TIMEOUT_S = 3.0

# Battery voltage string, e.g. "11.2D24" -> voltage 11.2, type D, version 24.
_VOLTAGE_RE = re.compile(r"(?P<voltage>[.0-9]+)(?P<type>[A-Za-z])(?P<version>\d{2})")
# Serial name response, e.g. "!S1SNAMEWindow;".
_NAME_RE = re.compile(r"!(?P<id>\w{3})NAME(?P<name>.+);")

# Device type letters that indicate a battery-powered motor.
_BATTERY_TYPES = ("D", "U", "d")
# Below this voltage the smaller 8.3v battery formula is used.
_BATTERY_8V_MAX_VOLTAGE = 9.0

_DEVICE_TYPES = {
    "A": "AC motor",
    "B": "Hub/Gateway",
    "C": "Curtain motor",
    "D": "DC motor",
    "d": "DC motor (lower)",
    "U": "DC motor (U)",
    "S": "Socket",
    "L": "Lighting device",
}


class MovingAction(Enum):
    """Direction a roller is currently moving (best guess when not commanded)."""

    stopped = 0
    up = 1
    down = 2


class PulseHubError(Exception):
    """Base exception for the Pulse hub client."""


class NotConnectedException(PulseHubError):
    """Raised when a websocket send is attempted while disconnected."""


class NotRunningException(PulseHubError):
    """Raised when a command is attempted while the hub isn't running/connected."""


def _ssl_context() -> ssl.SSLContext:
    # The hub presents a self-signed certificate - accept it.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Roller:
    """A single roller/blind/shade behind the hub."""

    def __init__(self, hub: "PulseHub", roller_id: str):
        self.hub = hub
        self.id = roller_id
        self.name: str | None = None
        self.closed_percent: int | None = None
        self.tilt_percent: int | None = None
        self.online: bool = False
        self.signal: int | None = None
        self.battery: float | None = None
        self.devicetypeshort: str | None = None
        self.devicetype: str | None = None
        self.version: str | None = None
        self.target_closed_percent: int | None = None
        self._moving: bool = False
        self.action: MovingAction = MovingAction.stopped
        self._callbacks: list[Callable] = []

    def __repr__(self) -> str:
        return (
            f"Roller(id={self.id!r}, name={self.name!r}, "
            f"closed_percent={self.closed_percent}, online={self.online})"
        )

    # -- properties ---------------------------------------------------------

    @property
    def has_battery(self) -> bool:
        """True if this device appears to be battery powered."""
        return self.devicetypeshort in _BATTERY_TYPES

    @property
    def battery_percent(self) -> int | None:
        """Rough battery percentage, or None if not a battery device."""
        if not self.has_battery or not self.battery:
            return None
        if self.battery < _BATTERY_8V_MAX_VOLTAGE:
            percent = int(42.8 * self.battery - 255)
        else:
            percent = int(27.4 * self.battery - 255)
        return max(0, min(100, percent))

    @property
    def moving(self) -> bool:
        return self._moving

    @moving.setter
    def moving(self, value: bool) -> None:
        self._moving = value
        if value:
            if self.action == MovingAction.stopped:
                # Guess direction from position when the hub reports movement
                # we didn't initiate.
                if _to_int(self.closed_percent) > 50:
                    self.action = MovingAction.up
                else:
                    self.action = MovingAction.down
        else:
            self.action = MovingAction.stopped
            self.target_closed_percent = self.closed_percent

    # -- callbacks ----------------------------------------------------------

    def callback_subscribe(self, callback: Callable) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def callback_unsubscribe(self, callback: Callable) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def notify_callback(self) -> None:
        for callback in self._callbacks:
            self.hub._schedule(callback, self)

    # -- commands -----------------------------------------------------------

    async def move_to(self, percent: int) -> None:
        """Move to a closed-percentage (0 = fully open, 100 = fully closed)."""
        percent = max(0, min(100, int(percent)))
        current = self.closed_percent if self.closed_percent is not None else 50
        if percent > current:
            self.action = MovingAction.down
        elif percent < current:
            self.action = MovingAction.up
        else:
            self.action = MovingAction.stopped
        self._moving = percent != current
        self.target_closed_percent = percent
        self.notify_callback()
        await self.hub._send_shade_command(self.id, {"movePercent": percent})

    async def move_up(self) -> None:
        """Fully open."""
        await self.move_to(0)

    async def move_down(self) -> None:
        """Fully close."""
        await self.move_to(100)

    async def move_stop(self) -> None:
        """Stop any current movement."""
        await self.hub._send_shade_command(self.id, {"stopShade": True})


class PulseHub:
    """Representation of - and connection to - an Acmeda Pulse v2 hub."""

    # delay_callbacks accepted for drop-in compatibility; intentionally unused
    # (this client never delays callbacks - that coupling was the whole problem).
    def __init__(self, host: str, delay_callbacks: bool = False):
        self.host = host
        self.connected = False
        self.running = False

        self.rollers: dict[str, Roller] = {}
        self.rollers_known = asyncio.Event()

        self.name: str | None = None
        self.id: str | None = None
        self.mac_address: str | None = None
        self.firmware_ver: str | None = None
        self.model: str | None = None

        self._loop = asyncio.get_event_loop()
        self._ssl = _ssl_context()
        self._wsuri = f"wss://{host}:{WS_PORT}/rpc"
        self._ws = None
        self._callbacks: list[Callable] = []
        self._run_task: asyncio.Task | None = None
        self._name_task: asyncio.Task | None = None
        # Set to wake the poller early (e.g. right after sending a command) so
        # fast position updates start immediately instead of after an idle wait.
        self._poll_wakeup = asyncio.Event()

    def __repr__(self) -> str:
        return (
            f"PulseHub(host={self.host!r}, name={self.name!r}, "
            f"connected={self.connected}, rollers={len(self.rollers)})"
        )

    # -- callbacks ----------------------------------------------------------

    def callback_subscribe(self, callback: Callable) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def callback_unsubscribe(self, callback: Callable) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_hub(self) -> None:
        for callback in self._callbacks:
            self._schedule(callback, self)

    def _schedule(self, callback: Callable, arg: Any) -> None:
        """Run a subscriber callback on the event loop (callbacks are coroutines)."""
        try:
            result = callback(arg)
            if asyncio.iscoroutine(result):
                self._loop.create_task(result)
        except Exception:  # pragma: no cover - defensive
            _LOG.exception("%s: error in update callback", self.host)

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Connect and keep the connection alive until stop() is called."""
        if self.running:
            _LOG.warning("%s: already running", self.host)
            return
        self.running = True
        _LOG.info("%s: starting", self.host)
        while self.running:
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - log & retry any ws error
                _LOG.debug("%s: connection error: %s", self.host, ex)
            self._ws = None
            if self.connected:
                self.connected = False
                self._notify_hub()
            if self.running:
                await asyncio.sleep(RECONNECT_DELAY_S)
        _LOG.info("%s: stopped", self.host)

    async def _connect_and_consume(self) -> None:
        async with websockets.connect(
            self._wsuri, ssl=self._ssl, open_timeout=WS_OPEN_TIMEOUT_S
        ) as ws:
            self._ws = ws
            _LOG.debug("%s: websocket connected", self.host)
            poller = self._loop.create_task(self._poller())
            try:
                async for message in ws:
                    self._handle_message(message)
            finally:
                poller.cancel()

    async def _poller(self) -> None:
        """
        Periodically request the full hub/roller state.

        Polls at ``POLL_INTERVAL_MOVING_S`` while any roller is moving so the
        cover position updates smoothly, and backs off to ``POLL_INTERVAL_S``
        when everything is stationary. A command wakes the poller immediately
        (via ``_poll_wakeup``) so fast updates begin the moment a blind is told
        to move, rather than up to one idle interval later.
        """
        self._poll_wakeup.clear()
        while True:
            try:
                await self._send({"method": "shadow", "src": "app", "id": int(time.time())})
            except NotConnectedException:
                return
            interval = POLL_INTERVAL_MOVING_S if self._any_moving() else POLL_INTERVAL_S
            try:
                await asyncio.wait_for(self._poll_wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._poll_wakeup.clear()

    def _any_moving(self) -> bool:
        return any(roller.moving for roller in self.rollers.values())

    async def stop(self) -> None:
        """Stop the run loop and close the connection."""
        if not self.running:
            return
        _LOG.debug("%s: stopping", self.host)
        self.running = False
        if self._name_task is not None:
            self._name_task.cancel()
            self._name_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover - defensive
                pass
        self.connected = False

    # -- sending ------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnectedException()
        await ws.send(json.dumps(payload))

    async def _send_shade_command(self, roller_id: str, command: dict) -> None:
        if not self.connected or self._ws is None:
            raise NotRunningException()
        await self._send(
            {
                "method": "shadow",
                "args": {
                    "desired": {"shades": {roller_id: command}},
                    "timeStamp": time.time(),
                },
            }
        )
        # Poll again straight away so movement is picked up without waiting out
        # the current idle interval.
        self._poll_wakeup.set()

    # -- receiving ----------------------------------------------------------

    def _handle_message(self, message: str) -> None:
        data = self._parse_json(message)
        if data is None:
            return
        reported = data.get("result", {}).get("reported")
        if reported is None:
            _LOG.debug("%s: ignoring message without reported data", self.host)
            return

        newly_connected = not self.connected
        if newly_connected:
            self.connected = True

        hub_changed = self._apply_hub_fields(reported)
        rollers_added = self._apply_shades(reported.get("shades", {}))

        if self.rollers and not self.rollers_known.is_set():
            self.rollers_known.set()
        if rollers_added:
            self._ensure_names()

        if newly_connected or hub_changed or rollers_added:
            self._notify_hub()

    @staticmethod
    def _parse_json(message: str) -> dict | None:
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            # Pulse Pro hubs (fw 1.1.0) can emit truncated JSON missing closing
            # braces - recover it the same way aiopulse2 does.
            missing = message.count("{") - message.count("}")
            if missing > 0:
                try:
                    return json.loads(message + "}" * missing)
                except json.JSONDecodeError:
                    pass
        _LOG.debug("Invalid JSON from hub: %.120s", message)
        return None

    def _apply_hub_fields(self, reported: dict) -> bool:
        model = reported.get("mfi", {}).get("model")
        changes = {
            "name": reported.get("name", self.name),
            "id": reported.get("hubId", self.id),
            "mac_address": reported.get("mac", self.mac_address),
            "firmware_ver": reported.get("firmware", {}).get("version", self.firmware_ver),
            "model": model if model else self.model,
        }
        changed = False
        for attr, value in changes.items():
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                changed = True
        return changed

    def _apply_shades(self, shades: dict) -> bool:
        rollers_added = False
        for roller_id, data in shades.items():
            roller = self.rollers.get(roller_id)
            if roller is None:
                roller = Roller(self, roller_id)
                self.rollers[roller_id] = roller
                rollers_added = True

            newvals: dict[str, Any] = {
                "online": bool(data.get("ol", False)),
                "closed_percent": _to_int(data.get("mp", 100), 100),
                "signal": data.get("rs"),
            }

            voltage_str = data.get("vo")
            if voltage_str:
                match = _VOLTAGE_RE.match(voltage_str)
                if match:
                    newvals["battery"] = float(match.group("voltage"))
                    newvals["devicetypeshort"] = match.group("type")
                    newvals["devicetype"] = _DEVICE_TYPES.get(
                        match.group("type"), f"unknown ({match.group('type')})"
                    )
                    newvals["version"] = match.group("version")

            changed = rollers_added
            for attr, value in newvals.items():
                if getattr(roller, attr) != value:
                    setattr(roller, attr, value)
                    changed = True

            # `is` = stationary; moving is its inverse. Apply via the setter so
            # the movement direction is updated too.
            moving = not data.get("is", True)
            if roller.moving != moving:
                roller.moving = moving
                changed = True

            if changed:
                roller.notify_callback()

        return rollers_added

    # -- names (best-effort, out-of-band) -----------------------------------

    def _ensure_names(self) -> None:
        """Start a background name fetch if any roller still lacks a name."""
        if self._name_task is not None and not self._name_task.done():
            return
        if all(r.name for r in self.rollers.values()):
            return
        self._name_task = self._loop.create_task(self._fetch_names())

    async def _fetch_names(self) -> None:
        for attempt in range(NAME_FETCH_ATTEMPTS):
            missing = [rid for rid, r in self.rollers.items() if r.name is None]
            if not missing:
                return
            try:
                await self._fetch_names_once(missing)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - name fetch is best-effort
                _LOG.debug("%s: name fetch attempt %d failed: %s", self.host, attempt, ex)
            if all(r.name for r in self.rollers.values()):
                return
            await asyncio.sleep(NAME_FETCH_RETRY_DELAY_S)

    async def _fetch_names_once(self, roller_ids: list[str]) -> None:
        _LOG.debug("%s: fetching names via serial for %s", self.host, roller_ids)
        reader, writer = await asyncio.open_connection(self.host, SERIAL_PORT)
        try:
            for roller_id in roller_ids:
                writer.write(f"!{roller_id}NAME?;".encode())
            await writer.drain()
            while True:
                try:
                    async with asyncio.timeout(SERIAL_READ_TIMEOUT_S):
                        chunk = await reader.readuntil(b";")
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                match = _NAME_RE.match(chunk.decode(errors="ignore"))
                if match:
                    roller = self.rollers.get(match.group("id"))
                    name = match.group("name")
                    if roller is not None and roller.name != name:
                        roller.name = name
                        roller.notify_callback()
                        self._notify_hub()
                if all(self.rollers[i].name for i in roller_ids if i in self.rollers):
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - defensive
                pass

    async def wait_for_names(self, timeout: float) -> None:
        """Best-effort wait until every known roller has a name (bounded)."""
        try:
            async with asyncio.timeout(timeout):
                while not self.rollers or not all(r.name for r in self.rollers.values()):
                    await asyncio.sleep(0.2)
        except asyncio.TimeoutError:
            _LOG.debug("%s: not all roller names resolved within %ss", self.host, timeout)
