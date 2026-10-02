from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from pathlib import Path

from .config import HardwareBridgeSettings, LedStyle, LedTheme, Settings
from .hardware_bridge import CoreHardwareBridge

_LED_STYLE_DEFAULTS = {
    "playing": ("#00FF30", 12),
    "paused": ("#FF7000", 18),
    "sleeping": ("#6000A0", 8),
    "button_press": ("#18BBF2", 10),
}
_LEGACY_OPTIONS = {
    "media_player_entity",
    "dlna_source_id",
    "dlna_browse_player_entity_id",
    "dlna_owner_user_id",
    "library_exclude_patterns",
    "library_folders",
    "library_root_folder",
    "playlists",
    "plex_networks",
    "plex_ports",
    "plex_allow_http",
    "plex_auth_mode",
    "ha_allow_http_link",
    "ha_http_link_origin",
}


def _parse_led_theme(options: dict[str, object]) -> LedTheme:
    styles: dict[str, LedStyle] = {}
    for name, (default_color, default_brightness) in _LED_STYLE_DEFAULTS.items():
        color_field = f"led_{name}_color"
        brightness_field = f"led_{name}_brightness"
        color = options.get(color_field, default_color)
        brightness = options.get(brightness_field, default_brightness)
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-F]{6}", color):
            raise ValueError(f"{color_field} must use canonical #RRGGBB")
        if type(brightness) is not int or not 1 <= brightness <= 100:
            raise ValueError(
                f"{brightness_field} must be an integer from 1 to 100",
            )
        styles[name] = LedStyle(color, brightness)
    return LedTheme(
        playing=styles["playing"],
        paused=styles["paused"],
        sleeping=styles["sleeping"],
        button_press=styles["button_press"],
    )


def settings_from_options(path: Path = Path("/data/options.json")) -> Settings:
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read Home Assistant app options") from exc
    if not isinstance(options, dict):
        raise ValueError("Home Assistant app options must be an object")

    required = {
        "music_assistant_player_entity",
        "max_volume",
        "voice_button_event_entity",
        "voice_assist_satellite_entity",
        "voice_led_light_entity",
        "voice_led_select_entity",
        "voice_led_theme_text_entity",
        "voice_volume_cap_number_entity",
    }
    optional = {
        "voice_media_player_entity",
        *(
            f"led_{name}_{field}"
            for name in _LED_STYLE_DEFAULTS
            for field in ("color", "brightness")
        ),
        *_LEGACY_OPTIONS,
    }
    if not required <= set(options) or not set(options) <= required | optional:
        raise ValueError("Home Assistant app options are missing or unrecognized")

    music_assistant_player = options["music_assistant_player_entity"]
    legacy_voice_player = options.get("media_player_entity", "")
    voice_player = options.get("voice_media_player_entity") or legacy_voice_player
    if not isinstance(music_assistant_player, str):
        raise ValueError("music_assistant_player_entity must be a string")
    if not isinstance(voice_player, str):
        raise ValueError("voice_media_player_entity must be a string")
    max_volume = options["max_volume"]
    if type(max_volume) is not int:
        raise ValueError("max_volume must be an integer")

    led_theme = _parse_led_theme(options)
    hardware = HardwareBridgeSettings(
        button_event_entity=options["voice_button_event_entity"],
        assist_satellite_entity=options["voice_assist_satellite_entity"],
        led_light_entity=options["voice_led_light_entity"],
        led_select_entity=options["voice_led_select_entity"],
        led_theme_text_entity=options["voice_led_theme_text_entity"],
        volume_cap_number_entity=options["voice_volume_cap_number_entity"],
        led_theme=led_theme,
    )
    return Settings(
        music_assistant_player_entity=music_assistant_player,
        voice_media_player_entity=voice_player,
        max_volume=max_volume,
        hardware=hardware,
    )


async def run(settings: Settings) -> None:
    bridge = CoreHardwareBridge.from_env(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await bridge.start()
    try:
        await stopped.wait()
    finally:
        await bridge.close()


def main() -> None:
    if not os.environ.get("SUPERVISOR_TOKEN"):
        raise RuntimeError("Home Assistant Supervisor token is unavailable")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(settings_from_options()))


if __name__ == "__main__":
    main()
