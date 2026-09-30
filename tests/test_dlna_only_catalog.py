from __future__ import annotations

import json

import pytest

from bedside_audio.catalog import DlnaLibraryCatalog, DlnaMedia, InvalidMediaPath


class FakeDlna:
    def health(self, relative: str | None = None) -> None:
        return None

    def list_dir(self, relative: str = ""):
        if not relative:
            return {
                "path": "",
                "parent": None,
                "display_path": "DLNA",
                "entries": [{"path": "opaque-folder", "name": "TV Shows", "kind": "folder"}],
            }
        assert relative == "opaque-folder"
        return {
            "path": relative,
            "parent": "",
            "display_path": "DLNA / TV Shows",
            "folder_queue": True,
            "entries": [{"path": "opaque-video", "name": "Episode", "kind": "file"}],
        }

    def prepare(self, relative: str):
        assert relative == "opaque-video"
        return (
            DlnaMedia(
                "media-source://dlna_dms/calculon/:42", "video/x-matroska",
                "Episode", "DLNA / TV Shows / Episode",
            ),
            ["opaque-video"], 0,
        )

    def file(self, relative: str):
        return self.prepare(relative)[0]

    def queue(self, relative: str):
        return self.prepare(relative)[1:]


def test_only_dlna_is_browseable_and_opaque_queue_survives_prefix() -> None:
    catalog = DlnaLibraryCatalog(FakeDlna())
    assert catalog.list_dir()["entries"] == [
        {"path": "dlna", "name": "TV library", "kind": "folder"},
    ]
    assert catalog.list_dir("dlna")["entries"] == [
        {"path": "dlna/opaque-folder", "name": "TV Shows", "kind": "folder"},
    ]
    episodes = catalog.list_dir("dlna/opaque-folder")
    assert episodes["folder_queue"] is True
    assert episodes["parent"] == "dlna"
    assert episodes["entries"][0]["path"] == "dlna/opaque-video"
    assert "media-source://" not in json.dumps(episodes)
    media, queue, index = catalog.prepare("dlna/opaque-video")
    assert isinstance(media, DlnaMedia)
    assert queue == ["dlna/opaque-video"]
    assert index == 0


@pytest.mark.parametrize("path", [
    "plex", "plex/nasA", "media-source://dlna_dms/calculon/:42",
    "dlna/", "dlna/../other",
])
def test_old_plex_and_raw_media_paths_cannot_browse_or_play(path: str) -> None:
    catalog = DlnaLibraryCatalog(FakeDlna())
    with pytest.raises(InvalidMediaPath):
        catalog.list_dir(path)
    with pytest.raises(InvalidMediaPath):
        catalog.prepare(path)
