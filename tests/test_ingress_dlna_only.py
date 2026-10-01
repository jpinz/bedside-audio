from __future__ import annotations

import json
import logging
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from bedside_audio.app import create_app
from bedside_audio.catalog import DlnaMedia
from bedside_audio.config import (
    DlnaSettings,
    HardwareBridgeSettings,
    PlaylistSettings,
    Settings,
    VoiceSettings,
)
from bedside_audio.owner import OwnerError
from bedside_audio.persistence import SavedSession, StateStore

from .conftest import FakePlayer

_OWNER = "ha-user-123"
_VOICE = "media_player.bedroom_voice"
_TV = "media_player.example_tv"
_PREFIX = "media-source://dlna_dms/plex_media_server_example/:"
_INGRESS = {
    "X-Remote-User-Id": _OWNER,
    "X-Ingress-Path": "/api/hassio_ingress/session123",
}
_CONTROL = {"X-Bedside-Control": "1", "Origin": "https://homeassistant.local"}


def _settings(
    tmp_path: Path,
    owner: str | None = None,
    playlists: tuple[PlaylistSettings, ...] = (),
    allowed_folders: tuple[tuple[str, ...], ...] = (),
    excluded_title_patterns: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        voice=VoiceSettings(_VOICE),
        dlna=DlnaSettings(
            "plex_media_server_example",
            _TV,
            owner,
            allowed_folders,
            excluded_title_patterns,
        ),
        playlists=playlists,
    )


def _legacy_owner(state_dir: Path, *, user: str = _OWNER) -> bytes:
    state_dir.mkdir(mode=0o700)
    content = json.dumps({
        "account_id": 42,
        "title": "Example listener",
        "token": "synthetic-invalid-pms-token",
        "ha_user_id": user,
        "token_kind": "jwt",
    }).encode()
    path = state_dir / "plex_auth.json"
    path.write_bytes(content)
    path.chmod(0o600)
    return content


class Browser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def browse_media(self, entity_id: str, media_content_id: str, media_content_type: str):
        self.calls.append((entity_id, media_content_id, media_content_type))
        assert entity_id == _TV
        assert media_content_id == _PREFIX + "0"
        assert media_content_type == "object.container.storageFolder"
        return {
            "media_content_id": media_content_id,
            "media_content_type": media_content_type,
            "title": "TV library",
            "media_class": "directory",
            "can_expand": True,
            "can_play": False,
            "not_shown": 0,
            "children": [{
                "media_content_id": _PREFIX + "42",
                "media_content_type": "video/x-matroska",
                "title": "Episode",
                "media_class": "episode",
                "can_expand": False,
                "can_play": True,
            }],
        }


class PlaylistBrowser:
    def __init__(self) -> None:
        self.show_id = _PREFIX + "show"
        self.season_id = _PREFIX + "show$season1"
        self.first_id = _PREFIX + "show$season1$1"
        self.second_id = _PREFIX + "show$season1$2"

    @staticmethod
    def _folder(media_id: str, title: str, children: list[dict[str, object]]):
        return {
            "media_content_id": media_id,
            "media_content_type": "object.container.storageFolder",
            "title": title,
            "media_class": "directory",
            "can_expand": True,
            "can_play": False,
            "not_shown": 0,
            "children": children,
        }

    @staticmethod
    def _video(media_id: str, title: str):
        return {
            "media_content_id": media_id,
            "media_content_type": "video/x-matroska",
            "title": title,
            "media_class": "episode",
            "can_expand": False,
            "can_play": True,
        }

    def browse_media(
        self, entity_id: str, media_content_id: str, media_content_type: str,
    ):
        assert entity_id == _TV
        assert media_content_type == "object.container.storageFolder"
        if media_content_id == _PREFIX + "0":
            return self._folder(
                media_content_id,
                "TV library",
                [{
                    "media_content_id": self.show_id,
                    "media_content_type": "object.container.storageFolder",
                    "title": "Example Show",
                    "media_class": "directory",
                    "can_expand": True,
                    "can_play": False,
                }],
            )
        if media_content_id == self.show_id:
            return self._folder(
                media_content_id,
                "Example Show",
                [{
                    "media_content_id": self.season_id,
                    "media_content_type": "object.container.storageFolder",
                    "title": "Season 1",
                    "media_class": "directory",
                    "can_expand": True,
                    "can_play": False,
                }],
            )
        if media_content_id == self.season_id:
            return self._folder(
                media_content_id,
                "Season 1",
                [
                    self._video(self.first_id, "Episode 1"),
                    self._video(self.second_id, "Episode 2"),
                ],
            )
        raise AssertionError(f"Unexpected browse ID {media_content_id}")


