from __future__ import annotations

from pathlib import Path

import pytest

from bedside_audio.catalog import DlnaLibraryCatalog, DlnaMedia
from bedside_audio.config import PlaylistSettings
from bedside_audio.controller import ControlError, PlaybackController
from bedside_audio.persistence import SavedSession, StateStore

from .conftest import FakeClock, FakePlayer


class QueueCatalog:
    def health(self, relative: str | None = None) -> None:
        return None

    def prepare(self, relative: str):
        paths = ["a", "b", "c"]
        index = paths.index(relative)
        return (
            DlnaMedia(
                f"media-source://dlna_dms/calculon/:4{index}",
                "video/x-matroska", f"Episode {index}", f"DLNA / Episode {index}",
            ),
            paths, index,
        )

    def file(self, relative: str):
        return self.prepare(relative)[0]

    def queue(self, relative: str):
        return self.prepare(relative)[1:]

    def list_dir(self, relative: str = ""):
        return {"path": relative, "parent": None, "entries": []}

    def configured_queue(self, items: tuple[tuple[str, ...], ...]):
        assert items == (("Configured",),)
        return ["c", "a", "b"]

    def folder_queue(self, relative: str):
        assert relative == "folder"
        return ["a", "b", "c"], "DLNA / Example Show"


def test_saved_plex_item_never_appears_or_autoplays_in_dlna_only_remote(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state", 15)
    store.save(SavedSession("plex/nasA/7/12/21/31", 15, "Old Plex episode", "Plex / Old"))
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), player, store, max_volume=15,
    )

    state = controller.state()
    assert state["current"] is None
    assert "bedtime_active" not in state
    assert state["playback"] == "stopped"
    assert state["resume_available"] is False
    assert player.loads == []
    assert not hasattr(controller, "forget_plex")


def test_dlna_queue_supports_manual_skip_and_sleep_timer(tmp_path: Path) -> None:
    clock = FakeClock()
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()),
        player, StateStore(tmp_path / "state", 15),
        clock=clock, max_volume=15,
    )
    first = controller.play("dlna/a")
    assert first["queue"] == {
        "index": 0,
        "length": 3,
        "previous": False,
        "next": True,
        "kind": "folder",
        "name": None,
    }
    assert first["capabilities"]["auto_advance"] is True
    assert controller.skip("next")["current"]["path"] == "dlna/b"
    assert controller.skip("previous")["current"]["path"] == "dlna/a"
    assert len(player.loads) == 3

    controller.set_timer(1)
    clock.advance(60)
    with pytest.raises(ControlError, match="sleep timer expired"):
        controller.skip("next")
    assert controller.state()["timer_remaining"] is None
    assert controller.state()["playback"] == "stopped"
    assert len(player.loads) == 3


def test_configured_playlist_keeps_explicit_queue_across_transport_actions(
    tmp_path: Path,
) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()),
        player,
        StateStore(tmp_path / "state", 15),
        max_volume=15,
        playlists=(PlaylistSettings("Quiet evening", (("Configured",),)),),
    )

    started = controller.play_playlist("Quiet evening")
    assert started["current"]["path"] == "dlna/c"
    assert started["queue"] == {
        "index": 0,
        "length": 3,
        "previous": False,
        "next": True,
        "kind": "playlist",
        "name": "Quiet evening",
    }
    assert controller.skip("next")["current"]["path"] == "dlna/a"
    restarted = controller.restart_current()
    assert restarted["current"]["path"] == "dlna/a"
    assert restarted["queue"]["kind"] == "playlist"
    assert restarted["queue"]["index"] == 1
    assert [media.name for media in player.loads] == [
        "Episode 2",
        "Episode 0",
        "Episode 0",
    ]

    with pytest.raises(ControlError, match="not found"):
        controller.play_playlist("Missing")


