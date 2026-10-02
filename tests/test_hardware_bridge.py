from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from bedside_audio.config import (
    HardwareBridgeSettings,
    LedStyle,
    LedTheme,
    Settings,
)
from bedside_audio.hardware_bridge import (
    CoreHardwareBridge,
    CoreRestActions,
    HardwareIntentProcessor,
)

_MA = "media_player.bedroom_voice_music_assistant"
_VOICE = "media_player.bedroom_voice"
_BUTTON = "event.bedroom_voice_button"
_ASSIST = "assist_satellite.bedroom_voice"
_LIGHT = "light.bedroom_voice_ring"
_SELECT = "select.bedroom_voice_ring_mode"
_THEME = "text.bedroom_voice_led_theme"
_NUMBER = "number.bedroom_voice_volume_cap"
_THEME_PAYLOAD = "v1|#00FF30@012|#FF7000@018|#6000A0@008|#18BBF2@010"


def _run(coro):
    return asyncio.run(coro)


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeRest:
    calls: list[tuple[str, str, object]] = field(default_factory=list)

    async def control_player(
        self,
        entity_id: str,
        action: str,
        *,
        seek_position: float | None = None,
    ) -> None:
        self.calls.append(("player", entity_id, (action, seek_position)))

    async def correct_volume(self, entity_id: str, volume_level: float) -> None:
        self.calls.append(("volume", entity_id, volume_level))

    async def set_light(self, entity_id: str, display: str) -> None:
        self.calls.append(("light", entity_id, display))

    async def set_select(self, entity_id: str, option: str) -> None:
        self.calls.append(("select", entity_id, option))

    async def set_number(self, entity_id: str, value: float) -> None:
        self.calls.append(("number", entity_id, value))

    async def set_text(self, entity_id: str, value: str) -> None:
        self.calls.append(("text", entity_id, value))

    async def close(self) -> None:
        return None


def _settings(
    *,
    custom: bool = False,
    cap_number: bool = False,
    theme: bool = False,
    led_theme: LedTheme | None = None,
) -> Settings:
    hardware = HardwareBridgeSettings(
        button_event_entity=_BUTTON,
        assist_satellite_entity=_ASSIST,
        led_select_entity=_SELECT if custom else "",
        led_light_entity="" if custom else _LIGHT,
        led_theme_text_entity=_THEME if theme else "",
        volume_cap_number_entity=_NUMBER if cap_number else "",
        led_theme=led_theme or LedTheme(),
    )
    return Settings(_MA, _VOICE, 50, hardware)


async def _ready(
    processor: HardwareIntentProcessor,
    *,
    playback: str = "playing",
    position: float = 0.0,
    volume: float = 0.15,
) -> None:
    await processor.process_state(
        _MA,
        playback,
        {"media_position": position},
        live=False,
        event_key="ma-snapshot",
    )
    await processor.process_state(
        _VOICE,
        playback,
        {"volume_level": volume},
        live=False,
        event_key="voice-snapshot",
    )
    await processor.process_state(
        _ASSIST,
        "idle",
        {},
        live=False,
        event_key="assist-snapshot",
    )


def _player_calls(rest: FakeRest) -> list[tuple[str, float | None]]:
    return [
        value
        for kind, entity, value in rest.calls
        if kind == "player" and entity == _MA
    ]


def test_button_contract_controls_music_assistant_player() -> None:
    clock = FakeClock()
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), rest, clock=clock)

    async def exercise() -> None:
        await _ready(processor, position=2.0)
        await processor.process_state(
            _BUTTON,
            "2026-10-02T12:00:00-04:00",
            {"event_type": "single_press"},
            live=True,
            event_key="single",
        )
        await processor.process_state(
            _BUTTON,
            "2026-10-02T12:00:01-04:00",
            {"event_type": "double_press"},
            live=True,
            event_key="double",
        )
        clock.advance(9.0)
        await processor.process_state(
            _BUTTON,
            "2026-10-02T12:00:02-04:00",
            {"event_type": "triple_press"},
            live=True,
            event_key="triple-restart",
        )
        await processor.process_state(
            _MA,
            "paused",
            {"media_position": 10.0},
            live=True,
            event_key="paused-at-boundary",
        )
        await processor.process_state(
            _BUTTON,
            "2026-10-02T12:00:03-04:00",
            {"event_type": "triple_press"},
            live=True,
            event_key="triple-previous",
        )

    _run(exercise())
    assert _player_calls(rest) == [
        ("media_play_pause", None),
        ("media_next_track", None),
        ("media_seek", 0.0),
        ("media_previous_track", None),
    ]


