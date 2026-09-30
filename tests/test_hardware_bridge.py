from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bedside_audio.catalog import DlnaLibraryCatalog, DlnaMedia
from bedside_audio.config import HardwareBridgeSettings, PlaylistSettings
from bedside_audio.controller import PlaybackController
from bedside_audio.hardware_bridge import (
    BridgeProtocolError,
    CoreHardwareBridge,
    CoreRestActions,
    HardwareIntentProcessor,
)
from bedside_audio.persistence import StateStore
from bedside_audio.player import PlayerError

from .conftest import FakeClock, FakePlayer

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


def _settings(
    *, custom: bool = False, cap_number: bool = False, theme: bool = False,
) -> HardwareBridgeSettings:
    return HardwareBridgeSettings(
        button_event_entity=_BUTTON,
        assist_satellite_entity=_ASSIST,
        led_select_entity=_SELECT if custom else "",
        led_light_entity="" if custom else _LIGHT,
        led_theme_text_entity=_THEME if theme else "",
        volume_cap_number_entity=_NUMBER if cap_number else "",
    )


@dataclass
class FakeController:
    playback: str = "playing"
    position: float | None = 30.0
    volume: int = 15
    max_volume: int = 50
    owns_transport: bool = True
    stop_pending: bool = False
    error: str | None = None
    timer_remaining: float | None = None
    expected_media_content_id: str = "media-source://dlna_dms/bedside/:1"
    fail_volume: bool = False
    fail_play_media_observation: bool = False
    calls: list[tuple[str, object | None]] = field(default_factory=list)
    observations: list[tuple[str, bool]] = field(default_factory=list)
    play_media_observations: list[tuple[object, bool]] = field(default_factory=list)

    def state(self) -> dict[str, object]:
        return {
            "playback": self.playback,
            "position": self.position,
            "volume": self.volume,
            "owns_transport": self.owns_transport,
            "stop_pending": self.stop_pending,
            "error": self.error,
            "timer_remaining": self.timer_remaining,
            "capabilities": {"max_volume": self.max_volume},
        }

    def toggle_pause(self) -> dict[str, object]:
        self.calls.append(("toggle", None))
        self.playback = "paused" if self.playback == "playing" else "playing"
        return self.state()

    def skip(self, direction: str) -> dict[str, object]:
        self.calls.append(("skip", direction))
        return self.state()

    def restart_current(self) -> dict[str, object]:
        self.calls.append(("restart", None))
        self.position = 0.0
        return self.state()

    def set_volume(self, volume: int) -> dict[str, object]:
        if self.fail_volume:
            raise PlayerError("Voice volume failed")
        self.calls.append(("volume", volume))
        self.volume = volume
        return self.state()

    def observe_transport(
        self,
        state: str,
        attributes: dict[str, object],
        *,
        live: bool,
    ) -> None:
        self.observations.append((state, live))
        media_content_id = attributes.get("media_content_id")
        self.owns_transport = (
            isinstance(media_content_id, str)
            and media_content_id == self.expected_media_content_id
        )

    def observe_play_media(
        self,
        media_content_id: object,
        *,
        owned_call: bool,
    ) -> None:
        if self.fail_play_media_observation:
            raise PlayerError("Transport provenance failed")
        self.play_media_observations.append((media_content_id, owned_call))
        self.owns_transport = (
            owned_call
            and media_content_id == self.expected_media_content_id
        )


@dataclass
class FakeRest:
    calls: list[tuple[str, str, object]] = field(default_factory=list)
    correction_failures: int = 0

    async def correct_volume(self, entity_id: str, volume_level: float) -> None:
        self.calls.append(("volume", entity_id, volume_level))
        if self.correction_failures:
            self.correction_failures -= 1
            raise OSError("Core correction failed")

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


class ControllerCatalog:
    def health(self, relative: str | None = None) -> None:
        return None

    def prepare(self, relative: str):
        paths = ["a", "b", "c"]
        index = paths.index(relative)
        return (
            DlnaMedia(
                f"media-source://dlna_dms/calculon/:4{index}",
                "video/x-matroska",
                f"Episode {index}",
                f"DLNA / Episode {index}",
            ),
            paths,
            index,
        )

    def file(self, relative: str):
        return self.prepare(relative)[0]

    def configured_queue(self, items: tuple[tuple[str, ...], ...]):
        assert items == (("Configured",),)
        return ["c", "a", "b"]

    def list_dir(self, relative: str = ""):
        return {"path": relative, "parent": None, "entries": []}


async def _ready(
    processor: HardwareIntentProcessor,
    *,
    volume: float = 0.15,
) -> None:
    await processor.process_state(
        _VOICE, "playing", {
            "volume_level": volume,
            "media_content_id": "media-source://dlna_dms/bedside/:1",
        },
        live=False, event_key="voice-snapshot",
    )
    await processor.process_state(
        _ASSIST, "idle", {},
        live=False, event_key="assist-snapshot",
    )