class FilteredLibraryBrowser:
    def __init__(self) -> None:
        self.video_id = _PREFIX + "video"
        self.photos_id = _PREFIX + "photos"
        self.movies_id = _PREFIX + "video$movies"
        self.tv_id = _PREFIX + "video$tv"
        self.allowed_id = _PREFIX + "video$tv$allowed"
        self.hidden_id = _PREFIX + "video$tv$hidden"
        self.specials_id = _PREFIX + "video$tv$allowed$specials"
        self.season_zero_id = _PREFIX + "video$tv$allowed$season0"
        self.season_id = _PREFIX + "video$tv$allowed$season"
        self.episode_id = _PREFIX + "video$tv$allowed$season$episode"

    @staticmethod
    def _folder(media_id: str, title: str, children: list[dict[str, object]]):
        return {
            "media_content_id": media_id,
            "media_content_type": "object.container.storageFolder",
            "title": title,
            "media_class": "directory",
            "can_expand": True,
            "can_play": False,
            "not_shown": 0,
            "children": children,
        }

    @staticmethod
    def _folder_entry(media_id: str, title: str):
        return {
            "media_content_id": media_id,
            "media_content_type": "object.container.storageFolder",
            "title": title,
            "media_class": "directory",
            "can_expand": True,
            "can_play": False,
        }

    def browse_media(
        self, entity_id: str, media_content_id: str, media_content_type: str,
    ):
        assert entity_id == _TV
        assert media_content_type == "object.container.storageFolder"
        if media_content_id == _PREFIX + "0":
            return self._folder(
                media_content_id,
                "Library",
                [
                    self._folder_entry(self.video_id, "Video"),
                    self._folder_entry(self.photos_id, "Photos"),
                ],
            )
        if media_content_id == self.video_id:
            return self._folder(
                media_content_id,
                "Video",
                [
                    self._folder_entry(self.movies_id, "Movies"),
                    self._folder_entry(self.tv_id, "TV Shows"),
                ],
            )
        if media_content_id == self.tv_id:
            return self._folder(
                media_content_id,
                "TV Shows",
                [
                    self._folder_entry(self.allowed_id, "Example Show"),
                    self._folder_entry(self.hidden_id, "Hidden Show"),
                ],
            )
        if media_content_id == self.allowed_id:
            return self._folder(
                media_content_id,
                "Example Show",
                [
                    self._folder_entry(self.specials_id, "Specials"),
                    self._folder_entry(self.season_zero_id, "Season 0"),
                    self._folder_entry(self.season_id, "Season 1"),
                ],
            )
        if media_content_id == self.season_id:
            return self._folder(
                media_content_id,
                "Season 1",
                [{
                    "media_content_id": self.episode_id,
                    "media_content_type": "video/x-matroska",
                    "title": "Episode 1",
                    "media_class": "episode",
                    "can_expand": False,
                    "can_play": True,
                }],
            )
        raise AssertionError(f"Unexpected browse ID {media_content_id}")


def _post(client: TestClient, path: str, body: dict | None = None):
    return client.post(path, json=body or {}, headers=_CONTROL)


class FailedBridge:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "running": False,
            "connected": False,
            "reconnect_failures": 8,
            "error": "Core unavailable",
        }


