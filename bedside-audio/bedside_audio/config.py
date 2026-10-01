from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

MAX_PLAYLISTS = 16
MAX_PLAYLIST_ITEMS = 64
MAX_PLAYLIST_PATH_SEGMENTS = 16
MAX_PLAYLIST_NAME_LENGTH = 64
MAX_PLAYLIST_ITEM_TEXT_LENGTH = 8192
MAX_LIBRARY_FOLDERS = 32
MAX_LIBRARY_PATH_TEXT_LENGTH = 1024
MAX_LIBRARY_EXCLUDE_PATTERNS = 32
MAX_LIBRARY_EXCLUDE_PATTERN_LENGTH = 256
LED_THEME_PAYLOAD_LENGTH = 50
_UNSAFE_PLAYLIST_SEGMENT = re.compile(
    r"(?:^[\\/]|^[A-Za-z]:[\\/]|[A-Za-z][A-Za-z0-9+.-]*://|media-source:)",
    re.IGNORECASE,
)
_LED_COLOR = re.compile(r"#[0-9A-F]{6}")


def _valid_label(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value == value.strip()
        and value not in {".", ".."}
        and not any(
            char in "\\<>" or unicodedata.category(char).startswith("C")
            for char in value
        )
    )


def _valid_dlna_path_segment(value: object) -> bool:
    return (
        _valid_label(value, maximum=256)
        and not _UNSAFE_PLAYLIST_SEGMENT.search(value)
    )


def _valid_library_exclude_pattern(value: object) -> bool:
    return (
        _valid_label(value, maximum=MAX_LIBRARY_EXCLUDE_PATTERN_LENGTH)
        and not value.startswith("/")
    )


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    entity_id: str
    max_volume: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, str) or not re.fullmatch(
            r"media_player\.[a-z0-9_]+", self.entity_id,
        ):
            raise ValueError("media_player_entity must name one media_player entity")
        if type(self.max_volume) is not int or not 1 <= self.max_volume <= 50:
            raise ValueError("Voice maximum volume must be 1 to 50")


@dataclass(frozen=True, slots=True)
class DlnaSettings:
    source_id: str
    browse_player_entity_id: str
    owner_user_id: str | None = None
    allowed_folders: tuple[tuple[str, ...], ...] = ()
    excluded_title_patterns: tuple[str, ...] = ()
    browse_root: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not re.fullmatch(
            r"[a-z0-9_]{1,128}", self.source_id,
        ):
            raise ValueError("dlna_source_id must identify one Home Assistant DLNA source")
        if not isinstance(self.browse_player_entity_id, str) or not re.fullmatch(
            r"media_player\.[a-z0-9_]+", self.browse_player_entity_id,
        ):
            raise ValueError("dlna_browse_player_entity_id must name one media player")
        if self.owner_user_id is not None and (
            not isinstance(self.owner_user_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.owner_user_id)
        ):
            raise ValueError("dlna_owner_user_id must name one Home Assistant user")
        if (
            not isinstance(self.allowed_folders, tuple)
            or len(self.allowed_folders) > MAX_LIBRARY_FOLDERS
        ):
            raise ValueError(
                f"Configure no more than {MAX_LIBRARY_FOLDERS} library folders",
            )
        for path in self.allowed_folders:
            if (
                not isinstance(path, tuple)
                or not 1 <= len(path) <= MAX_PLAYLIST_PATH_SEGMENTS
                or any(not _valid_dlna_path_segment(segment) for segment in path)
            ):
                raise ValueError("Library folders must be valid DLNA title paths")
        if len(set(self.allowed_folders)) != len(self.allowed_folders):
            raise ValueError("Library folder paths must be unique")
        if (
            not isinstance(self.excluded_title_patterns, tuple)
            or len(self.excluded_title_patterns) > MAX_LIBRARY_EXCLUDE_PATTERNS
            or any(
                not _valid_library_exclude_pattern(pattern)
                for pattern in self.excluded_title_patterns
            )
        ):
            raise ValueError(
                "Library exclusion patterns must be safe title glob patterns",
            )
        normalized_patterns = [
            unicodedata.normalize("NFKC", pattern).casefold()
            for pattern in self.excluded_title_patterns
        ]
        if len(set(normalized_patterns)) != len(normalized_patterns):
            raise ValueError("Library exclusion patterns must be unique")
        if (
            not isinstance(self.browse_root, tuple)
            or len(self.browse_root) > MAX_PLAYLIST_PATH_SEGMENTS
            or any(
                not _valid_dlna_path_segment(segment)
                for segment in self.browse_root
            )
        ):
            raise ValueError("Library root folder must be a valid DLNA title path")