def test_button_contract_maps_single_double_and_triple_with_ten_second_boundary() -> None:
    controller = FakeController()
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )

    async def exercise() -> None:
        await _ready(processor)
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:00+00:00", {"event_type": "single_press"},
            live=True, event_key="single",
        )
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:01+00:00", {"event_type": "double_press"},
            live=True, event_key="double",
        )
        controller.position = 10.01
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:02+00:00", {"event_type": "triple_press"},
            live=True, event_key="triple-restart",
        )
        controller.position = 10.0
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:03+00:00", {"event_type": "triple_press"},
            live=True, event_key="triple-previous",
        )
        controller.position = None
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:04+00:00", {"event_type": "triple_press"},
            live=True, event_key="triple-fail-closed",
        )

    _run(exercise())
    assert controller.calls == [
        ("toggle", None),
        ("skip", "next"),
        ("restart", None),
        ("skip", "previous"),
        ("skip", "previous"),
    ]


def test_bedside_play_survives_esphome_playing_without_media_id_for_single_press(
    tmp_path: Path,
) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(ControllerCatalog()),
        player,
        StateStore(tmp_path / "state", 15),
        max_volume=50,
    )
    started = controller.play("dlna/a")
    generation = started["transport_generation"]
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, rest)

    async def exercise() -> None:
        await processor.process_state(
            _VOICE,
            "playing",
            {"volume_level": 0.15},
            live=True,
            event_key="esphome-playing-without-media-id",
        )
        await processor.process_state(
            _ASSIST,
            "idle",
            {},
            live=True,
            event_key="assist-idle",
        )
        for _ in range(2):
            await processor.process_state(
                _BUTTON,
                "2026-09-30T13:57:27-04:00",
                {"event_type": "single_press"},
                live=True,
                event_key="single-press-13:57:27",
            )

    _run(exercise())
    state = controller.state()
    assert state["transport_generation"] == generation
    assert rest.calls == [
        ("light", _LIGHT, "playing"),
        ("light", _LIGHT, "paused"),
    ]
    assert player.paused is True
    assert state["playback"] == "paused"


@pytest.mark.parametrize(
    ("caller_user_id", "foreign_media_content_id"),
    [
        ("foreign-user", "media-source://dlna_dms/foreign/:99"),
        ("foreign-user", "media-source://dlna_dms/calculon/:40"),
        ("bedside-app-user", "media-source://dlna_dms/foreign/:99"),
        ("bedside-app-user", None),
    ],
)
def test_foreign_play_media_call_revokes_missing_id_transport_before_gesture(
    tmp_path: Path,
    caller_user_id: str,
    foreign_media_content_id: str | None,
) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(ControllerCatalog()),
        player,
        StateStore(tmp_path / "state", 15),
        max_volume=50,
    )
    started = controller.play("dlna/a")
    generation = started["transport_generation"]
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )

    async def exercise() -> None:
        await processor.process_state(
            _VOICE,
            "playing",
            {"volume_level": 0.15},
            live=True,
            event_key="esphome-playing-without-media-id",
        )
        await processor.process_state(
            _ASSIST,
            "idle",
            {},
            live=True,
            event_key="assist-idle",
        )
        service_data: dict[str, object] = {"entity_id": _VOICE}
        if foreign_media_content_id is not None:
            service_data["media_content_id"] = foreign_media_content_id
        await processor.process_play_media_call(
            service_data,
            user_id=caller_user_id,
            own_user_id="bedside-app-user",
            event_key="foreign-play-media",
        )
        await processor.process_state(
            _VOICE,
            "playing",
            {"volume_level": 0.15},
            live=True,
            event_key="same-state-after-foreign-play-media",
        )
        await processor.process_state(
            _BUTTON,
            "2026-09-30T13:57:27-04:00",
            {"event_type": "single_press"},
            live=True,
            event_key="single-after-foreign",
        )

    _run(exercise())
    state = controller.state()
    assert state["transport_generation"] == generation
    assert state["owns_transport"] is False
    assert player.paused is False


def test_exact_owned_play_media_call_reclaims_after_missing_id_reconnect(
    tmp_path: Path,
) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(ControllerCatalog()),
        player,
        StateStore(tmp_path / "state", 15),
        max_volume=50,
    )
    generation = controller.play("dlna/a")["transport_generation"]
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )

    async def exercise() -> None:
        await processor.process_state(
            _VOICE,
            "playing",
            {"volume_level": 0.15},
            live=False,
            event_key="reconnect-snapshot-without-media-id",
        )
        assert controller.state()["owns_transport"] is False
        await processor.process_play_media_call(
            {
                "entity_id": _VOICE,
                "media_content_id": player.media_content_id,
            },
            user_id="bedside-app-user",
            own_user_id="bedside-app-user",
            event_key="new-owned-play-media",
        )
        await processor.process_state(
            _ASSIST,
            "idle",
            {},
            live=True,
            event_key="assist-idle",
        )
        await processor.process_state(
            _BUTTON,
            "2026-09-30T13:57:27-04:00",
            {"event_type": "single_press"},
            live=True,
            event_key="single-after-owned-call",
        )

    _run(exercise())
    state = controller.state()
    assert state["transport_generation"] == generation
    assert state["owns_transport"] is True
    assert player.paused is True


