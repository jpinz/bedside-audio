from __future__ import annotations

import json
from pathlib import Path

import pytest

from bedside_audio.config import HardwareBridgeSettings, Settings
from bedside_audio.ha_addon import settings_from_options


def _options() -> dict[str, object]:
    return {
        "music_assistant_player_entity": "media_player.bedroom_voice_music_assistant",
        "voice_media_player_entity": "media_player.bedroom_voice",
        "max_volume": 40,
        "led_playing_color": "#00FF30",
        "led_playing_brightness": 12,
        "led_paused_color": "#FF7000",
        "led_paused_brightness": 18,
        "led_sleeping_color": "#6000A0",
        "led_sleeping_brightness": 8,
        "led_button_press_color": "#18BBF2",
        "led_button_press_brightness": 10,
        "voice_button_event_entity": "event.bedroom_voice_button",
        "voice_assist_satellite_entity": "assist_satellite.bedroom_voice",
        "voice_led_light_entity": "",
        "voice_led_select_entity": "select.bedroom_voice_ring_mode",
        "voice_led_theme_text_entity": "text.bedroom_voice_led_theme",
        "voice_volume_cap_number_entity": "number.bedroom_voice_volume_cap",
    }


def _write(tmp_path: Path, options: dict[str, object]) -> Path:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    return path


def test_settings_load_hardware_only_options(tmp_path: Path) -> None:
    settings = settings_from_options(_write(tmp_path, _options()))

    assert settings.music_assistant_player_entity == (
        "media_player.bedroom_voice_music_assistant"
    )
    assert settings.voice_media_player_entity == "media_player.bedroom_voice"
    assert settings.max_volume == 40
    assert settings.hardware.led_theme.payload == (
        "v1|#00FF30@012|#FF7000@018|#6000A0@008|#18BBF2@010"
    )


def test_legacy_native_player_option_is_used_during_upgrade(tmp_path: Path) -> None:
    options = _options()
    options.pop("voice_media_player_entity")
    options["media_player_entity"] = "media_player.legacy_voice"
    options["dlna_source_id"] = "ignored"
    options["library_folders"] = []

    settings = settings_from_options(_write(tmp_path, options))

    assert settings.voice_media_player_entity == "media_player.legacy_voice"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "music_assistant_player_entity",
            "media_player.Bad",
            "music_assistant_player_entity",
        ),
        ("voice_media_player_entity", "light.voice", "voice_media_player_entity"),
        ("max_volume", 51, "max_volume"),
    ],
)
def test_settings_reject_invalid_player_and_volume_options(
    field: str,
    value: object,
    message: str,
) -> None:
    values = {
        "music_assistant_player_entity": "media_player.ma_voice",
        "voice_media_player_entity": "media_player.voice",
        "max_volume": 50,
        "hardware": HardwareBridgeSettings(
            button_event_entity="event.voice_button",
            assist_satellite_entity="assist_satellite.voice",
            led_select_entity="select.voice_led",
        ),
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        Settings(**values)


def test_settings_require_exactly_one_led_output() -> None:
    hardware = HardwareBridgeSettings(
        button_event_entity="event.voice_button",
        assist_satellite_entity="assist_satellite.voice",
    )

    with pytest.raises(ValueError, match="exactly one"):
        Settings("media_player.ma_voice", "media_player.voice", 50, hardware)


def test_firmware_volume_cap_requires_five_percent_steps() -> None:
    hardware = HardwareBridgeSettings(
        button_event_entity="event.voice_button",
        assist_satellite_entity="assist_satellite.voice",
        led_select_entity="select.voice_led",
        volume_cap_number_entity="number.voice_cap",
    )

    with pytest.raises(ValueError, match="5 percent steps"):
        Settings("media_player.ma_voice", "media_player.voice", 37, hardware)
