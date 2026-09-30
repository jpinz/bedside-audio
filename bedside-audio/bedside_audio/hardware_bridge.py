from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from threading import RLock
from typing import AsyncContextManager, Protocol

import requests
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import LED_THEME_PAYLOAD_LENGTH, HardwareBridgeSettings
from .controller import ControlError, PlaybackController
from .persistence import StateError
from .player import PlayerError

logger = logging.getLogger(__name__)
_CORE_WS = "ws://supervisor/core/websocket"
_CORE_API = "http://supervisor/core/api"
_MAX_RECONNECT_FAILURES = 8
_MAX_RECONNECT_SECONDS = 30.0
_MAX_DEDUPLICATION_KEYS = 256
_CUSTOM_LED_OPTIONS = frozenset({"off", "playing", "paused", "sleeping"})
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
        *,
        voice_entity: str,
        light_entity: str = "",
        select_entity: str = "",
        text_entity: str = "",
        theme_payload: str = "",
        number_entity: str = "",
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("Home Assistant Supervisor token is unavailable")
        self._voice_entity = voice_entity
        self._light_entity = light_entity
        self._select_entity = select_entity
        self._text_entity = text_entity
        self._theme_payload = theme_payload
        self._number_entity = number_entity
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _post(self, domain: str, service: str, body: dict[str, object]) -> None:
        entity_id = body.get("entity_id")
        allowed = {
            ("media_player", "volume_set", self._voice_entity),
            ("light", "turn_on", self._light_entity),
            ("light", "turn_off", self._light_entity),
            ("select", "select_option", self._select_entity),
            ("text", "set_value", self._text_entity),
            ("number", "set_value", self._number_entity),
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
                    f"Home Assistant Core rejected an allowlisted action "
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

    async def correct_volume(self, entity_id: str, volume_level: float) -> None:
        if entity_id != self._voice_entity or not 0.0 <= volume_level <= 0.5:
            raise ValueError("Voice volume correction is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "media_player",
            "volume_set",
            {"entity_id": entity_id, "volume_level": volume_level},
        )

    async def set_light(self, entity_id: str, display: str) -> None:
        if entity_id != self._light_entity:
            raise ValueError("Voice LED light is not allowlisted")
        if display == "off":
            await asyncio.to_thread(
                self._post, "light", "turn_off", {"entity_id": entity_id},
            )
            return
        styles = {
            "playing": {"rgb_color": [0, 96, 255], "brightness": 64},
            "paused": {"rgb_color": [255, 128, 0], "brightness": 48},
            "sleeping": {"rgb_color": [32, 8, 0], "brightness": 8},
        }
        if display not in styles:
            raise ValueError("Voice LED display is not supported")
        await asyncio.to_thread(
            self._post,
            "light",
            "turn_on",
            {"entity_id": entity_id, **styles[display]},
        )

    async def set_select(self, entity_id: str, option: str) -> None:
        if entity_id != self._select_entity or option not in _CUSTOM_LED_OPTIONS:
            raise ValueError("Voice LED select option is not allowlisted")
        await asyncio.to_thread(
            self._post,
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
        )

    async def set_number(self, entity_id: str, value: float) -> None:
        if (
            entity_id != self._number_entity
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
            entity_id != self._text_entity
            or value != self._theme_payload
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
        settings: HardwareBridgeSettings,
        voice_entity: str,
        controller: PlaybackController,
        rest: RestActions,
    ) -> None:
        if not settings.enabled:
            raise ValueError(settings.disabled_reason or "Hardware bridge is disabled")
        self._settings = settings
        self._voice_entity = voice_entity
        self._controller = controller
        self._rest = rest
        entities = {
            voice_entity,
            settings.button_event_entity,
            settings.assist_satellite_entity,
            settings.led_entity,
        }
        if settings.volume_cap_number_entity:
            entities.add(settings.volume_cap_number_entity)
        if settings.led_theme_text_entity:
            entities.add(settings.led_theme_text_entity)
        self._entities = frozenset(entities)
        self._output_available = False
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
        return tuple(filter(None, (
            self._voice_entity,
            self._settings.button_event_entity,
            self._settings.assist_satellite_entity,
            self._settings.led_entity,
            self._settings.led_theme_text_entity,
            self._settings.volume_cap_number_entity,
        )))

    async def reconcile_snapshot(self, states: Iterable[object]) -> None:
        self._theme_sync_pending = False
        exact: dict[str, dict[str, object]] = {}
        for value in states:
            if not isinstance(value, dict):
                continue
            entity_id = value.get("entity_id")
            if entity_id in self._entities:
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
        if entity_id not in self._entities:
            raise ValueError("Core event entity is not configured")
        event_type = attributes.get("event_type")
        key = (entity_id, state, event_type, event_key)
        if live and self._duplicate(key):
            return

        if entity_id == self._voice_entity:
            await asyncio.to_thread(
                self._controller.observe_transport,
                state,
                attributes,
                live=live,
            )
            self._output_available = state not in ("off", "unknown", "unavailable")
            level = attributes.get("volume_level")
            if self._output_available and isinstance(level, (int, float)):
                await self._apply_volume(float(level))
        elif entity_id == self._settings.assist_satellite_entity:
            was_priority = self._assist_priority
            muted = attributes.get("microphone_muted") is True or attributes.get("muted") is True
            self._assist_idle = state == "idle"
            self._assist_priority = not self._assist_idle or muted
            if self._assist_idle and not muted:
                self._local_led_priority = False
            if was_priority or self._assist_priority:
                self._led_needs_reapply = True
        elif entity_id == self._settings.button_event_entity and live:
            if isinstance(event_type, str):
                await self._apply_gesture(event_type)
        elif entity_id == self._settings.led_entity and live:
            if not self._led_event_matches(state, attributes):
                self._local_led_priority = True
                self._led_needs_reapply = True
            return
        elif entity_id == self._settings.led_theme_text_entity:
            await self._sync_led_theme(state, attributes)
        elif entity_id == self._settings.volume_cap_number_entity:
            await self._sync_volume_cap_number(state, attributes)
        await self._reconcile_led()

    async def process_play_media_call(
        self,
        service_data: dict[str, object],
        *,
        user_id: object,
        own_user_id: str,
        event_key: object,
    ) -> None:
        target = service_data.get("entity_id")
        targets_voice = (
            target == self._voice_entity
            or isinstance(target, list) and self._voice_entity in target
        )
        if not targets_voice:
            return
        key = ("play_media", user_id, event_key)
        if self._duplicate(key):
            return
        await asyncio.to_thread(
            self._controller.observe_play_media,
            service_data.get("media_content_id"),
            owned_call=user_id == own_user_id,
        )

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
        state = await asyncio.to_thread(self._controller.state)
        capabilities = state.get("capabilities")
        if not isinstance(capabilities, dict):
            return
        configured_cap = capabilities.get("max_volume")
        if type(configured_cap) is not int:
            return
        cap = min(50, configured_cap)
        target = min(round(level * 100), cap)
        if state.get("volume") != target:
            await asyncio.to_thread(self._controller.set_volume, target)
        if level * 100 > cap:
            await self._rest.correct_volume(self._voice_entity, cap / 100)
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
        controller_state = await asyncio.to_thread(self._controller.state)
        capabilities = controller_state.get("capabilities")
        max_volume = (
            capabilities.get("max_volume")
            if isinstance(capabilities, dict) else None
        )
        if type(max_volume) is not int:
            raise ValueError("Controller volume capability is unavailable")
        target = min(0.5, max_volume / 100)
        if abs(current - target) < 0.0001:
            return
        await self._rest.set_number(
            self._settings.volume_cap_number_entity, target,
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
        target = self._settings.led_theme.payload
        if state == target:
            self._theme_sync_pending = False
            return
        if self._theme_sync_pending:
            return
        try:
            await self._rest.set_text(
                self._settings.led_theme_text_entity, target,
            )
        except (CoreActionError, OSError) as exc:
            logger.warning("Voice LED theme synchronization failed: %s", exc)
            return
        self._theme_sync_pending = True

    async def _apply_gesture(self, event_type: str) -> None:
        if event_type not in ("single_press", "double_press", "triple_press"):
            return
        state = await asyncio.to_thread(self._controller.state)
        if (
            self._assist_priority
            or not self._output_available
            or state.get("stop_pending") is True
            or state.get("error") is not None
            or state.get("playback") not in ("playing", "paused")
            or state.get("owns_transport") is not True
        ):
            return
        try:
            if event_type == "single_press":
                await asyncio.to_thread(self._controller.toggle_pause)
            elif event_type == "double_press":
                await asyncio.to_thread(self._controller.skip, "next")
            else:
                position = state.get("position")
                if isinstance(position, (int, float)) and position > 10:
                    await asyncio.to_thread(self._controller.restart_current)
                else:
                    await asyncio.to_thread(self._controller.skip, "previous")
        except (ControlError, PlayerError, StateError) as exc:
            logger.warning("Voice hardware gesture was rejected: %s", exc)

    async def _reconcile_led(self) -> None:
        state = await asyncio.to_thread(self._controller.state)
        if (
            self._assist_priority
            or self._local_led_priority
            or state.get("error") is not None
            or state.get("playback") == "error"
        ):
            self._led_needs_reapply = True
            return
        if not self._output_available:
            display = "off"
        elif state.get("playback") == "paused":
            display = "paused"
        elif state.get("playback") in ("playing", "buffering"):
            display = "playing"
        else:
            display = "sleeping"
        if display == self._last_led_display and not self._led_needs_reapply:
            return
        if self._settings.led_light_entity:
            await self._rest.set_light(self._settings.led_light_entity, display)
        else:
            await self._rest.set_select(self._settings.led_select_entity, display)
        self._last_led_display = display
        self._led_needs_reapply = False

    def _led_event_matches(
        self, state: str, attributes: dict[str, object],
    ) -> bool:
        display = self._last_led_display
        if display is None:
            return False
        if self._settings.led_select_entity:
            return state == display
        if display == "off":
            return state == "off"
        styles = {
            "playing": ([0, 96, 255], 64),
            "paused": ([255, 128, 0], 48),
            "sleeping": ([32, 8, 0], 8),
        }
        expected = styles.get(display)
        if expected is None or state != "on":
            return False
        color = attributes.get("rgb_color")
        brightness = attributes.get("brightness")
        if not isinstance(color, (list, tuple)):
            return False
        return list(color) == expected[0] and brightness == expected[1]


class CoreHardwareBridge:
    def __init__(
        self,
        settings: HardwareBridgeSettings,
        voice_entity: str,
        controller: PlaybackController,
        *,
        token: str,
        rest: RestActions | None = None,
        connector: Callable[[], AsyncContextManager[WebSocketConnection]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not settings.enabled:
            raise ValueError(settings.disabled_reason or "Hardware bridge is disabled")
        self._settings = settings
        self._voice_entity = voice_entity
        self._token = token
        self._rest = rest or CoreRestActions(
            token,
            voice_entity=voice_entity,
            light_entity=settings.led_light_entity,
            select_entity=settings.led_select_entity,
            text_entity=settings.led_theme_text_entity,
            theme_payload=settings.led_theme.payload,
            number_entity=settings.volume_cap_number_entity,
        )
        self._processor = HardwareIntentProcessor(
            settings, voice_entity, controller, self._rest,
        )
        self._connector = connector or self._default_connector
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._lock = RLock()
        self._status: dict[str, object] = {
            "enabled": True,
            "running": False,
            "connected": False,
            "reconnect_failures": 0,
            "error": None,
        }

    @classmethod
    def from_env(
        cls,
        settings: HardwareBridgeSettings,
        voice_entity: str,
        controller: PlaybackController,
    ) -> CoreHardwareBridge:
        return cls(
            settings,
            voice_entity,
            controller,
            token=os.environ.get("SUPERVISOR_TOKEN", ""),
        )

    def _default_connector(self) -> AsyncContextManager[WebSocketConnection]:
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
                ControlError,
                PlayerError,
                StateError,
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
            while failures < _MAX_RECONNECT_FAILURES:
                try:
                    await self.run_once()
                    raise BridgeProtocolError("Core WebSocket closed")
                except asyncio.CancelledError:
                    raise
                except (
                    BridgeProtocolError,
                    CoreActionError,
                    ControlError,
                    PlayerError,
                    StateError,
                    ConnectionClosed,
                    WebSocketException,
                    OSError,
                    TimeoutError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    failures += 1
                    delay = min(2 ** (failures - 1), _MAX_RECONNECT_SECONDS)
                    self._set_status(
                        connected=False,
                        reconnect_failures=failures,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    logger.warning(
                        "Home Assistant Core hardware bridge disconnected "
                        "(attempt %s/%s): %s",
                        failures,
                        _MAX_RECONNECT_FAILURES,
                        exc,
                    )
                    if failures >= _MAX_RECONNECT_FAILURES:
                        break
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
            command_id += 1
            current_user = await self._request(
                websocket,
                {"id": command_id, "type": "auth/current_user"},
                pending_events=pending_events,
            )
            own_user_id = (
                current_user.get("id")
                if isinstance(current_user, dict) else None
            )
            if not isinstance(own_user_id, str) or not own_user_id:
                raise BridgeProtocolError(
                    "Core WebSocket returned an invalid authenticated user",
                )
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
            await self._request(
                websocket,
                {
                    "id": command_id,
                    "type": "subscribe_trigger",
                    "trigger": {
                        "platform": "event",
                        "event_type": "call_service",
                        "event_data": {
                            "domain": "media_player",
                            "service": "play_media",
                        },
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
                await self._process_event(message, own_user_id)
            self._set_status(
                connected=True, reconnect_failures=0, error=None,
            )
            await asyncio.sleep(0)

            async for raw in websocket:
                message = self._decode(raw)
                if message.get("type") != "event":
                    continue
                await self._process_event(message, own_user_id)
        self._set_status(connected=False)

    async def _process_event(
        self,
        message: dict[str, object],
        own_user_id: str,
    ) -> None:
        play_media_call = self._event_play_media_call(message)
        if play_media_call is not None:
            await self._processor.process_play_media_call(
                play_media_call["service_data"],
                user_id=play_media_call["user_id"],
                own_user_id=own_user_id,
                event_key=play_media_call["event_key"],
            )
            return
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

    @staticmethod
    def _event_play_media_call(
        message: dict[str, object],
    ) -> dict[str, object] | None:
        event = message.get("event")
        if not isinstance(event, dict):
            return None
        variables = event.get("variables")
        trigger = variables.get("trigger") if isinstance(variables, dict) else None
        service_event = trigger.get("event") if isinstance(trigger, dict) else None
        if (
            not isinstance(service_event, dict)
            or service_event.get("event_type") != "call_service"
        ):
            return None
        data = service_event.get("data")
        if (
            not isinstance(data, dict)
            or data.get("domain") != "media_player"
            or data.get("service") != "play_media"
            or not isinstance(data.get("service_data"), dict)
        ):
            return None
        context = service_event.get("context")
        user_id = context.get("user_id") if isinstance(context, dict) else None
        context_id = context.get("id") if isinstance(context, dict) else None
        return {
            "service_data": data["service_data"],
            "user_id": user_id,
            "event_key": context_id or service_event.get("time_fired"),
        }


class DisabledHardwareBridge:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "running": False,
            "connected": False,
            "reconnect_failures": 0,
            "error": self._reason,
        }