def test_playlist_queue_keeps_gesture_provenance_generation_and_timer_policy(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(ControllerCatalog()),
        player,
        StateStore(tmp_path / "state", 15),
        clock=clock,
        max_volume=50,
        playlists=(PlaylistSettings("Quiet evening", (("Configured",),)),),
    )
    started = controller.play_playlist("Quiet evening")
    first_generation = started["transport_generation"]
    controller.set_timer(30)
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )

    async def exercise() -> None:
        await processor.process_state(
            _VOICE,
            "playing",
            {"volume_level": 0.15},
            live=True,
            event_key="playlist-playing-without-media-id",
        )
        await processor.process_state(
            _ASSIST,
            "idle",
            {},
            live=True,
            event_key="playlist-assist-idle",
        )
        await processor.process_state(
            _BUTTON,
            "2026-09-30T14:00:00-04:00",
            {"event_type": "double_press"},
            live=True,
            event_key="playlist-next",
        )

        controller.state()
        clock.advance(11)
        await processor.process_state(
            _BUTTON,
            "2026-09-30T14:00:11-04:00",
            {"event_type": "triple_press"},
            live=True,
            event_key="playlist-restart",
        )

        await processor.process_play_media_call(
            {"entity_id": _VOICE},
            user_id="foreign-user",
            own_user_id="bedside-app-user",
            event_key="foreign-missing-id",
        )
        await processor.process_state(
            _BUTTON,
            "2026-09-30T14:00:12-04:00",
            {"event_type": "double_press"},
            live=True,
            event_key="blocked-foreign-next",
        )

    _run(exercise())
    state = controller.state()
    assert state["current"]["path"] == "dlna/a"
    assert state["queue"]["kind"] == "playlist"
    assert state["queue"]["name"] == "Quiet evening"
    assert state["transport_generation"] == first_generation + 2
    assert state["owns_transport"] is False
    assert state["timer_remaining"] == pytest.approx(30 * 60 - 11)
    assert state["capabilities"]["auto_advance"] is False
    assert [media.name for media in player.loads] == [
        "Episode 2",
        "Episode 0",
        "Episode 0",
    ]


def test_duplicate_play_media_event_is_observed_once() -> None:
    controller = FakeController(owns_transport=False)
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )
    service_data = {
        "entity_id": _VOICE,
        "media_content_id": controller.expected_media_content_id,
    }

    async def exercise() -> None:
        for _ in range(2):
            await processor.process_play_media_call(
                service_data,
                user_id="bedside-app-user",
                own_user_id="bedside-app-user",
                event_key="same-call-context",
            )

    _run(exercise())
    assert controller.play_media_observations == [
        (controller.expected_media_content_id, True),
    ]


def test_play_media_event_for_other_entity_does_not_change_ownership() -> None:
    controller = FakeController()
    processor = HardwareIntentProcessor(
        _settings(), _VOICE, controller, FakeRest(),
    )

    _run(processor.process_play_media_call(
        {
            "entity_id": "media_player.other",
            "media_content_id": "media-source://dlna_dms/foreign/:99",
        },
        user_id="foreign-user",
        own_user_id="bedside-app-user",
        event_key="other-player-call",
    ))

    assert controller.play_media_observations == []
    assert controller.owns_transport is True


@pytest.mark.parametrize(
    ("changes", "voice_state", "assist_state"),
    [
        ({"owns_transport": False}, "playing", "idle"),
        ({"stop_pending": True}, "playing", "idle"),
        ({"error": "recovery required"}, "playing", "idle"),
        ({"playback": "error"}, "playing", "idle"),
        ({}, "unavailable", "idle"),
        ({}, "off", "idle"),
        ({}, "playing", "listening"),
    ],
)
def test_button_gates_reject_assist_stop_error_unavailable_and_foreign_transport(
    changes: dict[str, object], voice_state: str, assist_state: str,
) -> None:
    controller = FakeController()
    for name, value in changes.items():
        setattr(controller, name, value)
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, FakeRest())

    async def exercise() -> None:
        await processor.process_state(
            _VOICE, voice_state, {"volume_level": 0.15},
            live=False, event_key="voice",
        )
        await processor.process_state(
            _ASSIST, assist_state, {},
            live=False, event_key="assist",
        )
        await processor.process_state(
            _BUTTON, "2026-09-29T01:00:00+00:00", {"event_type": "double_press"},
            live=True, event_key="gesture",
        )

    _run(exercise())
    assert controller.calls == []


