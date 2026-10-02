from __future__ import annotations

import re
from dataclasses import dataclass, field

LED_THEME_PAYLOAD_LENGTH = 50
_LED_COLOR = re.compile(r"#[0-9A-F]{6}")
_MEDIA_PLAYER_ENTITY = re.compile(r"media_player\.[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class LedStyle:
    color: str
    brightness: int

    def __post_init__(self) -> None:
        if not isinstance(self.color, str) or not _LED_COLOR.fullmatch(self.color):
            raise ValueError("LED colors must use canonical #RRGGBB")
        if type(self.brightness) is not int or not 1 <= self.brightness <= 100:
            raise ValueError("LED brightness must be an integer from 1 to 100")

    @property
    def payload(self) -> str:
        return f"{self.color}@{self.brightness:03d}"

    @property
    def rgb_color(self) -> list[int]:
        return [
            int(self.color[index:index + 2], 16)
            for index in (1, 3, 5)
        ]

    @property
    def ha_brightness(self) -> int:
        return round(self.brightness * 255 / 100)


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

    def display_style(self, display: str) -> LedStyle:
        if display == "playing":
            return self.playing
        if display == "paused":
            return self.paused
        if display == "sleeping":
            return self.sleeping
        raise ValueError("Voice LED display is not supported")


@dataclass(frozen=True, slots=True)
class HardwareBridgeSettings:
    button_event_entity: str
    assist_satellite_entity: str
    led_light_entity: str = ""
    led_select_entity: str = ""
    led_theme_text_entity: str = ""
    volume_cap_number_entity: str = ""
    led_theme: LedTheme = field(default_factory=LedTheme)

    @property
    def invalid_reason(self) -> str | None:
        values = (
            self.button_event_entity,
            self.assist_satellite_entity,
            self.led_light_entity,
            self.led_select_entity,
            self.led_theme_text_entity,
            self.volume_cap_number_entity,
        )
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
    def led_entity(self) -> str:
        return self.led_light_entity or self.led_select_entity


@dataclass(frozen=True, slots=True)
class Settings:
    music_assistant_player_entity: str
    voice_media_player_entity: str
    max_volume: int
    hardware: HardwareBridgeSettings

    def __post_init__(self) -> None:
        if not _MEDIA_PLAYER_ENTITY.fullmatch(self.music_assistant_player_entity):
            raise ValueError(
                "music_assistant_player_entity must name one media_player entity",
            )
        if not _MEDIA_PLAYER_ENTITY.fullmatch(self.voice_media_player_entity):
            raise ValueError(
                "voice_media_player_entity must name one media_player entity",
            )
        if type(self.max_volume) is not int or not 1 <= self.max_volume <= 50:
            raise ValueError("max_volume must be an integer from 1 to 50")
        if not isinstance(self.hardware, HardwareBridgeSettings):
            raise ValueError("Hardware bridge settings are required")
        if reason := self.hardware.invalid_reason:
            raise ValueError(reason)
        if (
            self.hardware.volume_cap_number_entity
            and self.max_volume % 5 != 0
        ):
            raise ValueError(
                "max_volume must use 5 percent steps with the firmware volume cap",
            )
