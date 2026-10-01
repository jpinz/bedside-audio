from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn

from .config import (
    MAX_LIBRARY_EXCLUDE_PATTERN_LENGTH,
    MAX_LIBRARY_EXCLUDE_PATTERNS,
    MAX_LIBRARY_FOLDERS,
    MAX_LIBRARY_PATH_TEXT_LENGTH,
    MAX_PLAYLIST_ITEM_TEXT_LENGTH,
    MAX_PLAYLIST_ITEMS,
    MAX_PLAYLISTS,
    DlnaSettings,
    HardwareBridgeSettings,
    PlaylistSettings,
    Settings,
    VoiceSettings,
)


def _parse_library_exclude_patterns(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LIBRARY_EXCLUDE_PATTERNS:
        raise ValueError(
            f"library_exclude_patterns must be a list with at most "
            f"{MAX_LIBRARY_EXCLUDE_PATTERNS} entries",
        )
    patterns: list[str] = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {"pattern"}:
            raise ValueError(
                "Each library exclusion must contain only pattern",
            )
        pattern = record["pattern"]
        if (
            not isinstance(pattern, str)
            or not 1 <= len(pattern) <= MAX_LIBRARY_EXCLUDE_PATTERN_LENGTH
        ):
            raise ValueError(
                f"Library exclusion patterns must be 1 to "
                f"{MAX_LIBRARY_EXCLUDE_PATTERN_LENGTH} characters",
            )
        patterns.append(pattern)
    return tuple(patterns)


def _parse_library_folders(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) > MAX_LIBRARY_FOLDERS:
        raise ValueError(
            f"library_folders must be a list with at most "
            f"{MAX_LIBRARY_FOLDERS} entries",
        )
    folders: list[tuple[str, ...]] = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {"path"}:
            raise ValueError("Each library folder must contain only path")
        path_text = record["path"]
        if (
            not isinstance(path_text, str)
            or not 1 <= len(path_text) <= MAX_LIBRARY_PATH_TEXT_LENGTH
        ):
            raise ValueError(
                f"Library folder paths must be 1 to "
                f"{MAX_LIBRARY_PATH_TEXT_LENGTH} characters",
            )
        if path_text != path_text.strip():
            raise ValueError("Library folder paths cannot have outer whitespace")
        try:
            path = json.loads(path_text)
        except ValueError as exc:
            raise ValueError(
                "Library folder paths must be JSON arrays of DLNA titles",
            ) from exc
        if not isinstance(path, list) or any(
            not isinstance(part, str) for part in path
        ):
            raise ValueError(
                "Library folder paths must be JSON arrays of DLNA titles",
            )
        folders.append(tuple(path))
    return tuple(folders)


def _parse_playlists(value: object) -> tuple[PlaylistSettings, ...]:
    if not isinstance(value, list) or len(value) > MAX_PLAYLISTS:
        raise ValueError(f"playlists must be a list with at most {MAX_PLAYLISTS} entries")
    playlists: list[PlaylistSettings] = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {"name", "items"}:
            raise ValueError("Each playlist must contain only name and items")
        name = record["name"]
        item_text = record["items"]
        if not isinstance(name, str) or not isinstance(item_text, str):
            raise ValueError("Playlist name and items must be strings")
        if not 1 <= len(item_text) <= MAX_PLAYLIST_ITEM_TEXT_LENGTH:
            raise ValueError(
                f"Playlist {name!r} items must be 1 to "
                f"{MAX_PLAYLIST_ITEM_TEXT_LENGTH} characters",
            )
        lines = item_text.splitlines()
        if not lines or len(lines) > MAX_PLAYLIST_ITEMS or any(not line for line in lines):
            raise ValueError(
                f"Playlist {name!r} must contain 1 to {MAX_PLAYLIST_ITEMS} non-empty lines",
            )
        items: list[tuple[str, ...]] = []
        for line in lines:
            if line != line.strip():
                raise ValueError(f"Playlist {name!r} item lines cannot have outer whitespace")
            try:
                path = json.loads(line)
            except ValueError as exc:
                raise ValueError(
                    f"Playlist {name!r} item lines must be JSON arrays of DLNA titles",
                ) from exc
            if not isinstance(path, list) or any(not isinstance(part, str) for part in path):
                raise ValueError(
                    f"Playlist {name!r} item lines must be JSON arrays of DLNA titles",
                )
            items.append(tuple(path))
        playlists.append(PlaylistSettings(name, tuple(items)))
    return tuple(playlists)


def settings_from_options(path: Path = Path("/data/options.json")) -> Settings:
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read Home Assistant app options") from exc
    if not isinstance(options, dict):
        raise ValueError("Home Assistant app options must be an object")
    required = {
        "media_player_entity", "dlna_source_id",
        "dlna_browse_player_entity_id", "max_volume",
    }
    optional = {
        "dlna_owner_user_id", "plex_networks", "plex_ports",
        "plex_allow_http", "plex_auth_mode", "ha_allow_http_link",
        "ha_http_link_origin",
        "library_exclude_patterns", "library_folders", "playlists",
        "voice_button_event_entity", "voice_assist_satellite_entity",
        "voice_led_light_entity", "voice_led_select_entity",
        "voice_volume_cap_number_entity",
    }
    if not required <= set(options) or not set(options) <= required | optional:
        raise ValueError("Home Assistant app options are missing or unrecognized")

    for field in ("dlna_source_id", "dlna_browse_player_entity_id", "dlna_owner_user_id"):
        if type(options.get(field, "")) is not str:
            raise ValueError(f"{field} must be a string")
    source_id = options.get("dlna_source_id", "")
    browse_player = options.get("dlna_browse_player_entity_id", "")
    owner_user_id = options.get("dlna_owner_user_id", "")
    allowed_folders = _parse_library_folders(options.get("library_folders", []))
    excluded_patterns = _parse_library_exclude_patterns(
        options.get("library_exclude_patterns", []),
    )
    dlna = DlnaSettings(
        source_id,
        browse_player,
        owner_user_id or None,
        allowed_folders,
        excluded_patterns,
    )
    max_volume = options["max_volume"]
    if type(max_volume) is not int or not 1 <= max_volume <= 50:
        raise ValueError("max_volume must be an integer from 1 to 50")
    voice = VoiceSettings(options["media_player_entity"], max_volume=max_volume)
    hardware = HardwareBridgeSettings(
        button_event_entity=options.get("voice_button_event_entity", ""),
        assist_satellite_entity=options.get("voice_assist_satellite_entity", ""),
        led_light_entity=options.get("voice_led_light_entity", ""),
        led_select_entity=options.get("voice_led_select_entity", ""),
        volume_cap_number_entity=options.get(
            "voice_volume_cap_number_entity", "",
        ),
    )
    playlists = _parse_playlists(options.get("playlists", []))
    return Settings(
        state_dir=Path("/data/bedside-audio"),
        initial_volume=min(15, max_volume),
        voice=voice,
        dlna=dlna,
        hardware=hardware,
        playlists=playlists,
    )


def main() -> None:
    if not os.environ.get("SUPERVISOR_TOKEN"):
        raise RuntimeError("Home Assistant Supervisor token is unavailable")
    from .app import create_app

    uvicorn.run(
        create_app(settings_from_options()),
        host="0.0.0.0",
        port=8099,
        proxy_headers=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