def test_snapshots_never_replay_gestures_or_auto_next_and_live_tuples_deduplicate() -> None:
    controller = FakeController(playback="stopped", owns_transport=False)
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, FakeRest())

    async def exercise() -> None:
        await _ready(processor)
        event = {
            "entity_id": _BUTTON,
            "state": "2026-09-29T01:00:00+00:00",
            "attributes": {"event_type": "double_press"},
            "last_updated": "2026-09-29T01:00:00+00:00",
        }
        await processor.reconcile_snapshot([event])
        controller.playback = "playing"
        controller.owns_transport = True
        await processor.process_state(
            _BUTTON, event["state"], event["attributes"],
            live=True, event_key=event["last_updated"],
        )
        await processor.process_state(
            _BUTTON, event["state"], event["attributes"],
            live=True, event_key=event["last_updated"],
        )

    _run(exercise())
    assert controller.calls == [("skip", "next")]


def test_dial_routes_through_controller_caps_at_fifty_percent_and_corrects_once() -> None:
    controller = FakeController(max_volume=40)
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, rest)

    async def exercise() -> None:
        await processor.process_state(
            _VOICE, "playing", {
                "volume_level": 0.55,
                "media_content_id": controller.expected_media_content_id,
            },
            live=True, event_key="overshoot-1",
        )
        await processor.process_state(
            _VOICE, "playing", {
                "volume_level": 0.55,
                "media_content_id": controller.expected_media_content_id,
            },
            live=True, event_key="overshoot-2",
        )
        await processor.process_state(
            _VOICE, "playing", {
                "volume_level": 0.40,
                "media_content_id": controller.expected_media_content_id,
            },
            live=True, event_key="correction",
        )
        await processor.process_state(
            _VOICE, "playing", {
                "volume_level": 0.35,
                "media_content_id": controller.expected_media_content_id,
            },
            live=True, event_key="accepted",
        )

    _run(exercise())
    assert controller.calls == [("volume", 40), ("volume", 35)]
    assert rest.calls == [("volume", _VOICE, 0.4)]


def test_failed_volume_correction_retries_identical_reconnect_snapshot() -> None:
    controller = FakeController(max_volume=40)
    rest = FakeRest(correction_failures=1)
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, rest)
    attributes = {
        "volume_level": 0.55,
        "media_content_id": controller.expected_media_content_id,
    }

    async def exercise() -> None:
        with pytest.raises(OSError, match="correction failed"):
            await processor.process_state(
                _VOICE, "playing", attributes,
                live=False, event_key="snapshot-1",
            )
        await processor.process_state(
            _VOICE, "playing", attributes,
            live=False, event_key="snapshot-2",
        )

    _run(exercise())
    assert controller.calls == [("volume", 40)]
    assert rest.calls == [
        ("volume", _VOICE, 0.4),
        ("volume", _VOICE, 0.4),
    ]


def test_configured_firmware_volume_cap_number_syncs_idempotently() -> None:
    controller = FakeController(max_volume=40)
    rest = FakeRest()
    processor = HardwareIntentProcessor(
        _settings(cap_number=True), _VOICE, controller, rest,
    )
    assert processor.entities == (_VOICE, _BUTTON, _ASSIST, _LIGHT, _NUMBER)

    async def exercise() -> None:
        await processor.process_state(
            _NUMBER, "0.50", {"min": 0.0, "max": 0.5, "step": 0.05},
            live=False, event_key="number-snapshot",
        )
        await processor.process_state(
            _NUMBER, "0.40", {"min": 0.0, "max": 0.5, "step": 0.05},
            live=True, event_key="number-echo",
        )

    _run(exercise())
    assert rest.calls == [("number", _NUMBER, 0.4)]


def test_configured_firmware_theme_syncs_snapshot_mismatch_and_deduplicates_echoes() -> None:
    rest = FakeRest()
    processor = HardwareIntentProcessor(
        _settings(custom=True, theme=True), _VOICE, FakeController(), rest,
    )
    assert processor.entities == (_VOICE, _BUTTON, _ASSIST, _SELECT, _THEME)

    async def exercise() -> None:
        await processor.process_state(
            _THEME,
            "v1|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100",
            {"min": 50, "max": 50, "mode": "text"},
            live=False,
            event_key="theme-snapshot",
        )
        await processor.process_state(
            _THEME,
            "v1|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100",
            {"min": 50, "max": 50, "mode": "text"},
            live=True,
            event_key="theme-stale-echo",
        )
        await processor.process_state(
            _THEME,
            _THEME_PAYLOAD,
            {"min": 50, "max": 50, "mode": "text"},
            live=True,
            event_key="theme-valid-echo",
        )
        await processor.process_state(
            _THEME,
            "v1|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100|#FFFFFF@100",
            {"min": 50, "max": 50, "mode": "text"},
            live=True,
            event_key="theme-later-mismatch",
        )

    _run(exercise())
    assert rest.calls == [
        ("text", _THEME, _THEME_PAYLOAD),
        ("text", _THEME, _THEME_PAYLOAD),
    ]