def test_explicit_folder_shuffle_is_fresh_and_never_reshuffles_on_skip(
    tmp_path: Path,
) -> None:
    orders: list[list[str]] = []

    def shuffle(queue: list[str]) -> None:
        if not orders:
            queue.reverse()
        else:
            queue.append(queue.pop(0))
        orders.append(list(queue))

    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()),
        FakePlayer(),
        StateStore(tmp_path / "state", 15),
        max_volume=15,
        queue_shuffler=shuffle,
    )

    first = controller.shuffle_folder("dlna/folder")
    assert first["current"]["path"] == "dlna/c"
    assert first["queue"]["kind"] == "shuffle"
    assert first["queue"]["name"] == "DLNA / Example Show"
    assert orders == [["dlna/c", "dlna/b", "dlna/a"]]

    assert controller.skip("next")["current"]["path"] == "dlna/b"
    assert controller.state()["current"]["path"] == "dlna/b"
    assert controller.restart_current()["current"]["path"] == "dlna/b"
    assert orders == [["dlna/c", "dlna/b", "dlna/a"]]

    second = controller.shuffle_folder("dlna/folder")
    assert second["current"]["path"] == "dlna/b"
    assert orders == [
        ["dlna/c", "dlna/b", "dlna/a"],
        ["dlna/b", "dlna/c", "dlna/a"],
    ]


def test_configured_volume_cap_does_not_replace_saved_volume(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state", 15)
    store.save(SavedSession(None, 23))
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), player, store, max_volume=50,
    )

    state = controller.state()
    assert state["volume"] == 23
    assert state["capabilities"]["max_volume"] == 50
    controller.play("dlna/a")
    assert player.volume == 23


def test_playback_error_requires_explicit_stop_before_another_play(tmp_path: Path) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), player,
        StateStore(tmp_path / "state", 15), max_volume=50,
    )
    controller.play("dlna/a")
    player.error = "decoder failed"
    assert controller.state()["playback"] == "error"

    with pytest.raises(ControlError, match="Stop Voice"):
        controller.play("dlna/b")

    assert controller.stop()["playback"] == "stopped"
    assert controller.play("dlna/b")["current"]["path"] == "dlna/b"


def test_controller_tracks_only_bedside_owned_monotonic_position(tmp_path: Path) -> None:
    clock = FakeClock()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), FakePlayer(),
        StateStore(tmp_path / "state", 15), clock=clock, max_volume=50,
    )

    stopped = controller.state()
    assert stopped["position"] is None
    assert stopped["owns_transport"] is False

    started = controller.play("dlna/a")
    assert started["position"] == 0.0
    assert started["owns_transport"] is True
    generation = started["transport_generation"]
    clock.advance(8)
    assert controller.state()["position"] == 8.0

    controller.toggle_pause()
    clock.advance(5)
    paused = controller.state()
    assert paused["position"] == 8.0
    assert paused["transport_generation"] == generation

    controller.toggle_pause()
    clock.advance(3)
    assert controller.state()["position"] == 11.0
    restarted = controller.restart_current()
    assert restarted["current"]["path"] == "dlna/a"
    assert restarted["position"] == 0.0
    assert restarted["transport_generation"] == generation + 1

    controller.stop()
    assert controller.state()["position"] is None
    assert controller.state()["owns_transport"] is False


def test_controller_position_does_not_count_initial_buffering_time(tmp_path: Path) -> None:
    clock = FakeClock()
    player = FakePlayer()
    player.starting = True
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), player,
        StateStore(tmp_path / "state", 15), clock=clock, max_volume=50,
    )

    assert controller.play("dlna/a")["position"] == 0.0
    clock.advance(12)
    assert controller.state()["position"] == 0.0

    player.starting = False
    clock.advance(1)
    assert controller.state()["position"] == 0.0
    clock.advance(3)
    assert controller.state()["position"] == 3.0


def test_foreign_transport_identity_revokes_bedside_hardware_ownership(
    tmp_path: Path,
) -> None:
    player = FakePlayer()
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), player,
        StateStore(tmp_path / "state", 15), max_volume=50,
    )
    controller.play("dlna/a")

    controller.observe_transport(
        "playing",
        {"media_content_id": player.media_content_id},
        live=True,
    )
    assert controller.state()["owns_transport"] is True

    controller.observe_transport(
        "playing",
        {"media_content_id": "media-source://dlna_dms/other/:99"},
        live=True,
    )
    state = controller.state()
    assert state["owns_transport"] is False
    assert state["position"] is None


def test_unverifiable_reconnect_snapshot_revokes_hardware_ownership(
    tmp_path: Path,
) -> None:
    controller = PlaybackController(
        DlnaLibraryCatalog(QueueCatalog()), FakePlayer(),
        StateStore(tmp_path / "state", 15), max_volume=50,
    )
    controller.play("dlna/a")

    controller.observe_transport("playing", {}, live=False)

    assert controller.state()["owns_transport"] is False