def test_legacy_ha_owner_migrates_without_exposing_plex_or_starting_audio(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    original = _legacy_owner(settings.state_dir)
    browser = Browser()
    player = FakePlayer()
    app = create_app(settings, player, run_worker=False, dlna_browser=browser)
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="plex-connect"' not in page.text
        assert 'id="plex-migration"' not in page.text
        assert 'id="seek"' not in page.text
        status = client.get("/api/owner/status").json()
        assert status == {
            "owner_access": True,
            "dlna_enabled": True,
            "legacy_credentials_present": True,
            "legacy_cleanup_ready": False,
        }
        assert "synthetic-invalid-pms-token" not in str(status)
        assert client.get("/api/library").json()["entries"] == [
            {"path": "dlna", "name": "TV library", "kind": "folder"},
        ]
        assert client.get("/api/library", params={"path": "plex"}).status_code == 400
        assert _post(client, "/api/plex/login").status_code == 404
        assert _post(client, "/api/owner/cleanup-legacy", {
            "confirm": "delete-local-plex-credentials",
        }).status_code == 409
        listing = client.get("/api/library", params={"path": "dlna"}).json()
        assert listing["folder_queue"] is True
        assert len(listing["entries"]) == 1
        assert _PREFIX not in json.dumps(listing)
        assert client.get("/api/owner/status").json()["legacy_cleanup_ready"] is False
        assert player.loads == []
        assert "bedtime_active" not in client.get("/api/state").json()

    owner_file = settings.state_dir / "owner.json"
    assert json.loads(owner_file.read_text(encoding="utf-8")) == {
        "version": 1, "ha_user_id": _OWNER,
    }
    assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    assert (settings.state_dir / "plex_auth.json").read_bytes() == original
    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as restarted:
        assert restarted.get("/api/owner/status").json()["legacy_cleanup_ready"] is False
        assert restarted.get("/api/library", params={"path": "dlna"}).status_code == 200
        assert restarted.get("/api/owner/status").json()["legacy_cleanup_ready"] is True


def test_ingress_owner_gate_rejects_other_user_and_csrf_without_browsing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    browser = Browser()
    app = create_app(settings, FakePlayer(), run_worker=False, dlna_browser=browser)
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        headers = {"X-Remote-User-Id": "other-user"}
        assert client.get("/api/owner/status", headers=headers).json() == {
            "owner_access": False,
            "dlna_enabled": True,
            "legacy_credentials_present": False,
            "legacy_cleanup_ready": False,
        }
        assert client.get("/api/library", headers=headers).status_code == 403
        assert client.get("/api/state", headers=headers).status_code == 403
        assert client.post(
            "/api/play", json={"path": "dlna/opaque"}, headers={**_CONTROL, **headers},
        ).status_code == 403
        assert client.post("/api/play", json={"path": "dlna/opaque"}).status_code == 403
        assert client.post(
            "/api/play", json={"path": "dlna/opaque"},
            headers={**_CONTROL, "Sec-Fetch-Site": "cross-site"},
        ).status_code == 403
        direct = TestClient(
            app, base_url="https://homeassistant.local",
            client=("127.0.0.1", 55000), headers=_INGRESS,
        )
        assert direct.get("/api/owner/status").status_code == 403
        assert browser.calls == []


def test_hardware_bridge_failure_is_isolated_and_ingress_security_is_unchanged(
    tmp_path: Path,
) -> None:
    base = _settings(tmp_path, _OWNER)
    settings = Settings(
        state_dir=base.state_dir,
        voice=base.voice,
        dlna=base.dlna,
        hardware=HardwareBridgeSettings(
            button_event_entity="event.bedroom_voice_button",
            assist_satellite_entity="assist_satellite.bedroom_voice",
            led_light_entity="light.bedroom_voice_ring",
        ),
    )
    bridge = FailedBridge()
    app = create_app(
        settings,
        FakePlayer(),
        run_worker=False,
        dlna_browser=Browser(),
        hardware_bridge_factory=lambda controller: bridge,
    )
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["hardware_bridge"]["error"] == "Core unavailable"
        assert client.post("/api/toggle-pause").status_code == 403
        direct = TestClient(
            app, base_url="https://homeassistant.local",
            client=("127.0.0.1", 55000), headers=_INGRESS,
        )
        assert direct.get("/api/state").status_code == 403
    assert bridge.started == 1
    assert bridge.closed == 1


def test_missing_bridge_token_warns_and_leaves_web_remote_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    base = _settings(tmp_path, _OWNER)
    settings = Settings(
        state_dir=base.state_dir,
        voice=base.voice,
        dlna=base.dlna,
        hardware=HardwareBridgeSettings(
            button_event_entity="event.bedroom_voice_button",
            assist_satellite_entity="assist_satellite.bedroom_voice",
            led_light_entity="light.bedroom_voice_ring",
        ),
    )
    with caplog.at_level(logging.WARNING), TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["hardware_bridge"]["enabled"] is False
        assert "Supervisor token is unavailable" in state.json()["hardware_bridge"]["error"]
    assert "Voice hardware bridge is disabled" in caplog.text


def test_dlna_play_uses_no_legacy_plex_token_or_automatic_start(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    player = FakePlayer()
    app = create_app(settings, player, run_worker=False, dlna_browser=Browser())
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        episode = client.get("/api/library", params={"path": "dlna"}).json()["entries"][0]
        playing = _post(client, "/api/play", {"path": episode["path"]})
        assert playing.status_code == 200
        assert playing.json()["queue"]["length"] == 1
        assert playing.json()["capabilities"]["auto_advance"] is True
        assert isinstance(player.loads[-1], DlnaMedia)
        assert player.loads[-1].media_content_id == _PREFIX + "42"
        assert "synthetic-invalid-pms-token" not in playing.text
        assert "synthetic-invalid-pms-token" not in repr(player.loads[-1])
        assert _post(client, "/api/volume", {"volume": 51}).status_code == 422
        assert _post(client, "/api/stop").status_code == 200
        assert player.active is False
        assert len(player.loads) == 1
        assert _post(client, "/api/bedtime/start").status_code == 404


def test_library_allowlist_surfaces_only_configured_show_and_descendants(
    tmp_path: Path,
) -> None:
    browser = FilteredLibraryBrowser()
    settings = _settings(
        tmp_path,
        _OWNER,
        allowed_folders=(("Video", "TV Shows", "Example Show"),),
        excluded_title_patterns=("specials", "Season 0*"),
    )
    app = create_app(
        settings,
        FakePlayer(),
        run_worker=False,
        dlna_browser=browser,
    )
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        video = client.get(
            "/api/library", params={"path": "dlna"},
        ).json()["entries"]
        assert [(entry["name"], entry["kind"]) for entry in video] == [
            ("Video", "folder"),
        ]

        tv_shows = client.get(
            "/api/library", params={"path": video[0]["path"]},
        ).json()["entries"]
        assert [entry["name"] for entry in tv_shows] == ["TV Shows"]

        shows = client.get(
            "/api/library", params={"path": tv_shows[0]["path"]},
        ).json()["entries"]
        assert [entry["name"] for entry in shows] == ["Example Show"]

        seasons = client.get(
            "/api/library", params={"path": shows[0]["path"]},
        ).json()["entries"]
        assert [entry["name"] for entry in seasons] == ["Season 1"]

        episodes = client.get(
            "/api/library", params={"path": seasons[0]["path"]},
        ).json()["entries"]
        assert [entry["name"] for entry in episodes] == ["Episode 1"]


def test_playlist_and_folder_shuffle_routes_keep_owner_and_csrf_protection(
    tmp_path: Path,
) -> None:
    browser = PlaylistBrowser()
    settings = _settings(
        tmp_path,
        _OWNER,
        playlists=(
            PlaylistSettings(
                "Quiet evening",
                (("Example Show", "Season 1"),),
            ),
        ),
    )
    player = FakePlayer()
    app = create_app(
        settings,
        player,
        run_worker=False,
        dlna_browser=browser,
        queue_shuffler=lambda queue: queue.reverse(),
    )
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        assert client.get("/api/playlists").json() == {
            "playlists": [{"name": "Quiet evening"}],
        }
        assert client.get(
            "/api/playlists",
            headers={"X-Remote-User-Id": "other-user"},
        ).status_code == 403
        assert client.post(
            "/api/playlists/play",
            json={"name": "Quiet evening"},
        ).status_code == 403

        playing = _post(
            client,
            "/api/playlists/play",
            {"name": "Quiet evening"},
        )
        assert playing.status_code == 200
        assert playing.json()["queue"]["kind"] == "playlist"
        assert playing.json()["queue"]["name"] == "Quiet evening"
        assert player.loads[-1].media_content_id == browser.first_id
        assert _post(
            client,
            "/api/playlists/play",
            {"name": "Missing"},
        ).status_code == 404

        show = client.get(
            "/api/library", params={"path": "dlna"},
        ).json()["entries"][0]
        shuffled = _post(client, "/api/shuffle", {"path": show["path"]})
        assert shuffled.status_code == 200
        assert shuffled.json()["queue"]["kind"] == "shuffle"
        assert shuffled.json()["queue"]["length"] == 2
        assert player.loads[-1].media_content_id == browser.second_id
        assert _PREFIX not in shuffled.text

        denied = client.post(
            "/api/shuffle",
            json={"path": show["path"]},
            headers={**_CONTROL, "Sec-Fetch-Site": "cross-site"},
        )
        assert denied.status_code == 403


def test_owner_bound_cleanup_only_after_successful_dlna_browse(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    for name in (
        "plex_pin.json", "plex-client-id",
        "plex-device-private.key", "plex-device-public.key",
    ):
        (settings.state_dir / name).write_text("synthetic-secret", encoding="utf-8")
        (settings.state_dir / name).chmod(0o600)
    untouched = settings.state_dir / "session.json"
    StateStore(settings.state_dir, 15).save(SavedSession(None, 15))
    app = create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser())
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        assert _post(client, "/api/owner/cleanup-legacy").status_code == 422
        assert _post(client, "/api/owner/cleanup-legacy", {
            "confirm": "delete-local-plex-credentials",
        }).status_code == 409
        assert client.get("/api/library", params={"path": "dlna"}).status_code == 200
        assert client.get("/api/owner/status").json()["legacy_cleanup_ready"] is False

    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        assert client.get("/api/owner/status").json()["legacy_cleanup_ready"] is False
        assert client.get("/api/library", params={"path": "dlna"}).status_code == 200
        assert client.get("/api/owner/status").json()["legacy_cleanup_ready"] is True
        result = _post(client, "/api/owner/cleanup-legacy", {
            "confirm": "delete-local-plex-credentials",
        })
        assert result.status_code == 200
        assert result.json() == {"deleted_files": 5, "plex_revocation": "not_attempted"}
        assert _post(client, "/api/owner/cleanup-legacy", {
            "confirm": "delete-local-plex-credentials",
        }).json()["deleted_files"] == 0
        assert client.get("/api/owner/status").json()["legacy_credentials_present"] is False
    assert json.loads((settings.state_dir / "owner.json").read_text())["ha_user_id"] == _OWNER
    assert untouched.exists()


def test_fresh_dlna_install_needs_an_explicit_owner_before_ingress(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(OwnerError):
        with TestClient(
            create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        ):
            pass
    assert not (settings.state_dir / "owner.json").exists()

    configured = _settings(tmp_path, owner=_OWNER)
    with TestClient(
        create_app(configured, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        status = client.get("/api/owner/status").json()
        assert status["owner_access"] is True
        assert status["legacy_credentials_present"] is False
        assert client.get(
            "/api/owner/status", headers={"X-Remote-User-Id": "first-visitor"},
        ).json()["owner_access"] is False
        assert client.get(
            "/api/library", headers={"X-Remote-User-Id": "first-visitor"},
        ).status_code == 403


def test_corrupt_legacy_owner_cannot_be_overridden_by_option(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, owner=_OWNER)
    settings.state_dir.mkdir(mode=0o700)
    legacy = settings.state_dir / "plex_auth.json"
    legacy.write_text('{"token":"synthetic-secret", broken', encoding="utf-8")
    legacy.chmod(0o600)
    player = FakePlayer()

    with pytest.raises(OwnerError):
        with TestClient(
            create_app(settings, player, run_worker=False, dlna_browser=Browser()),
        ):
            pass
    assert not (settings.state_dir / "owner.json").exists()
    assert player.loads == []


def test_owner_state_lost_after_startup_returns_safe_503_not_a_traceback(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    player = FakePlayer()
    app = create_app(settings, player, run_worker=False, dlna_browser=Browser())
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        (settings.state_dir / "owner.json").unlink()
        owner_status = client.get("/api/owner/status")
        assert owner_status.status_code == 503
        denied = _post(client, "/api/play", {"path": "dlna/opaque-video"})
        assert denied.status_code == 503
        assert "synthetic-invalid-pms-token" not in denied.text
        assert player.loads == []


def test_owner_guard_loss_after_startup_denies_ingress_controls(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    player = FakePlayer()
    with TestClient(
        create_app(settings, player, run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        (settings.state_dir / "owner-bound.json").unlink()
        assert client.get("/api/owner/status").status_code == 503
        assert _post(client, "/api/play", {"path": "dlna/opaque-video"}).status_code == 503
        assert player.loads == []


def test_deleted_legacy_credentials_cannot_enable_a_new_owner_claim(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    for _ in range(2):
        with TestClient(
            create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
            base_url="https://homeassistant.local",
            client=("172.30.32.2", 55000), headers=_INGRESS,
        ) as client:
            assert client.get("/api/library", params={"path": "dlna"}).status_code == 200
            if client.get("/api/owner/status").json()["legacy_cleanup_ready"]:
                assert _post(client, "/api/owner/cleanup-legacy", {
                    "confirm": "delete-local-plex-credentials",
                }).status_code == 200
    assert not (settings.state_dir / "plex_auth.json").exists()
    owner_path = settings.state_dir / "owner.json"
    assert owner_path.exists()
    owner_path.unlink()

    different = _settings(tmp_path, owner="different-ha-user")
    with pytest.raises(OwnerError):
        with TestClient(
            create_app(different, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        ):
            pass
    assert not owner_path.exists()


def test_legacy_owner_staging_blocks_ingress_cleanup_without_deleting_files(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
    ):
        pass
    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
        base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        assert client.get("/api/library", params={"path": "dlna"}).status_code == 200
        staging = settings.state_dir / (".plex-auth-" + "a" * 32)
        staging.write_bytes(b"synthetic-private-staging")
        status = client.get("/api/owner/status")
        assert status.status_code == 503
        cleanup = _post(client, "/api/owner/cleanup-legacy", {
            "confirm": "delete-local-plex-credentials",
        })
        assert cleanup.status_code == 503
        assert "synthetic-private-staging" not in cleanup.text
        assert (settings.state_dir / "plex_auth.json").exists()
        assert staging.exists()


def test_cleanup_serializes_concurrent_play_until_deletion_finishes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
    ):
        pass
    player = FakePlayer()
    app = create_app(settings, player, run_worker=False, dlna_browser=Browser())
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        item = client.get("/api/library", params={"path": "dlna"}).json()["entries"][0]
        entered, release, finished, started = Event(), Event(), Event(), Event()
        original_cleanup = client.app.state.owner.cleanup_legacy

        def slow_cleanup(ha_user_id: str, *, browse_verified: bool):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic cleanup did not resume")
            result = original_cleanup(ha_user_id, browse_verified=browse_verified)
            finished.set()
            return result

        client.app.state.owner.cleanup_legacy = slow_cleanup

        def play():
            started.set()
            return _post(client, "/api/play", {"path": item["path"]})

        with ThreadPoolExecutor(max_workers=2) as workers:
            cleaning = workers.submit(_post, client, "/api/owner/cleanup-legacy", {
                "confirm": "delete-local-plex-credentials",
            })
            assert entered.wait(2)
            controls = workers.submit(play)
            try:
                assert started.wait(2)
                time.sleep(0.2)
                assert not controls.done()
                assert player.loads == []
            finally:
                release.set()
            assert cleaning.result(timeout=5).status_code == 200
            assert controls.result(timeout=5).status_code == 200
            assert finished.is_set()
            assert len(player.loads) == 1


def test_stop_and_timer_remain_responsive_during_legacy_cleanup(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _legacy_owner(settings.state_dir)
    with TestClient(
        create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser()),
    ):
        pass
    app = create_app(settings, FakePlayer(), run_worker=False, dlna_browser=Browser())
    with TestClient(
        app, base_url="https://homeassistant.local",
        client=("172.30.32.2", 55000), headers=_INGRESS,
    ) as client:
        assert client.get("/api/library", params={"path": "dlna"}).status_code == 200
        entered, release = Event(), Event()
        original_cleanup = client.app.state.owner.cleanup_legacy

        def slow_cleanup(ha_user_id: str, *, browse_verified: bool):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic cleanup did not resume")
            return original_cleanup(ha_user_id, browse_verified=browse_verified)

        client.app.state.owner.cleanup_legacy = slow_cleanup
        with ThreadPoolExecutor(max_workers=3) as workers:
            cleaning = workers.submit(_post, client, "/api/owner/cleanup-legacy", {
                "confirm": "delete-local-plex-credentials",
            })
            try:
                assert entered.wait(2)
                assert workers.submit(_post, client, "/api/stop").result(timeout=1).status_code == 200
                assert workers.submit(_post, client, "/api/timer", {
                    "minutes": 1,
                }).result(timeout=1).status_code == 200
            finally:
                release.set()
            assert cleaning.result(timeout=5).status_code == 200