def test_theme_sync_retries_on_reconnect_and_isolates_rest_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingThemeRest(FakeRest):
        failures = 1

        async def set_text(self, entity_id: str, value: str) -> None:
            self.calls.append(("text", entity_id, value))
            if self.failures:
                self.failures -= 1
                raise OSError("theme write failed")

    rest = FailingThemeRest()
    processor = HardwareIntentProcessor(
        _settings(custom=True, theme=True), _VOICE, FakeController(), rest,
    )
    snapshot = [{
        "entity_id": _THEME,
        "state": "unknown",
        "attributes": {"min": 50, "max": 50, "mode": "text"},
        "last_updated": "theme-snapshot",
    }]

    async def exercise() -> None:
        await processor.reconcile_snapshot(snapshot)
        await processor.reconcile_snapshot(snapshot)

    with caplog.at_level(logging.WARNING):
        _run(exercise())
    assert rest.calls == [
        ("text", _THEME, _THEME_PAYLOAD),
        ("text", _THEME, _THEME_PAYLOAD),
    ]
    assert "theme write failed" in caplog.text


def test_theme_entity_contract_must_match_fixed_payload_shape() -> None:
    processor = HardwareIntentProcessor(
        _settings(custom=True, theme=True), _VOICE, FakeController(), FakeRest(),
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="theme text contract"):
            await processor.process_state(
                _THEME,
                "unknown",
                {"min": 0, "max": 255, "mode": "text"},
                live=False,
                event_key="bad-contract",
            )

    _run(exercise())


def test_core_rest_actions_are_tightly_limited_to_configured_entities_and_services() -> None:
    session = RestSession()
    actions = CoreRestActions(
        "synthetic-token",
        voice_entity=_VOICE,
        light_entity=_LIGHT,
        select_entity=_SELECT,
        text_entity=_THEME,
        theme_payload=_THEME_PAYLOAD,
        number_entity=_NUMBER,
        session=session,
    )

    async def exercise() -> None:
        await actions.correct_volume(_VOICE, 0.5)
        await actions.set_light(_LIGHT, "sleeping")
        await actions.set_select(_SELECT, "paused")
        await actions.set_text(_THEME, _THEME_PAYLOAD)
        await actions.set_number(_NUMBER, 0.4)
        with pytest.raises(ValueError, match="not allowlisted"):
            await actions.correct_volume("media_player.other", 0.5)
        with pytest.raises(ValueError, match="not allowlisted"):
            await actions.set_select(_SELECT, "error")
        with pytest.raises(ValueError, match="not allowlisted"):
            await actions.set_text("text.other", _THEME_PAYLOAD)

    _run(exercise())
    assert session.trust_env is False
    assert session.headers["Authorization"].startswith("Bearer ")
    assert session.headers["Authorization"].endswith("synthetic-token")
    assert session.headers["Authorization"] == "Bearer synthetic-token"
    assert session.calls == [
        (
            "http://supervisor/core/api/services/media_player/volume_set",
            {"entity_id": _VOICE, "volume_level": 0.5},
        ),
        (
            "http://supervisor/core/api/services/light/turn_on",
            {
                "entity_id": _LIGHT,
                "rgb_color": [32, 8, 0],
                "brightness": 8,
            },
        ),
        (
            "http://supervisor/core/api/services/select/select_option",
            {"entity_id": _SELECT, "option": "paused"},
        ),
        (
            "http://supervisor/core/api/services/text/set_value",
            {"entity_id": _THEME, "value": _THEME_PAYLOAD},
        ),
        (
            "http://supervisor/core/api/services/number/set_value",
            {"entity_id": _NUMBER, "value": 0.4},
        ),
    ]


