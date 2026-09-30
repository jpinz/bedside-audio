from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, NotRequired, Protocol, TypedDict


class MediaError(Exception):
    status_code = 503


class InvalidMediaPath(MediaError):
    status_code = 400


class MediaNotFound(MediaError):
    status_code = 404


class EntryData(TypedDict):
    path: str
    name: str
    kind: Literal["folder", "file"]


class DirectoryListing(TypedDict):
    path: str
    parent: str | None
    entries: list[EntryData]
    display_path: NotRequired[str]
    folder_queue: NotRequired[bool]


@dataclass(frozen=True, slots=True)
class DlnaMedia:
    media_content_id: str = field(repr=False)
    media_content_type: str
    name: str
    display_path: str


class MediaSource(Protocol):
    def health(self, relative: str | None = None) -> str | None: ...

    def file(self, relative: str) -> DlnaMedia: ...

    def list_dir(self, relative: str = "") -> DirectoryListing: ...

    def queue(self, relative: str) -> tuple[list[str], int]: ...

    def prepare(self, relative: str) -> tuple[DlnaMedia, list[str], int]: ...

    def configured_queue(self, items: tuple[tuple[str, ...], ...]) -> list[str]: ...

    def folder_queue(self, relative: str) -> tuple[list[str], str]: ...


class DlnaLibraryCatalog:
    def __init__(self, dlna: MediaSource) -> None:
        self._dlna = dlna

    @staticmethod
    def _relative(path: str) -> str:
        if path == "dlna":
            return ""
        if isinstance(path, str) and path.startswith("dlna/") and re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", path[5:],
        ):
            return path[5:]
        raise InvalidMediaPath("Choose a DLNA video from the library")

    def health(self, relative: str | None = None) -> str | None:
        if relative is None:
            return self._dlna.health()
        try:
            return self._dlna.health(self._relative(relative))
        except MediaError as exc:
            return str(exc)

    def list_dir(self, relative: str = "") -> DirectoryListing:
        if relative == "":
            return {
                "path": "",
                "parent": None,
                "entries": [{"path": "dlna", "name": "TV library", "kind": "folder"}],
            }
        child = self._relative(relative)
        listing = self._dlna.list_dir(child)
        parent = listing["parent"]
        result: DirectoryListing = {
            "path": "dlna" if not child else f"dlna/{listing['path']}",
            "parent": "" if parent is None else "dlna" if not parent else f"dlna/{parent}",
            "entries": [
                {**entry, "path": f"dlna/{entry['path']}"}
                for entry in listing["entries"]
            ],
        }
        if "display_path" in listing:
            result["display_path"] = listing["display_path"]
        if listing.get("folder_queue") is True:
            result["folder_queue"] = True
        return result

    def file(self, relative: str) -> DlnaMedia:
        return self._dlna.file(self._relative(relative))

    def queue(self, relative: str) -> tuple[list[str], int]:
        _, paths, index = self.prepare(relative)
        return paths, index

    def prepare(self, relative: str) -> tuple[DlnaMedia, list[str], int]:
        target, paths, index = self._dlna.prepare(self._relative(relative))
        return target, [f"dlna/{path}" for path in paths], index

    def configured_queue(self, items: tuple[tuple[str, ...], ...]) -> list[str]:
        return [f"dlna/{path}" for path in self._dlna.configured_queue(items)]

    def folder_queue(self, relative: str) -> tuple[list[str], str]:
        paths, display_path = self._dlna.folder_queue(self._relative(relative))
        return [f"dlna/{path}" for path in paths], display_path