def test_idle_music_assistant_queue_remains_resumable() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(custom=True), rest)

    async def exercise() -> None:
        await _ready(processor)
        await processor.process_state(
            _BUTTON,
            "first",
            {"event_type": "single_press"},
            live=True,
            event_key="pause",
        )
        await processor.process_state(
            _MA,
            "idle",
            {
                "active_queue": "bedside-queue",
                "media_content_id": "library://podcast_episode/1",
                "media_title": "Episode",
            },
            live=True,
            event_key="music-assistant-idle",
        )
        await processor.process_state(
            _BUTTON,
            "second",
            {"event_type": "single_press"},
            live=True,
            event_key="resume",
        )

    _run(exercise())
    assert ("select", _SELECT, "paused") in rest.calls
    assert _player_calls(rest) == [
        ("media_play_pause", None),
        ("media_play_pause", None),
    ]


def test_gestures_fail_closed_during_assist_or_without_active_playback() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), rest)

    async def exercise() -> None:
        await _ready(processor)
        await processor.process_state(
            _ASSIST,
            "listening",
            {},
            live=True,
            event_key="assist-listening",
        )
        await processor.process_state(
            _BUTTON,
            "one",
            {"event_type": "single_press"},
            live=True,
            event_key="blocked-by-assist",
        )
        await processor.process_state(
            _ASSIST,
            "idle",
            {},
            live=True,
            event_key="assist-idle",
        )
        await processor.process_state(
            _MA,
            "idle",
            {},
            live=True,
            event_key="ma-idle",
        )
        await processor.process_state(
            _BUTTON,
            "two",
            {"event_type": "double_press"},
            live=True,
            event_key="blocked-by-idle",
        )

    _run(exercise())
    assert _player_calls(rest) == []


def test_duplicate_button_event_is_applied_once() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), rest)

    async def exercise() -> None:
        await _ready(processor)
        for _ in range(2):
            await processor.process_state(
                _BUTTON,
                "same-state",
                {"event_type": "single_press"},
                live=True,
                event_key="same-context",
            )

    _run(exercise())
    assert _player_calls(rest) == [("media_play_pause", None)]


def test_volume_above_cap_is_corrected_on_native_voice_player() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), rest)

    _run(processor.process_state(
        _VOICE,
        "playing",
        {"volume_level": 0.8},
        live=True,
        event_key="unsafe-volume",
    ))

    assert ("volume", _VOICE, 0.5) in rest.calls


def test_custom_firmware_number_and_theme_are_synchronized() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(
        _settings(custom=True, cap_number=True, theme=True),
        rest,
    )

    async def exercise() -> None:
        await processor.process_state(
            _NUMBER,
            "0.25",
            {"min": 0.0, "max": 0.5, "step": 0.05},
            live=False,
            event_key="number-snapshot",
        )
        await processor.process_state(
            _THEME,
            "x" * 50,
            {"min": 50, "max": 50, "mode": "text"},
            live=False,
            event_key="theme-snapshot",
        )

    _run(exercise())
    assert ("number", _NUMBER, 0.5) in rest.calls
    assert ("text", _THEME, _THEME_PAYLOAD) in rest.calls


def test_custom_led_tracks_music_assistant_playback_state() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(custom=True), rest)

    async def exercise() -> None:
        await _ready(processor, playback="playing")
        await processor.process_state(
            _MA,
            "paused",
            {"media_position": 20.0},
            live=True,
            event_key="ma-paused",
        )
        await processor.process_state(
            _MA,
            "idle",
            {},
            live=True,
            event_key="ma-idle",
        )
        await processor.process_state(
            _VOICE,
            "unavailable",
            {},
            live=True,
            event_key="voice-unavailable",
        )

    _run(exercise())
    displays = [
        value
        for kind, entity, value in rest.calls
        if kind == "select" and entity == _SELECT
    ]
    assert displays == ["playing", "paused", "sleeping", "off"]


