from __future__ import annotations

from pathlib import Path

import pytest
import requests

from bedside_audio.catalog import DlnaLibraryCatalog, DlnaMedia
from bedside_audio.controller import PlaybackController
from bedside_audio.ha_voice import HomeAssistantClient, VoicePlayer, VoicePlayerError
from bedside_audio.persistence import StateStore

from .conftest import FakeClock

_VOICE = "media_player.bedroom_voice"
_BROWSE_TV = "media_player.example_tv"


class Response:
    def __init__(self, value: object, status_code: int = 200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return self.value

    def close(self):
        pass


class CoreSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.player_state = "idle"
        self.actions: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs):
        assert url.startswith("http://supervisor/core/api/")
        assert kwargs["allow_redirects"] is False
        if method == "GET":
            assert url.endswith(f"/states/{_VOICE}")
            return Response({
                "entity_id": _VOICE, "state": self.player_state, "attributes": {},
            })
        assert method == "POST"
        action = url.rsplit("/", 1)[-1]
        assert action in {"volume_set", "play_media", "media_stop"}
        self.actions.append((action, kwargs["json"]))
        if action == "media_stop":
            self.player_state = "idle"
        return Response([])


def test_dlna_only_voice_plays_exact_core_id_then_stops_without_startup_audio() -> None:
    session = CoreSession()
    player = VoicePlayer(HomeAssistantClient("synthetic-token", session=session), _VOICE, 15, 50)
    item = DlnaMedia(
        "media-source://dlna_dms/calculon/:42", "video/x-matroska",
        "Episode", "DLNA / TV Shows / Episode",
    )

    assert session.actions == []
    player.load(item)
    assert session.actions == [
        ("volume_set", {"entity_id": _VOICE, "volume_level": 0.15}),
        ("play_media", {
            "entity_id": _VOICE,
            "media_content_id": "media-source://dlna_dms/calculon/:42",
            "media_content_type": "video/x-matroska",
        }),
    ]
    assert all(action["entity_id"] != _BROWSE_TV for _, action in session.actions)
    session.player_state = "playing"
    assert player.snapshot().active is True
    player.stop()
    assert session.actions[-1] == ("media_stop", {"entity_id": _VOICE})
    assert player.snapshot().active is False


def test_core_browse_uses_response_only_service_for_the_video_browse_entity() -> None:
    class BrowseSession(CoreSession):
        def request(self, method: str, url: str, **kwargs):
            if url.endswith("/media_player/browse_media?return_response"):
                assert method == "POST"
                assert kwargs["allow_redirects"] is False
                assert kwargs["timeout"] == (2.0, 5.0)
                assert kwargs["json"] == {
                    "entity_id": _BROWSE_TV,
                    "media_content_id": "media-source://dlna_dms/calculon/:0",
                    "media_content_type": "object.container.storageFolder",
                }
                return Response({
                    "changed_states": [],
                    "service_response": {
                        _BROWSE_TV: {
                            "media_content_id": "media-source://dlna_dms/calculon/:0",
                            "media_content_type": "object.container.storageFolder",
                            "title": "TV library",
                            "can_expand": True,
                            "can_play": False,
                            "children": [],
                        },
                    },
                })
            return super().request(method, url, **kwargs)

    session = BrowseSession()
    client = HomeAssistantClient("synthetic-supervisor-token", session=session)
    result = client.browse_media(
        _BROWSE_TV, "media-source://dlna_dms/calculon/:0",
        "object.container.storageFolder",
    )
    assert result["title"] == "TV library"
    assert session.actions == []
    assert session.trust_env is False
    assert "synthetic-supervisor-token" not in repr(client)


