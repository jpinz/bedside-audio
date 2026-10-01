from __future__ import annotations

import logging
import re
import secrets
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from threading import RLock
from typing import Literal, Protocol

from .catalog import (
    DirectoryListing,
    DlnaMedia,
    InvalidMediaPath,
    MediaError,
    MediaNotFound,
)
from .config import DlnaSettings
from .player import PlayerError


_logger = logging.getLogger(__name__)


class DlnaBrowser(Protocol):
    def browse_media(
        self, entity_id: str, media_content_id: str, media_content_type: str
    ) -> dict[str, object]: ...


_ROOT_TYPE = "object.container.storageFolder"
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PLAYER_ENTITY = re.compile(r"media_player\.[a-z0-9_]{1,128}\Z")
_FOLDER_TYPE = re.compile(r"object\.container(?:\.[A-Za-z0-9_-]+)*\Z")
_VIDEO_TYPE = re.compile(r"video/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z")
_NONVIDEO_TYPE = re.compile(r"(?:audio|image)/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z")
_MEDIA_CLASS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HANDLE = re.compile(r"[A-Za-z0-9_-]{16}\Z")
_CHILD_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@-]{0,1023}\Z")
_VIDEO_CLASSES = frozenset({"video", "episode", "movie", "tv_show"})
_SENSITIVE_NAME = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|media-source:|plex:|"
    r"\b(?:x-plex-token|access[_-]?token|token|api[_-]?key|authorization)\s*[:=]|"
    r"\bbearer\s+\S+|%[0-9a-f]{2})",
    re.IGNORECASE,
)
_MAX_CHILDREN = 256
_MAX_HANDLES = 1024
_MAX_QUEUE_ITEMS = 256
_MAX_QUEUE_DEPTH = 16


@dataclass(frozen=True, slots=True)
class _Node:
    media_id: str
    media_type: str
    name: str
    title_path: tuple[str, ...]
    kind: Literal["folder", "file"]
    display_path: str
    parent_handle: str | None
    parent_id: str | None
    parent_type: str | None
    parent_display_path: str
    ordinal: int


