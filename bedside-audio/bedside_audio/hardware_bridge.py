from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from threading import RLock
from typing import Protocol

import requests
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import LED_THEME_PAYLOAD_LENGTH, Settings

logger = logging.getLogger(__name__)
_CORE_WS = "ws://supervisor/core/websocket"
_CORE_API = "http://supervisor/core/api"
_MAX_RECONNECT_BACKOFF_FAILURES = 8
_MAX_RECONNECT_SECONDS = 30.0
_MAX_DEDUPLICATION_KEYS = 256
_CUSTOM_LED_OPTIONS = frozenset({"off", "playing", "paused", "sleeping"})
_PLAYER_ACTIONS = frozenset({
    "media_play_pause",
    "media_next_track",
    "media_previous_track",
    "media_seek",
})
_LED_THEME_PAYLOAD = re.compile(
    r"v1\|#[0-9A-F]{6}@[0-9]{3}"
    r"\|#[0-9A-F]{6}@[0-9]{3}"
    r"\|#[0-9A-F]{6}@[0-9]{3}"
    r"\|#[0-9A-F]{6}@[0-9]{3}",
)


class BridgeProtocolError(RuntimeError):
    pass


class CoreActionError(RuntimeError):
    pass


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    def __aiter__(self): ...


class RestActions(Protocol):
    async def control_player(
        self,
        entity_id: str,
        action: str,
        *,
        seek_position: float | None = None,
    ) -> None: ...

    async def correct_volume(self, entity_id: str, volume_level: float) -> None: ...

    async def set_light(self, entity_id: str, display: str) -> None: ...

    async def set_select(self, entity_id: str, option: str) -> None: ...

    async def set_number(self, entity_id: str, value: float) -> None: ...

    async def set_text(self, entity_id: str, value: str) -> None: ...

    async def close(self) -> None: ...