def test_unconfirmed_voice_stop_blocks_replay_until_retry() -> None:
    class FailingStop(CoreSession):
        fail_stop = True

        def request(self, method: str, url: str, **kwargs):
            if url.endswith("/media_stop") and self.fail_stop:
                self.actions.append(("media_stop", kwargs["json"]))
                return Response({"detail": "private-media-url"}, status_code=502)
            return super().request(method, url, **kwargs)

    session = FailingStop()
    player = VoicePlayer(HomeAssistantClient("synthetic-token", session=session), _VOICE, 15, 50)
    item = DlnaMedia(
        "media-source://dlna_dms/calculon/:42", "video/x-matroska",
        "Episode", "DLNA / TV Shows / Episode",
    )
    player.load(item)
    with pytest.raises(VoicePlayerError, match="Voice stop was not confirmed") as error:
        player.stop()
    assert "private-media-url" not in str(error.value)
    with pytest.raises(VoicePlayerError, match="Retry stopping Voice"):
        player.load(item)
    assert [action for action, _ in session.actions].count("play_media") == 1
    session.fail_stop = False
    player.stop()
    player.load(item)
    assert [action for action, _ in session.actions].count("play_media") == 2


def test_timer_stops_dlna_voice_without_advancing_queue(tmp_path: Path) -> None:
    clock = FakeClock()
    session = CoreSession()
    player = VoicePlayer(
        HomeAssistantClient("synthetic-token", session=session),
        _VOICE, 15, 50, clock=clock,
    )

    class Queue:
        def health(self, relative: str | None = None) -> None:
            return None

        def prepare(self, relative: str):
            assert relative in ("a", "b")
            return (
                DlnaMedia(
                    f"media-source://dlna_dms/calculon/:4{relative}",
                    "video/x-matroska", relative, f"DLNA / {relative}",
                ),
                ["a", "b"], 0 if relative == "a" else 1,
            )

    controller = PlaybackController(
        DlnaLibraryCatalog(Queue()), player,
        StateStore(tmp_path / "state", 15), clock=clock, max_volume=50,
    )
    controller.play("dlna/a")
    controller.set_timer(1)
    clock.advance(60)
    controller.tick()
    assert controller.state()["playback"] == "stopped"
    assert controller.state()["timer_remaining"] is None
    assert [action for action, _ in session.actions].count("play_media") == 1
    assert session.actions[-1] == ("media_stop", {"entity_id": _VOICE})


def test_completed_dlna_voice_playback_advances_queue(tmp_path: Path) -> None:
    class CompletedSession(CoreSession):
        def request(self, method: str, url: str, **kwargs):
            if url.endswith("/media_stop"):
                return Response({"detail": "already idle"}, status_code=502)
            return super().request(method, url, **kwargs)

    clock = FakeClock()
    session = CompletedSession()
    player = VoicePlayer(
        HomeAssistantClient("synthetic-token", session=session),
        _VOICE, 15, 50, clock=clock,
    )

    class Queue:
        def health(self, relative: str | None = None) -> None:
            return None

        def prepare(self, relative: str):
            assert relative in ("a", "b")
            return (
                DlnaMedia(
                    f"media-source://dlna_dms/calculon/:4{relative}",
                    "video/x-matroska", relative, f"DLNA / {relative}",
                ),
                ["a", "b"], 0 if relative == "a" else 1,
            )

        def file(self, relative: str):
            return self.prepare(relative)[0]

    controller = PlaybackController(
        DlnaLibraryCatalog(Queue()), player,
        StateStore(tmp_path / "state", 15), clock=clock, max_volume=50,
    )
    controller.play("dlna/a")
    session.player_state = "playing"
    assert controller.state()["playback"] == "playing"

    session.player_state = "idle"
    assert controller.state()["playback"] == "buffering"
    clock.advance(3)
    state = controller.state()

    assert state["current"]["path"] == "dlna/b"
    assert state["queue"]["index"] == 1
    assert state["playback"] == "playing"
    assert state["capabilities"]["auto_advance"] is True
    assert [action for action, _ in session.actions].count("play_media") == 2
    assert [action for action, _ in session.actions].count("media_stop") == 0