def test_stock_led_uses_configured_theme() -> None:
    theme = LedTheme(
        playing=LedStyle("#112233", 20),
        paused=LedStyle("#445566", 30),
        sleeping=LedStyle("#778899", 10),
        button_press=LedStyle("#AABBCC", 40),
    )
    settings = _settings(led_theme=theme)
    session = RestSession()
    rest = CoreRestActions("token", settings, session=session)

    _run(rest.set_light(_LIGHT, "playing"))

    assert session.calls == [(
        "http://supervisor/core/api/services/light/turn_on",
        {
            "entity_id": _LIGHT,
            "rgb_color": [17, 34, 51],
            "brightness_pct": 20,
        },
    )]


def test_rest_actions_reject_non_configured_entities() -> None:
    rest = CoreRestActions("token", _settings(), session=RestSession())

    with pytest.raises(ValueError, match="not allowlisted"):
        _run(rest.control_player(
            "media_player.somewhere_else",
            "media_next_track",
        ))
    with pytest.raises(ValueError, match="not allowlisted"):
        _run(rest.correct_volume(_MA, 0.25))


def test_snapshot_reconciliation_ignores_unconfigured_entities() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(custom=True), rest)

    _run(processor.reconcile_snapshot([
        {
            "entity_id": "media_player.unrelated",
            "state": "playing",
            "attributes": {"volume_level": 1.0},
        },
        {
            "entity_id": _MA,
            "state": "playing",
            "attributes": {"media_position": 1.0},
        },
        {
            "entity_id": _VOICE,
            "state": "playing",
            "attributes": {"volume_level": 0.15},
        },
        {
            "entity_id": _ASSIST,
            "state": "idle",
            "attributes": {},
        },
    ]))

    assert ("select", _SELECT, "playing") in rest.calls
    assert not any(call[1] == "media_player.unrelated" for call in rest.calls)


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, object]] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return self.messages.pop(0)

    def __aiter__(self) -> AsyncIterator[str]:
        async def empty() -> AsyncIterator[str]:
            if False:
                yield ""

        return empty()


def test_bridge_subscribes_only_to_configured_hardware_entities() -> None:
    settings = _settings(custom=True)
    entities = (
        _MA,
        _VOICE,
        _BUTTON,
        _ASSIST,
        _SELECT,
    )
    messages: list[dict[str, object]] = [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        *(
            {"id": index, "type": "result", "success": True, "result": None}
            for index in range(1, len(entities) + 1)
        ),
        {
            "id": len(entities) + 1,
            "type": "result",
            "success": True,
            "result": [
                {
                    "entity_id": _MA,
                    "state": "playing",
                    "attributes": {"media_position": 3.0},
                },
                {
                    "entity_id": _VOICE,
                    "state": "playing",
                    "attributes": {"volume_level": 0.15},
                },
                {
                    "entity_id": _ASSIST,
                    "state": "idle",
                    "attributes": {},
                },
            ],
        },
    ]
    websocket = FakeWebSocket(messages)

    @asynccontextmanager
    async def connector():
        yield websocket

    rest = FakeRest()
    bridge = CoreHardwareBridge(
        settings,
        token="token",
        rest=rest,
        connector=connector,
    )

    _run(bridge.run_once())

    subscriptions = [
        command["trigger"]["entity_id"]
        for command in websocket.sent
        if command.get("type") == "subscribe_trigger"
    ]
    assert subscriptions == list(entities)
    assert ("select", _SELECT, "playing") in rest.calls


class RestResponse:
    status_code = 200

    def json(self) -> list[object]:
        return []

    def close(self) -> None:
        return None


class RestSession:
    def __init__(self) -> None:
        self.trust_env = True
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> RestResponse:
        body = kwargs["json"]
        assert isinstance(body, dict)
        self.calls.append((url, body))
        return RestResponse()

    def close(self) -> None:
        return None