class CoreRestActions:
    def __init__(
        self,
        token: str,
        settings: Settings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("Home Assistant Supervisor token is unavailable")
        self._settings = settings
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _post(self, domain: str, service: str, body: dict[str, object]) -> None:
        entity_id = body.get("entity_id")
        hardware = self._settings.hardware
        allowed = {
            *(
                ("media_player", action, self._settings.music_assistant_player_entity)
                for action in _PLAYER_ACTIONS
            ),
            (
                "media_player",
                "volume_set",
                self._settings.voice_media_player_entity,
            ),
            ("light", "turn_on", hardware.led_light_entity),
            ("light", "turn_off", hardware.led_light_entity),
            ("select", "select_option", hardware.led_select_entity),
            ("text", "set_value", hardware.led_theme_text_entity),
            ("number", "set_value", hardware.volume_cap_number_entity),
        }
        if (domain, service, entity_id) not in allowed or not entity_id:
            raise ValueError("Home Assistant Core action is not allowlisted")
        try:
            response = self._session.post(
                f"{_CORE_API}/services/{domain}/{service}",
                json=body,
                timeout=(2.0, 5.0),
                allow_redirects=False,
            )
        except (requests.RequestException, OSError) as exc:
            raise CoreActionError(
                f"Home Assistant Core action failed ({type(exc).__name__})"
            ) from None
        try:
            if not 200 <= response.status_code < 300:
                raise CoreActionError(
                    "Home Assistant Core rejected an allowlisted action "
                    f"(HTTP {response.status_code})"
                )
            try:
                payload = response.json()
            except ValueError:
                raise CoreActionError(
                    "Home Assistant Core returned an invalid action response"
                ) from None
            if not isinstance(payload, list):
                raise CoreActionError("Home Assistant Core did not confirm the action")
        finally:
            response.close()

    async def control_player(
        self,
        entity_id: str,
        action: str,
        *,
        seek_position: float | None = None,
    ) -> None:
        if (
            entity_id != self._settings.music_assistant_player_entity
            or action not in _PLAYER_ACTIONS
            or (action == "media_seek") != (seek_position is not None)
        ):
            raise ValueError("Music Assistant player action is not allowlisted")
        body: dict[str, object] = {"entity_id": entity_id}
        if seek_position is not None:
            if not isinstance(seek_position, float) or seek_position < 0:
                raise ValueError("Music Assistant seek position is invalid")
            body["seek_position"] = seek_position
        await asyncio.to_thread(self._post, "media_player", action, body)

    async def correct_volume(self, entity_id: str, volume_level: float) -> None:
        if (
            entity_id != self._settings.voice_media_player_entity
            or not 0.0 <= volume_level <= 0.5
        ):
            raise ValueError("Voice volume correction is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "media_player",
            "volume_set",
            {"entity_id": entity_id, "volume_level": volume_level},
        )

    async def set_light(self, entity_id: str, display: str) -> None:
        hardware = self._settings.hardware
        if entity_id != hardware.led_light_entity:
            raise ValueError("Voice LED light is not allowlisted")
        if display == "off":
            await asyncio.to_thread(
                self._post, "light", "turn_off", {"entity_id": entity_id},
            )
            return
        style = hardware.led_theme.display_style(display)
        await asyncio.to_thread(
            self._post,
            "light",
            "turn_on",
            {
                "entity_id": entity_id,
                "rgb_color": style.rgb_color,
                "brightness_pct": style.brightness,
            },
        )

    async def set_select(self, entity_id: str, option: str) -> None:
        if (
            entity_id != self._settings.hardware.led_select_entity
            or option not in _CUSTOM_LED_OPTIONS
        ):
            raise ValueError("Voice LED select option is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
        )

    async def set_number(self, entity_id: str, value: float) -> None:
        if (
            entity_id != self._settings.hardware.volume_cap_number_entity
            or not isinstance(value, float)
            or not 0.0 <= value <= 0.5
        ):
            raise ValueError("Voice volume cap number is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
        )

    async def set_text(self, entity_id: str, value: str) -> None:
        if (
            entity_id != self._settings.hardware.led_theme_text_entity
            or value != self._settings.hardware.led_theme.payload
            or len(value) != LED_THEME_PAYLOAD_LENGTH
            or not _LED_THEME_PAYLOAD.fullmatch(value)
        ):
            raise ValueError("Voice LED theme text is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "text",
            "set_value",
            {"entity_id": entity_id, "value": value},
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._session.close)


class HardwareIntentProcessor:
    def __init__(
        self,
        settings: Settings,
        rest: RestActions,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._hardware = settings.hardware
        self._rest = rest
        self._clock = clock
        self._playback_state = "unavailable"
        self._playback_available = False
        self._voice_available = False
        self._position_base: float | None = None
        self._position_started_at: float | None = None
        self._assist_idle = False
        self._assist_priority = True
        self._last_volume_level: float | None = None
        self._last_led_display: str | None = None
        self._led_needs_reapply = True
        self._local_led_priority = False
        self._theme_sync_pending = False
        self._seen: OrderedDict[tuple[object, ...], None] = OrderedDict()

    @property
    def entities(self) -> tuple[str, ...]:
        values = (
            self._settings.music_assistant_player_entity,
            self._settings.voice_media_player_entity,
            self._hardware.button_event_entity,
            self._hardware.assist_satellite_entity,
            self._hardware.led_entity,
            self._hardware.led_theme_text_entity,
            self._hardware.volume_cap_number_entity,
        )
        return tuple(dict.fromkeys(value for value in values if value))

    async def reconcile_snapshot(self, states: Iterable[object]) -> None:
        self._theme_sync_pending = False
        exact: dict[str, dict[str, object]] = {}
        for value in states:
            if not isinstance(value, dict):
                continue
            entity_id = value.get("entity_id")
            if entity_id in self.entities:
                exact[str(entity_id)] = value
        for entity_id in self.entities:
            value = exact.get(entity_id)
            if value is None:
                continue
            state = value.get("state")
            attributes = value.get("attributes")
            if not isinstance(state, str) or not isinstance(attributes, dict):
                continue
            await self.process_state(
                entity_id,
                state,
                attributes,
                live=False,
                event_key=value.get("last_updated"),
            )
        await self._reconcile_led()

    async def process_state(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, object],
        *,
        live: bool,
        event_key: object,
    ) -> None:
        if entity_id not in self.entities:
            raise ValueError("Core event entity is not configured")
        event_type = attributes.get("event_type")
        key = (entity_id, state, event_type, event_key)
        if live and self._duplicate(key):
            return

        if entity_id == self._settings.music_assistant_player_entity:
            self._observe_playback(state, attributes)
        if entity_id == self._settings.voice_media_player_entity:
            self._voice_available = state not in ("off", "unknown", "unavailable")
            level = attributes.get("volume_level")
            if self._voice_available and isinstance(level, (int, float)):
                await self._apply_volume(float(level))
        if entity_id == self._hardware.assist_satellite_entity:
            was_priority = self._assist_priority
            muted = (
                attributes.get("microphone_muted") is True
                or attributes.get("muted") is True
            )
            self._assist_idle = state == "idle"
            self._assist_priority = not self._assist_idle or muted
            if self._assist_idle and not muted:
                self._local_led_priority = False
            if was_priority or self._assist_priority:
                self._led_needs_reapply = True
        elif entity_id == self._hardware.button_event_entity and live:
            if isinstance(event_type, str):
                await self._apply_gesture(event_type)
        elif entity_id == self._hardware.led_entity and live:
            if not self._led_event_matches(state, attributes):
                self._local_led_priority = True
                self._led_needs_reapply = True
            return
        elif entity_id == self._hardware.led_theme_text_entity:
            await self._sync_led_theme(state, attributes)
        elif entity_id == self._hardware.volume_cap_number_entity:
            await self._sync_volume_cap_number(state, attributes)
        await self._reconcile_led()

    def _observe_playback(
        self, state: str, attributes: dict[str, object],
    ) -> None:
        previous_state = self._playback_state
        self._playback_state = state
        self._playback_available = state not in ("off", "unknown", "unavailable")
        raw_position = attributes.get("media_position")
        position = (
            float(raw_position)
            if isinstance(raw_position, (int, float)) and raw_position >= 0
            else None
        )
        if state == "playing":
            if (
                previous_state != "playing"
                or self._position_base is None
                or position is not None and abs(position - self._position_base) > 0.5
            ):
                self._position_base = position
                self._position_started_at = (
                    self._clock() if position is not None else None
                )
        elif state == "paused":
            if position is None:
                position = self._estimated_position()
            self._position_base = position
            self._position_started_at = None
        else:
            self._position_base = None
            self._position_started_at = None

    def _estimated_position(self) -> float | None:
        if self._position_base is None:
            return None
        if self._position_started_at is None:
            return self._position_base
        return self._position_base + max(0.0, self._clock() - self._position_started_at)

    def _duplicate(self, key: tuple[object, ...]) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        if len(self._seen) > _MAX_DEDUPLICATION_KEYS:
            self._seen.popitem(last=False)
        return False

    async def _apply_volume(self, level: float) -> None:
        if not 0.0 <= level <= 1.0 or level == self._last_volume_level:
            return
        cap = self._settings.max_volume / 100
        if level > cap:
            await self._rest.correct_volume(
                self._settings.voice_media_player_entity, cap,
            )
        self._last_volume_level = level

    async def _sync_volume_cap_number(
        self, state: str, attributes: dict[str, object],
    ) -> None:
        try:
            current = float(state)
        except ValueError:
            raise ValueError("Voice volume cap number returned an invalid state") from None
        if (
            attributes.get("min") != 0.0
            or attributes.get("max") != 0.5
            or attributes.get("step") != 0.05
        ):
            raise ValueError("Voice volume cap number contract is invalid")
        target = self._settings.max_volume / 100
        if abs(current - target) < 0.0001:
            return
        await self._rest.set_number(
            self._hardware.volume_cap_number_entity, target,
        )

    async def _sync_led_theme(
        self, state: str, attributes: dict[str, object],
    ) -> None:
        if (
            attributes.get("min") != LED_THEME_PAYLOAD_LENGTH
            or attributes.get("max") != LED_THEME_PAYLOAD_LENGTH
            or attributes.get("mode") != "text"
        ):
            raise ValueError("Voice LED theme text contract is invalid")
        target = self._hardware.led_theme.payload
        if state == target:
            self._theme_sync_pending = False
            return
        if self._theme_sync_pending:
            return
        try:
            await self._rest.set_text(
                self._hardware.led_theme_text_entity, target,
            )
        except (CoreActionError, OSError) as exc:
            logger.warning("Voice LED theme synchronization failed: %s", exc)
            return
        self._theme_sync_pending = True

    async def _apply_gesture(self, event_type: str) -> None:
        if event_type not in ("single_press", "double_press", "triple_press"):
            return
        if (
            self._assist_priority
            or not self._playback_available
            or self._playback_state not in ("playing", "paused")
        ):
            return
        try:
            if event_type == "single_press":
                await self._rest.control_player(
                    self._settings.music_assistant_player_entity,
                    "media_play_pause",
                )
            elif event_type == "double_press":
                await self._rest.control_player(
                    self._settings.music_assistant_player_entity,
                    "media_next_track",
                )
            elif (self._estimated_position() or 0.0) > 10.0:
                await self._rest.control_player(
                    self._settings.music_assistant_player_entity,
                    "media_seek",
                    seek_position=0.0,
                )
            else:
                await self._rest.control_player(
                    self._settings.music_assistant_player_entity,
                    "media_previous_track",
                )
        except (CoreActionError, OSError) as exc:
            logger.warning("Voice hardware gesture was rejected: %s", exc)

    async def _reconcile_led(self) -> None:
        if self._assist_priority or self._local_led_priority:
            self._led_needs_reapply = True
            return
        if not self._voice_available:
            display = "off"
        elif self._playback_state == "paused":
            display = "paused"
        elif self._playback_state in ("playing", "buffering"):
            display = "playing"
        else:
            display = "sleeping"
        if display == self._last_led_display and not self._led_needs_reapply:
            return
        if self._hardware.led_light_entity:
            await self._rest.set_light(self._hardware.led_light_entity, display)
        else:
            await self._rest.set_select(self._hardware.led_select_entity, display)
        self._last_led_display = display
        self._led_needs_reapply = False

    def _led_event_matches(
        self, state: str, attributes: dict[str, object],
    ) -> bool:
        display = self._last_led_display
        if display is None:
            return False
        if self._hardware.led_select_entity:
            return state == display
        if display == "off":
            return state == "off"
        try:
            style = self._hardware.led_theme.display_style(display)
        except ValueError:
            return False
        color = attributes.get("rgb_color")
        brightness = attributes.get("brightness")
        return (
            state == "on"
            and isinstance(color, (list, tuple))
            and list(color) == style.rgb_color
            and brightness == style.ha_brightness
        )


class CoreHardwareBridge:
    def __init__(
        self,
        settings: Settings,
        *,
        token: str,
        rest: RestActions | None = None,
        connector: (
            Callable[[], AbstractAsyncContextManager[WebSocketConnection]] | None
        ) = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._token = token
        self._rest = rest or CoreRestActions(token, settings)
        self._processor = HardwareIntentProcessor(settings, self._rest)
        self._connector = connector or self._default_connector
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._lock = RLock()
        self._status: dict[str, object] = {
            "running": False,
            "connected": False,
            "reconnect_failures": 0,
            "error": None,
        }

    @classmethod
    def from_env(cls, settings: Settings) -> CoreHardwareBridge:
        return cls(
            settings,
            token=os.environ.get("SUPERVISOR_TOKEN", ""),
        )

    def _default_connector(
        self,
    ) -> AbstractAsyncContextManager[WebSocketConnection]:
        return connect(
            _CORE_WS,
            open_timeout=5,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=20,
            max_size=1024 * 1024,
        )

    def _set_status(self, **changes: object) -> None:
        with self._lock:
            self._status.update(changes)

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._status)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._set_status(running=True)
        self._task = asyncio.create_task(
            self._run(), name="bedside-core-hardware-bridge",
        )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except (
                BridgeProtocolError,
                CoreActionError,
                ConnectionClosed,
                WebSocketException,
                OSError,
                TimeoutError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                logger.warning("Voice hardware bridge stopped with an error: %s", exc)
        try:
            await self._rest.close()
        except (CoreActionError, OSError) as exc:
            logger.warning("Voice hardware bridge REST shutdown failed: %s", exc)
        self._set_status(running=False, connected=False)

    async def _run(self) -> None:
        failures = 0
        try:
            while True:
                try:
                    await self.run_once()
                    failures = 0
                    raise BridgeProtocolError("Core WebSocket closed")
                except asyncio.CancelledError:
                    raise
                except (
                    BridgeProtocolError,
                    CoreActionError,
                    ConnectionClosed,
                    WebSocketException,
                    OSError,
                    TimeoutError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    failures += 1
                    backoff_step = min(failures, _MAX_RECONNECT_BACKOFF_FAILURES)
                    delay = min(2 ** (backoff_step - 1), _MAX_RECONNECT_SECONDS)
                    self._set_status(
                        connected=False,
                        reconnect_failures=failures,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    logger.warning(
                        "Home Assistant Core hardware bridge disconnected "
                        "(failure %s, retrying in %ss): %s",
                        failures,
                        delay,
                        exc,
                    )
                    await self._sleep(delay)
        finally:
            self._set_status(running=False, connected=False)

    async def run_once(self) -> None:
        if not self._token:
            raise ValueError("Home Assistant Supervisor token is unavailable")
        self._set_status(connected=False)
        async with self._connector() as websocket:
            required = await self._receive(websocket)
            if required.get("type") != "auth_required":
                raise BridgeProtocolError("Core WebSocket did not request authentication")
            await websocket.send(json.dumps({
                "type": "auth",
                "access_token": self._token,
            }))
            authenticated = await self._receive(websocket)
            if authenticated.get("type") != "auth_ok":
                raise BridgeProtocolError("Core WebSocket authentication failed")

            command_id = 0
            pending_events: list[dict[str, object]] = []
            for entity_id in self._processor.entities:
                command_id += 1
                await self._request(
                    websocket,
                    {
                        "id": command_id,
                        "type": "subscribe_trigger",
                        "trigger": {
                            "platform": "state",
                            "entity_id": entity_id,
                        },
                    },
                    pending_events=pending_events,
                )
            command_id += 1
            snapshot = await self._request(
                websocket,
                {"id": command_id, "type": "get_states"},
                pending_events=pending_events,
            )
            if not isinstance(snapshot, list):
                raise BridgeProtocolError("Core WebSocket returned an invalid snapshot")
            await self._processor.reconcile_snapshot(snapshot)
            for message in pending_events:
                await self._process_event(message)
            self._set_status(
                connected=True, reconnect_failures=0, error=None,
            )
            await asyncio.sleep(0)

            async for raw in websocket:
                message = self._decode(raw)
                if message.get("type") == "event":
                    await self._process_event(message)
        self._set_status(connected=False)

    async def _process_event(self, message: dict[str, object]) -> None:
        state = self._event_state(message)
        if state is None:
            return
        await self._processor.process_state(
            state["entity_id"],
            state["state"],
            state["attributes"],
            live=True,
            event_key=state["event_key"],
        )

    async def _request(
        self,
        websocket: WebSocketConnection,
        command: dict[str, object],
        *,
        pending_events: list[dict[str, object]],
    ) -> object:
        await websocket.send(json.dumps(command))
        expected_id = command["id"]
        while True:
            message = await self._receive(websocket)
            if message.get("type") == "event":
                pending_events.append(message)
                continue
            if message.get("id") != expected_id:
                raise BridgeProtocolError("Core WebSocket response ID was unexpected")
            if message.get("type") != "result" or message.get("success") is not True:
                raise BridgeProtocolError("Core WebSocket command failed")
            return message.get("result")

    async def _receive(
        self, websocket: WebSocketConnection,
    ) -> dict[str, object]:
        return self._decode(await websocket.recv())

    @staticmethod
    def _decode(raw: object) -> dict[str, object]:
        if not isinstance(raw, str):
            raise BridgeProtocolError("Core WebSocket returned a non-text message")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise BridgeProtocolError("Core WebSocket returned invalid JSON")
        return value

    @staticmethod
    def _event_state(message: dict[str, object]) -> dict[str, object] | None:
        event = message.get("event")
        if not isinstance(event, dict):
            return None
        variables = event.get("variables")
        trigger = variables.get("trigger") if isinstance(variables, dict) else None
        to_state = trigger.get("to_state") if isinstance(trigger, dict) else None
        if not isinstance(to_state, dict):
            data = event.get("data")
            to_state = data.get("new_state") if isinstance(data, dict) else None
        if not isinstance(to_state, dict):
            return None
        entity_id = to_state.get("entity_id")
        state = to_state.get("state")
        attributes = to_state.get("attributes")
        if (
            not isinstance(entity_id, str)
            or not isinstance(state, str)
            or not isinstance(attributes, dict)
        ):
            return None
        context = to_state.get("context")
        context_id = context.get("id") if isinstance(context, dict) else None
        return {
            "entity_id": entity_id,
            "state": state,
            "attributes": attributes,
            "event_key": context_id or to_state.get("last_updated"),
        }