class DlnaCatalog:
    def __init__(self, settings: DlnaSettings, browser: DlnaBrowser) -> None:
        if (
            not isinstance(settings.source_id, str)
            or not _SOURCE_ID.fullmatch(settings.source_id)
            or settings.source_id in {".", ".."}
            or not isinstance(settings.browse_player_entity_id, str)
            or not _PLAYER_ENTITY.fullmatch(settings.browse_player_entity_id)
        ):
            raise MediaError("Invalid DLNA browse settings")
        self._entity_id = settings.browse_player_entity_id
        self._id_prefix = f"media-source://dlna_dms/{settings.source_id}/:"
        self._root_id = f"{self._id_prefix}0"
        self._browser = browser
        self._allowed_folders = settings.allowed_folders
        self._excluded_title_patterns = tuple(
            unicodedata.normalize("NFKC", pattern).casefold()
            for pattern in settings.excluded_title_patterns
        )
        self._lock = RLock()
        self._handles: OrderedDict[str, _Node] = OrderedDict()
        self._last_error: str | None = None

    def _root(self) -> _Node:
        return _Node(
            self._root_id,
            _ROOT_TYPE,
            "DLNA",
            (),
            "folder",
            "DLNA",
            None,
            None,
            None,
            "",
            -1,
        )

    def health(self, relative: str | None = None) -> str | None:
        with self._lock:
            return self._last_error

    def list_dir(self, relative: str = "") -> DirectoryListing:
        if relative == "":
            parent = self._root()
        else:
            parent = self._resolve(relative)
            if parent.kind != "folder":
                raise MediaNotFound("DLNA folder not found")
        children = self._browse_folder(parent, relative)
        with self._lock:
            if relative:
                if self._handles.get(relative) is not parent:
                    raise MediaNotFound("DLNA folder not found")
                self._handles.move_to_end(relative)
            entries = [
                {"path": self._store(child), "name": child.name, "kind": child.kind}
                for child in children
            ]
        return {
            "path": relative,
            "parent": parent.parent_handle,
            "entries": entries,
            "display_path": parent.display_path,
            "folder_queue": any(child.kind == "file" for child in children),
        }

    def prepare(self, relative: str) -> tuple[DlnaMedia, list[str], int]:
        selected, children = self._selected_with_siblings(relative)
        files = [child for child in children if child.kind == "file"]
        with self._lock:
            if self._handles.get(relative) is not selected:
                raise MediaNotFound("DLNA item not found")
            self._handles.move_to_end(relative)
            queue = [
                relative if child.ordinal == selected.ordinal else self._store(child)
                for child in files
            ]
        index = next(
            index
            for index, child in enumerate(files)
            if child.ordinal == selected.ordinal
        )
        return self._media(selected), queue, index

    def file(self, relative: str) -> DlnaMedia:
        selected, _ = self._selected_with_siblings(relative)
        with self._lock:
            if self._handles.get(relative) is not selected:
                raise MediaNotFound("DLNA item not found")
        return self._media(selected)

    def queue(self, relative: str) -> tuple[list[str], int]:
        _, queue, index = self.prepare(relative)
        return queue, index

    def configured_queue(self, items: tuple[tuple[str, ...], ...]) -> list[str]:
        queue: list[str] = []
        for path in items:
            node, handle = self._resolve_named_path(path)
            if node.kind == "file":
                self._append_queue_item(queue, handle)
            else:
                self._append_folder_queue(node, handle, queue, frozenset(), 0)
        if not queue:
            raise MediaNotFound("Configured playlist has no playable videos")
        return queue

    def folder_queue(self, relative: str) -> tuple[list[str], str]:
        folder = self._resolve(relative)
        if folder.kind != "folder":
            raise MediaNotFound("Choose a DLNA folder to shuffle")
        queue: list[str] = []
        self._append_folder_queue(folder, relative, queue, frozenset(), 0)
        if not queue:
            raise MediaNotFound("This folder has no playable videos")
        return queue, folder.display_path

    @staticmethod
    def _media(node: _Node) -> DlnaMedia:
        return DlnaMedia(
            media_content_id=node.media_id,
            media_content_type=node.media_type,
            name=node.name,
            display_path=node.display_path,
        )

    def _selected_with_siblings(self, relative: str) -> tuple[_Node, list[_Node]]:
        selected = self._resolve(relative)
        if (
            selected.kind != "file"
            or selected.parent_id is None
            or selected.parent_type is None
        ):
            raise MediaNotFound("DLNA file not found")
        parent = _Node(
            selected.parent_id,
            selected.parent_type,
            "",
            selected.title_path[:-1],
            "folder",
            selected.parent_display_path,
            None,
            None,
            None,
            "",
            -1,
        )
        children = self._browse_folder(parent, selected.parent_handle or "")
        current = next(
            (child for child in children if child.ordinal == selected.ordinal),
            None,
        )
        if (
            current is None
            or current.kind != "file"
            or (current.media_id, current.media_type, current.name)
            != (selected.media_id, selected.media_type, selected.name)
        ):
            raise MediaNotFound("DLNA file is no longer available")
        return selected, children

    def _resolve_named_path(self, path: tuple[str, ...]) -> tuple[_Node, str]:
        parent = self._root()
        parent_handle = ""
        selected: _Node | None = None
        selected_handle = ""
        for index, name in enumerate(path):
            children = self._browse_folder(parent, parent_handle)
            matches = [child for child in children if child.name == name]
            if not matches:
                raise MediaNotFound(
                    f"Configured playlist item is unavailable at {name!r}",
                )
            if len(matches) != 1:
                raise MediaNotFound(
                    f"Configured playlist item is ambiguous at {name!r}",
                )
            selected = matches[0]
            with self._lock:
                if parent_handle and self._handles.get(parent_handle) is not parent:
                    raise MediaNotFound("Configured playlist folder is no longer available")
                selected_handle = self._store(selected)
                selected = self._handles[selected_handle]
            if index + 1 < len(path) and selected.kind != "folder":
                raise MediaNotFound(
                    f"Configured playlist path continues after file {name!r}",
                )
            parent = selected
            parent_handle = selected_handle
        if selected is None:
            raise MediaNotFound("Configured playlist item is empty")
        return selected, selected_handle

    def _append_folder_queue(
        self,
        folder: _Node,
        folder_handle: str,
        queue: list[str],
        ancestors: frozenset[str],
        depth: int,
    ) -> None:
        if depth >= _MAX_QUEUE_DEPTH:
            raise MediaError("DLNA folder nesting exceeds the queue limit")
        if folder.media_id in ancestors:
            raise MediaError("DLNA folder contains a recursive loop")
        children = self._browse_folder(folder, folder_handle)
        stored: list[tuple[_Node, str]] = []
        with self._lock:
            if folder_handle and self._handles.get(folder_handle) is not folder:
                raise MediaNotFound("DLNA folder is no longer available")
            for child in children:
                handle = self._store(child)
                stored.append((self._handles[handle], handle))
        next_ancestors = ancestors | {folder.media_id}
        for child, handle in stored:
            if child.kind == "file":
                self._append_queue_item(queue, handle)
            else:
                self._append_folder_queue(
                    child, handle, queue, next_ancestors, depth + 1,
                )

    @staticmethod
    def _append_queue_item(queue: list[str], handle: str) -> None:
        if len(queue) >= _MAX_QUEUE_ITEMS:
            raise MediaError(
                f"A Bedside queue may contain at most {_MAX_QUEUE_ITEMS} videos",
            )
        queue.append(handle)

    def _resolve(self, relative: str) -> _Node:
        if not isinstance(relative, str) or not _HANDLE.fullmatch(relative):
            raise InvalidMediaPath("Invalid DLNA media path")
        with self._lock:
            try:
                node = self._handles[relative]
                self._handles.move_to_end(relative)
                return node
            except KeyError:
                raise MediaNotFound("DLNA item not found") from None

    def _browse_folder(self, parent: _Node, parent_handle: str) -> list[_Node]:
        try:
            data = self._browser.browse_media(
                self._entity_id, parent.media_id, parent.media_type
            )
        except (PlayerError, OSError) as exc:
            _logger.warning("DLNA browse failed: %s", type(exc).__name__)
            with self._lock:
                self._last_error = "DLNA browsing unavailable"
            raise MediaError("DLNA browsing unavailable") from None
        try:
            nodes = self._decode_folder(data, parent, parent_handle)
        except MediaError:
            with self._lock:
                self._last_error = "DLNA browsing unavailable"
            raise
        with self._lock:
            self._last_error = None
        return nodes

    def _decode_folder(
        self, data: dict[str, object], parent: _Node, parent_handle: str
    ) -> list[_Node]:
        if (
            not isinstance(data, dict)
            or data.get("media_content_id") != parent.media_id
            or data.get("media_content_type") != parent.media_type
            or not _FOLDER_TYPE.fullmatch(parent.media_type)
            or data.get("can_expand") is not True
            or data.get("can_play") is not False
        ):
            raise MediaError("Invalid DLNA folder response")
        children = data.get("children")
        not_shown = data.get("not_shown", 0)
        if (
            not isinstance(children, list)
            or len(children) > _MAX_CHILDREN
            or type(not_shown) is not int
            or not_shown
        ):
            raise MediaError("Incomplete DLNA folder response")
        nodes: list[_Node] = []
        for ordinal, child in enumerate(children):
            if not isinstance(child, dict):
                raise MediaError("Invalid DLNA child")
            media_id = child.get("media_content_id")
            media_type = child.get("media_content_type")
            name = child.get("title")
            media_class = child.get("media_class")
            if (
                not isinstance(media_id, str)
                or not media_id.startswith(self._id_prefix)
            ):
                raise MediaError("Unsupported DLNA media identifier")
            suffix = media_id[len(self._id_prefix) :]
            if not _CHILD_SUFFIX.fullmatch(suffix) or ".." in suffix:
                raise MediaError("Unsupported DLNA media identifier")
            if (
                not isinstance(media_type, str)
                or len(media_type) > 128
                or not isinstance(name, str)
                or not self._safe_name(name)
                or len(parent.display_path) + 3 + len(name) > 512
                or not isinstance(media_class, str)
                or not _MEDIA_CLASS.fullmatch(media_class)
                or type(child.get("can_expand")) is not bool
                or type(child.get("can_play")) is not bool
            ):
                raise MediaError("Invalid DLNA child")
            is_folder = bool(_FOLDER_TYPE.fullmatch(media_type))
            is_video = bool(_VIDEO_TYPE.fullmatch(media_type))
            is_nonvideo = bool(_NONVIDEO_TYPE.fullmatch(media_type))
            if child["can_expand"]:
                if not is_folder or media_id == parent.media_id:
                    raise MediaError("Invalid DLNA child")
                kind: Literal["folder", "file"] = "folder"
            elif child["can_play"]:
                if is_video and media_class in _VIDEO_CLASSES:
                    kind = "file"
                elif is_nonvideo and media_class not in _VIDEO_CLASSES:
                    continue
                else:
                    raise MediaError("Invalid DLNA child")
            else:
                if not (is_folder or is_video or is_nonvideo):
                    raise MediaError("Invalid DLNA child")
                continue
            nodes.append(
                _Node(
                    media_id,
                    media_type,
                    name,
                    parent.title_path + (name,),
                    kind,
                    f"{parent.display_path} / {name}",
                    parent_handle,
                    parent.media_id,
                    parent.media_type,
                    parent.display_path,
                    ordinal,
                )
            )
        return self._filter_nodes(parent, nodes)

    def _filter_nodes(self, parent: _Node, nodes: list[_Node]) -> list[_Node]:
        nodes = [
            node
            for node in nodes
            if not self._title_is_excluded(node.name)
        ]
        if not self._allowed_folders:
            return nodes
        parent_path = parent.title_path
        if any(
            len(path) <= len(parent_path)
            and parent_path[:len(path)] == path
            for path in self._allowed_folders
        ):
            return nodes
        next_names = {
            path[len(parent_path)]
            for path in self._allowed_folders
            if (
                len(path) > len(parent_path)
                and path[:len(parent_path)] == parent_path
            )
        }
        filtered = [node for node in nodes if node.name in next_names]
        for name in next_names:
            matches = [node for node in filtered if node.name == name]
            if len(matches) > 1:
                raise MediaError(
                    f"Configured library folder is ambiguous at {name!r}",
                )
            if matches and matches[0].kind != "folder":
                raise MediaError(
                    f"Configured library folder is not a folder at {name!r}",
                )
        return filtered

    def _title_is_excluded(self, title: str) -> bool:
        normalized = unicodedata.normalize("NFKC", title).casefold()
        return any(
            fnmatchcase(normalized, pattern)
            for pattern in self._excluded_title_patterns
        )

    @staticmethod
    def _safe_name(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= 256
            and value == value.strip()
            and not value.startswith("/")
            and not any(
                char in "\\<>" or unicodedata.category(char).startswith("C")
                for char in value
            )
            and not _SENSITIVE_NAME.search(value)
        )

    def _store(self, node: _Node) -> str:
        existing = next(
            (
                (handle, cached)
                for handle, cached in self._handles.items()
                if cached.parent_handle == node.parent_handle
                and cached.ordinal == node.ordinal
            ),
            None,
        )
        if existing is not None:
            handle, cached = existing
            if cached == node:
                self._handles.move_to_end(handle)
                return handle
            del self._handles[handle]
        for _ in range(8):
            handle = secrets.token_urlsafe(12)
            if handle not in self._handles:
                break
        else:
            raise MediaError("Cannot allocate DLNA handle")
        self._handles[handle] = node
        if len(self._handles) > _MAX_HANDLES:
            self._handles.popitem(last=False)
        return handle