@dataclass(frozen=True, slots=True)
class HardwareBridgeSettings:
    button_event_entity: str = ""
    assist_satellite_entity: str = ""
    led_light_entity: str = ""
    led_select_entity: str = ""
    led_theme_text_entity: str = ""
    volume_cap_number_entity: str = ""
    led_theme: LedTheme = field(default_factory=lambda: LedTheme())

    @property
    def disabled_reason(self) -> str | None:
        values = (
            self.button_event_entity,
            self.assist_satellite_entity,
            self.led_light_entity,
            self.led_select_entity,
            self.led_theme_text_entity,
            self.volume_cap_number_entity,
        )
        if not any(values):
            return "Hardware bridge is not configured"
        if not all(isinstance(value, str) for value in values):
            return "Hardware bridge entity options must be strings"
        if not re.fullmatch(r"event\.[a-z0-9_]+", self.button_event_entity):
            return "Voice button event entity is missing or invalid"
        if not re.fullmatch(
            r"assist_satellite\.[a-z0-9_]+", self.assist_satellite_entity,
        ):
            return "Voice Assist satellite entity is missing or invalid"
        outputs = int(bool(self.led_light_entity)) + int(bool(self.led_select_entity))
        if outputs != 1:
            return "Configure exactly one Voice LED light or select entity"
        if self.led_light_entity and not re.fullmatch(
            r"light\.[a-z0-9_]+", self.led_light_entity,
        ):
            return "Voice LED light entity is invalid"
        if self.led_select_entity and not re.fullmatch(
            r"select\.[a-z0-9_]+", self.led_select_entity,
        ):
            return "Voice LED select entity is invalid"
        if self.led_theme_text_entity and not re.fullmatch(
            r"text\.[a-z0-9_]+", self.led_theme_text_entity,
        ):
            return "Voice LED theme text entity is invalid"
        if self.led_theme_text_entity and not self.led_select_entity:
            return "Voice LED theme text entity requires the custom LED select entity"
        if self.volume_cap_number_entity and not re.fullmatch(
            r"number\.[a-z0-9_]+", self.volume_cap_number_entity,
        ):
            return "Voice volume cap number entity is invalid"
        return None

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    @property
    def led_entity(self) -> str:
        return self.led_light_entity or self.led_select_entity


@dataclass(frozen=True, slots=True)
class LedStyle:
    color: str
    brightness: int

    def __post_init__(self) -> None:
        if not isinstance(self.color, str) or not _LED_COLOR.fullmatch(self.color):
            raise ValueError("LED colors must use canonical #RRGGBB")
        if (
            type(self.brightness) is not int
            or not 1 <= self.brightness <= 100
        ):
            raise ValueError("LED brightness must be an integer from 1 to 100")

    @property
    def payload(self) -> str:
        return f"{self.color}@{self.brightness:03d}"


@dataclass(frozen=True, slots=True)
class LedTheme:
    playing: LedStyle = field(
        default_factory=lambda: LedStyle("#00FF30", 12),
    )
    paused: LedStyle = field(
        default_factory=lambda: LedStyle("#FF7000", 18),
    )
    sleeping: LedStyle = field(
        default_factory=lambda: LedStyle("#6000A0", 8),
    )
    button_press: LedStyle = field(
        default_factory=lambda: LedStyle("#18BBF2", 10),
    )

    @property
    def payload(self) -> str:
        payload = "v1|" + "|".join((
            self.playing.payload,
            self.paused.payload,
            self.sleeping.payload,
            self.button_press.payload,
        ))
        if len(payload) != LED_THEME_PAYLOAD_LENGTH:
            raise ValueError("LED theme payload length is invalid")
        return payload


@dataclass(frozen=True, slots=True)
class PlaylistSettings:
    name: str
    items: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not _valid_label(self.name, maximum=MAX_PLAYLIST_NAME_LENGTH):
            raise ValueError(
                f"Playlist names must be 1 to {MAX_PLAYLIST_NAME_LENGTH} safe characters",
            )
        if (
            not isinstance(self.items, tuple)
            or not 1 <= len(self.items) <= MAX_PLAYLIST_ITEMS
        ):
            raise ValueError(
                f"Playlist {self.name!r} must contain 1 to {MAX_PLAYLIST_ITEMS} items",
            )
        for item in self.items:
            if (
                not isinstance(item, tuple)
                or not 1 <= len(item) <= MAX_PLAYLIST_PATH_SEGMENTS
                or any(not _valid_dlna_path_segment(segment) for segment in item)
            ):
                raise ValueError(
                    f"Playlist {self.name!r} contains an invalid DLNA title path",
                )

    @property
    def normalized_name(self) -> str:
        return unicodedata.normalize("NFKC", self.name).casefold()


@dataclass(frozen=True, slots=True)
class Settings:
    state_dir: Path
    voice: VoiceSettings
    dlna: DlnaSettings
    initial_volume: int = 15
    hardware: HardwareBridgeSettings = field(default_factory=HardwareBridgeSettings)
    playlists: tuple[PlaylistSettings, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.dlna, DlnaSettings):
            raise ValueError("Set dlna_source_id and dlna_browse_player_entity_id")
        if self.dlna.browse_player_entity_id == self.voice.entity_id:
            raise ValueError("DLNA browse player must differ from the Voice player")
        if type(self.initial_volume) is not int or not 0 <= self.initial_volume <= self.voice.max_volume:
            raise ValueError("Voice startup volume must not exceed its configured limit")
        if (
            not isinstance(self.playlists, tuple)
            or len(self.playlists) > MAX_PLAYLISTS
            or any(not isinstance(playlist, PlaylistSettings) for playlist in self.playlists)
        ):
            raise ValueError(f"Configure no more than {MAX_PLAYLISTS} playlists")
        normalized = [playlist.normalized_name for playlist in self.playlists]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Playlist names must be unique")