def test_stock_and_custom_led_outputs_use_sleeping_for_stopped_and_reapply_after_assist() -> None:
    stock_controller = FakeController(playback="stopped", owns_transport=False)
    stock_rest = FakeRest()
    stock = HardwareIntentProcessor(
        _settings(), _VOICE, stock_controller, stock_rest,
    )
    custom_controller = FakeController(playback="paused", timer_remaining=120)
    custom_rest = FakeRest()
    custom = HardwareIntentProcessor(
        _settings(custom=True), _VOICE, custom_controller, custom_rest,
    )

    async def exercise() -> None:
        await _ready(stock)
        await stock.process_state(
            _ASSIST, "listening", {},
            live=True, event_key="assist-listening",
        )
        stock_controller.playback = "playing"
        stock_controller.owns_transport = True
        await stock.process_state(
            _VOICE, "playing", {"volume_level": 0.15},
            live=True, event_key="playing-during-assist",
        )
        await stock.process_state(
            _ASSIST, "idle", {},
            live=True, event_key="assist-idle",
        )
        await _ready(custom)

    _run(exercise())
    assert stock_rest.calls == [
        ("light", _LIGHT, "sleeping"),
        ("light", _LIGHT, "playing"),
    ]
    assert custom_rest.calls == [("select", _SELECT, "paused")]


def test_unavailable_output_uses_off_instead_of_sleeping() -> None:
    controller = FakeController(playback="stopped", owns_transport=False)
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, rest)

    async def exercise() -> None:
        await processor.process_state(
            _VOICE, "unavailable", {},
            live=False, event_key="voice-unavailable",
        )
        await processor.process_state(
            _ASSIST, "idle", {},
            live=False, event_key="assist-idle",
        )

    _run(exercise())
    assert rest.calls == [("light", _LIGHT, "off")]


def test_local_led_mute_or_error_display_is_not_overridden_until_assist_returns_idle() -> None:
    controller = FakeController(playback="playing")
    rest = FakeRest()
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, rest)

    async def exercise() -> None:
        await _ready(processor)
        await processor.process_state(
            _LIGHT,
            "on",
            {"rgb_color": [255, 0, 0], "brightness": 255},
            live=True,
            event_key="local-priority",
        )
        controller.playback = "paused"
        await processor.process_state(
            _VOICE, "paused", {"volume_level": 0.15},
            live=True, event_key="paused-during-local-priority",
        )
        assert rest.calls == [("light", _LIGHT, "playing")]
        await processor.process_state(
            _ASSIST, "idle", {"microphone_muted": False},
            live=True, event_key="local-priority-cleared",
        )

    _run(exercise())
    assert rest.calls == [
        ("light", _LIGHT, "playing"),
        ("light", _LIGHT, "paused"),
    ]


class FakeSocket:
    def __init__(self, messages: list[object], *, hold_open: bool = False) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.hold_open = hold_open

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        if not self.messages:
            raise OSError("socket closed")
        return self.messages.pop(0)

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        if not self.messages:
            if self.hold_open:
                await asyncio.Future()
            raise StopAsyncIteration
        return self.messages.pop(0)


def _socket_messages(
    *, auth_ok: bool = True, snapshot_volume: float = 0.15,
) -> list[object]:
    auth = {"type": "auth_ok"} if auth_ok else {
        "type": "auth_invalid", "message": "bad token",
    }
    return [
        {"type": "auth_required", "ha_version": "2026.9.1"},
        auth,
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": {"id": "bedside-app-user"},
        },
        {"id": 2, "type": "result", "success": True, "result": None},
        {
            "id": 2,
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "to_state": {
                            "entity_id": _VOICE,
                            "state": "playing",
                            "attributes": {
                                "volume_level": 0.20,
                                "media_content_id": (
                                    "media-source://dlna_dms/bedside/:1"
                                ),
                            },
                            "last_updated": "2026-09-29T01:00:01+00:00",
                        },
                    },
                },
            },
        },
        {"id": 3, "type": "result", "success": True, "result": None},
        {
            "id": 3,
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "to_state": {
                            "entity_id": _BUTTON,
                            "state": "2026-09-29T01:00:00+00:00",
                            "attributes": {"event_type": "double_press"},
                            "last_updated": "2026-09-29T01:00:00+00:00",
                        },
                    },
                },
            },
        },
        {"id": 4, "type": "result", "success": True, "result": None},
        {"id": 5, "type": "result", "success": True, "result": None},
        {"id": 6, "type": "result", "success": True, "result": None},
        {
            "id": 7,
            "type": "result",
            "success": True,
            "result": [
                {
                    "entity_id": _BUTTON,
                    "state": "2026-09-29T01:00:00+00:00",
                    "attributes": {"event_type": "double_press"},
                    "last_updated": "2026-09-29T01:00:00+00:00",
                },
                {
                    "entity_id": _VOICE,
                    "state": "playing",
                    "attributes": {
                        "volume_level": snapshot_volume,
                        "media_content_id": "media-source://dlna_dms/bedside/:1",
                    },
                    "last_updated": "2026-09-29T01:00:00+00:00",
                },
                {
                    "entity_id": _ASSIST,
                    "state": "idle",
                    "attributes": {},
                    "last_updated": "2026-09-29T01:00:00+00:00",
                },
            ],
        },
    ]


def _play_media_event(
    *,
    context_id: str = "owned-play-context",
    user_id: str = "bedside-app-user",
    media_content_id: str = "media-source://dlna_dms/bedside/:1",
) -> dict[str, object]:
    return {
        "id": 6,
        "type": "event",
        "event": {
            "variables": {
                "trigger": {
                    "event": {
                        "event_type": "call_service",
                        "data": {
                            "domain": "media_player",
                            "service": "play_media",
                            "service_data": {
                                "entity_id": _VOICE,
                                "media_content_id": media_content_id,
                            },
                        },
                        "context": {
                            "id": context_id,
                            "user_id": user_id,
                        },
                    },
                },
            },
        },
    }


def _button_event(
    event_type: str = "single_press",
    event_key: str = "2026-09-30T13:57:27-04:00",
) -> dict[str, object]:
    return {
        "id": 3,
        "type": "event",
        "event": {
            "variables": {
                "trigger": {
                    "to_state": {
                        "entity_id": _BUTTON,
                        "state": event_key,
                        "attributes": {"event_type": event_type},
                        "last_updated": event_key,
                    },
                },
            },
        },
    }


def _ready_protocol_messages(*events: dict[str, object]) -> list[object]:
    return [
        {"type": "auth_required", "ha_version": "2026.9.1"},
        {"type": "auth_ok"},
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": {"id": "bedside-app-user"},
        },
        *[
            {"id": command_id, "type": "result", "success": True, "result": None}
            for command_id in range(2, 7)
        ],
        {
            "id": 7,
            "type": "result",
            "success": True,
            "result": [
                {
                    "entity_id": _VOICE,
                    "state": "playing",
                    "attributes": {"volume_level": 0.15},
                    "last_updated": "2026-09-30T13:57:20-04:00",
                },
                {
                    "entity_id": _ASSIST,
                    "state": "idle",
                    "attributes": {},
                    "last_updated": "2026-09-30T13:57:20-04:00",
                },
            ],
        },
        *events,
    ]


def _play_media_observation_messages(
    context_id: str = "owned-play-context",
) -> list[object]:
    return _ready_protocol_messages(
        _play_media_event(context_id=context_id),
    )


def test_core_auth_is_redacted_subscriptions_are_exact_and_snapshot_does_not_replay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "synthetic-supervisor-secret"
    socket = FakeSocket(_socket_messages())
    controller = FakeController()
    bridge = CoreHardwareBridge(
        _settings(), _VOICE, controller, token=token,
        rest=FakeRest(),
        connector=lambda: _socket_context(socket),
    )
    with caplog.at_level(logging.WARNING):
        _run(bridge.run_once())

    assert socket.sent[0] == {"type": "auth", "access_token": token}
    assert socket.sent[1] == {"id": 1, "type": "auth/current_user"}
    subscriptions = socket.sent[2:6]
    assert [command["trigger"]["entity_id"] for command in subscriptions] == [
        _VOICE, _BUTTON, _ASSIST, _LIGHT,
    ]
    assert all(command["type"] == "subscribe_trigger" for command in subscriptions)
    assert socket.sent[6] == {
        "id": 6,
        "type": "subscribe_trigger",
        "trigger": {
            "platform": "event",
            "event_type": "call_service",
            "event_data": {
                "domain": "media_player",
                "service": "play_media",
            },
        },
    }
    assert socket.sent[7] == {"id": 7, "type": "get_states"}
    assert controller.calls == [("volume", 20), ("skip", "next")]
    assert token not in caplog.text
    assert token not in repr(bridge.status())


def test_core_filters_play_media_calls_and_uses_authenticated_user_provenance() -> None:
    messages = _ready_protocol_messages(
        _play_media_event(),
        _button_event(),
    )
    socket = FakeSocket(messages)
    controller = FakeController(owns_transport=False)
    bridge = CoreHardwareBridge(
        _settings(),
        _VOICE,
        controller,
        token="synthetic-token",
        rest=FakeRest(),
        connector=lambda: _socket_context(socket),
    )

    _run(bridge.run_once())

    assert socket.sent[1] == {"id": 1, "type": "auth/current_user"}
    state_subscriptions = socket.sent[2:6]
    assert [
        command["trigger"]["entity_id"] for command in state_subscriptions
    ] == [_VOICE, _BUTTON, _ASSIST, _LIGHT]
    assert socket.sent[6] == {
        "id": 6,
        "type": "subscribe_trigger",
        "trigger": {
            "platform": "event",
            "event_type": "call_service",
            "event_data": {
                "domain": "media_player",
                "service": "play_media",
            },
        },
    }
    assert socket.sent[7] == {"id": 7, "type": "get_states"}
    assert controller.play_media_observations == [
        ("media-source://dlna_dms/bedside/:1", True),
    ]
    assert controller.calls == [("toggle", None)]


def test_failed_core_auth_never_exposes_supervisor_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "synthetic-supervisor-secret"
    bridge = CoreHardwareBridge(
        _settings(), _VOICE, FakeController(), token=token,
        rest=FakeRest(),
        connector=lambda: _socket_context(FakeSocket(_socket_messages(auth_ok=False))),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(
        BridgeProtocolError, match="authentication failed",
    ):
        _run(bridge.run_once())

    assert token not in caplog.text
    assert token not in str(bridge.status())


@asynccontextmanager
async def _socket_context(socket: FakeSocket):
    yield socket


def test_reconnect_is_bounded_and_shutdown_closes_without_crashing_controller() -> None:
    controller = FakeController()
    socket = FakeSocket(_socket_messages(), hold_open=True)
    attempts = 0
    sleeps: list[float] = []

    @asynccontextmanager
    async def connector():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Core unavailable")
        yield socket

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    bridge = CoreHardwareBridge(
        _settings(), _VOICE, controller, token="synthetic-token",
        rest=FakeRest(), connector=connector, sleep=sleep,
    )

    async def exercise() -> None:
        await bridge.start()
        for _ in range(200):
            if bridge.status()["connected"]:
                break
            await asyncio.sleep(0)
        assert bridge.status()["connected"] is True
        await bridge.close()

    _run(exercise())
    assert attempts == 2
    assert sleeps == [1.0]
    assert bridge.status()["running"] is False
    assert controller.calls == [("volume", 20), ("skip", "next")]


def test_controller_failure_reconnects_sets_terminal_status_and_close_is_safe() -> None:
    attempts = 0
    sleeps: list[float] = []
    controller = FakeController(fail_volume=True)

    @asynccontextmanager
    async def connector():
        nonlocal attempts
        attempts += 1
        yield FakeSocket(_socket_messages(snapshot_volume=0.20))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    bridge = CoreHardwareBridge(
        _settings(), _VOICE, controller, token="synthetic-token",
        rest=FakeRest(), connector=connector, sleep=sleep,
    )

    async def exercise() -> None:
        await bridge.start()
        while bridge.status()["running"]:
            await asyncio.sleep(0)
        await bridge.close()

    _run(exercise())
    assert attempts == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    status = bridge.status()
    assert status["running"] is False
    assert status["connected"] is False
    assert status["reconnect_failures"] == 8
    assert status["error"] == "PlayerError: Voice volume failed"


def test_play_media_observation_failure_uses_bounded_reconnect_and_safe_close() -> None:
    attempts = 0
    sleeps: list[float] = []
    controller = FakeController(fail_play_media_observation=True)

    @asynccontextmanager
    async def connector():
        nonlocal attempts
        attempts += 1
        yield FakeSocket(_play_media_observation_messages(f"owned-play-{attempts}"))

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    bridge = CoreHardwareBridge(
        _settings(),
        _VOICE,
        controller,
        token="synthetic-token",
        rest=FakeRest(),
        connector=connector,
        sleep=sleep,
    )

    async def exercise() -> None:
        await bridge.start()
        while bridge.status()["running"]:
            await asyncio.sleep(0)
        await bridge.close()

    _run(exercise())
    assert attempts == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    status = bridge.status()
    assert status["running"] is False
    assert status["connected"] is False
    assert status["reconnect_failures"] == 8
    assert status["error"] == "PlayerError: Transport provenance failed"


def test_short_authenticated_sessions_consume_bounded_reconnect_budget() -> None:
    attempts = 0
    sleeps: list[float] = []

    @asynccontextmanager
    async def connector():
        nonlocal attempts
        attempts += 1
        yield FakeSocket(_socket_messages())

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    bridge = CoreHardwareBridge(
        _settings(), _VOICE, FakeController(), token="synthetic-token",
        rest=FakeRest(), connector=connector, sleep=sleep,
    )

    async def exercise() -> None:
        await bridge.start()
        while bridge.status()["running"]:
            await asyncio.sleep(0)
        await bridge.close()

    _run(exercise())
    assert attempts == 8
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert bridge.status()["reconnect_failures"] == 8


def test_same_state_foreign_transport_event_blocks_hardware_gesture() -> None:
    controller = FakeController()
    processor = HardwareIntentProcessor(_settings(), _VOICE, controller, FakeRest())

    async def exercise() -> None:
        await _ready(processor)
        await processor.process_state(
            _VOICE,
            "playing",
            {
                "volume_level": 0.15,
                "media_content_id": "media-source://dlna_dms/foreign/:99",
            },
            live=True,
            event_key="foreign-replacement",
        )
        await processor.process_state(
            _BUTTON,
            "2026-09-29T01:00:05+00:00",
            {"event_type": "double_press"},
            live=True,
            event_key="blocked-gesture",
        )

    _run(exercise())
    assert ("skip", "next") not in controller.calls
