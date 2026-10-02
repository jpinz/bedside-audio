from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
FIRMWARE = ROOT / "firmware" / "voice-pe" / "bedside-voice-pe.yaml"
README = ROOT / "firmware" / "voice-pe" / "README.md"
UPSTREAM_COMMIT = "2644f4c794271d735d182ca7ebf899ed46164f4e"
MODEL_COMMIT = "05b65922cc433c9df13e98e32a7fe520758c837e"


def _source() -> str:
    return FIRMWARE.read_text(encoding="utf-8")


def test_firmware_is_complete_pinned_16mb_source_without_stock_updater() -> None:
    source = _source()
    assert UPSTREAM_COMMIT in source
    assert "version: 26.9.0-bedside.5" in source
    assert "flash_size: 16MB" in source
    assert "8mb" not in source.lower()
    assert "packages:" not in source
    assert "!include" not in source
    assert f"ref: {UPSTREAM_COMMIT}" in source
    assert "/raw/dev/" not in source
    assert source.count(f"/raw/{UPSTREAM_COMMIT}/sounds/") == 16
    assert "platform: http_request" not in source
    assert "update_http_request" not in source
    assert "Beta firmware" not in source
    assert "manifest-beta.json" not in source
    assert "dashboard_import:" not in source


def test_boot_reclamps_restored_volume_before_enabling_amplifier() -> None:
    source = _source()
    boot = source[source.index("  on_boot:"):source.index("\nesp32:")]
    assert "priority: 375" in boot
    clamp = boot.index("media_player.volume_set:")
    amplifier = boot.index("switch.turn_on: internal_speaker_amp")
    assert clamp < amplifier
    assert "id(external_media_player).volume" in boot
    assert "id(bedside_volume_cap).state" in boot
    assert "0.50f" in boot


def test_wake_word_and_vad_manifests_are_explicit_and_immutable() -> None:
    source = _source()
    assert "model: hey_jarvis" not in source
    assert "model: hey_mycroft" not in source
    assert "micro-wake-word-models/raw/main/" not in source
    wake_words = source[source.index("\nmicro_wake_word:"):source.index("\nselect:")]
    assert wake_words.count(
        f"micro-wake-word-models/raw/{MODEL_COMMIT}/models/v2/"
    ) == 3
    vad = wake_words[wake_words.index("  vad:"):wake_words.index(
        "  on_wake_word_detected:"
    )]
    assert "model: https://github.com/esphome/micro-wake-word-models/raw/" in vad


def test_firmware_button_contract_keeps_local_precedence_and_event_only_queue() -> None:
    source = _source()
    center = source.index("id: center_button")
    events = source.index("\nevent:", center)
    button = source[center:events]
    assert source.count("- platform: gpio\n    id: center_button") == 1
    assert source.count("on_multi_click:") == 1
    assert button.index("switch.is_on: timer_ringing") < button.index(
        "voice_assistant.is_running"
    )
    assert button.index("voice_assistant.is_running") < button.index(
        "media_player.is_announcing:"
    )
    assert button.index('current_option() == "playing"') < button.index(
        'event_type: "single_press"'
    )
    assert button.count('event_type: "single_press"') == 1
    assert button.count('event_type: "double_press"') == 1
    assert button.count('event_type: "triple_press"') == 1
    assert 'sound_file: "center_button_double_press_sound"' not in button
    assert 'sound_file: "center_button_triple_press_sound"' not in button
    assert "media_player.next" not in source
    assert "media_player.previous" not in source
    assert "restart_current" not in source
    bridge = (
        ROOT / "bedside-audio" / "bedside_audio" / "hardware_bridge.py"
    ).read_text(encoding="utf-8")
    assert '"media_next_track"' in bridge
    assert '"media_previous_track"' in bridge
    assert '"media_seek"' in bridge
    assert "> 10.0" in bridge


def test_firmware_select_number_and_dial_are_fail_safe() -> None:
    source = _source()
    display = source.index('name: "Bedside display intent"')
    sensitivity = source.index('name: "Wake word sensitivity"', display)
    display_block = source[display:sensitivity]
    assert 'initial_option: "off"' in display_block
    assert "restore_value: false" in display_block
    assert display_block.count('      - "off"') == 1
    assert display_block.count('      - "playing"') == 1
    assert display_block.count('      - "paused"') == 1
    assert display_block.count('      - "sleeping"') == 1

    number = source.index('name: "Bedside volume cap"')
    display_start = source.index("\nselect:", number)
    number_block = source[number:display_start]
    assert "initial_value: 0.50" in number_block
    assert "min_value: 0.0" in number_block
    assert "max_value: 0.50" in number_block
    assert "step: 0.05" in number_block
    assert "restore_value: false" in number_block

    control_volume = source.index("- id: control_volume")
    group_volume = source.index("- id: control_group_volume", control_volume)
    dial_block = source[control_volume:group_volume]
    assert "media_player.volume_set:" in dial_block
    assert "id(bedside_volume_cap).state" in dial_block
    assert "0.0f" in dial_block
    assert "0.50f" in dial_block
    assert "media_player.volume_up" not in dial_block
    assert "media_player.volume_down" not in dial_block
    assert "delay: 1s" in dial_block
    assert "sensor.rotary_encoder.set_value:" in dial_block


def test_bedside_template_startup_led_refresh_is_deferred_and_coalesced() -> None:
    source = _source()
    number_start = source.index('name: "Bedside volume cap"')
    display_start = source.index('name: "Bedside display intent"', number_start)
    sensitivity_start = source.index('name: "Wake word sensitivity"', display_start)
    number_block = source[number_start:display_start]
    display_block = source[display_start:sensitivity_start]

    for block in (number_block, display_block):
        assert "script.execute: request_led_refresh" in block
        assert "script.execute: control_leds" not in block

    refresh_start = source.index("- id: request_led_refresh")
    control_start = source.index("- id: control_leds", refresh_start)
    refresh = source[refresh_start:control_start]
    assert "mode: restart" in refresh
    delay = refresh.index("- delay: 1ms")
    control = refresh.index("- script.execute: control_leds")
    assert delay < control


def test_bedside_leds_are_last_idle_priority_and_define_requested_effects() -> None:
    source = _source()
    control = source.index("- id: control_leds")
    startup = source.index("- id: control_leds_voice_kit_startup_failed", control)
    reducer = source[control:startup]
    ordered = [
        "voice_kit_component).is_failed",
        "improv_ble_in_progress",
        "init_in_progress",
        "!id(wifi_id).is_connected() || !id(api_id).is_connected()",
        "id(center_button).state",
        "jack_plugged_recently",
        "jack_unplugged_recently",
        "dial_touched",
        "timer_ringing",
        "voice_assist_waiting_for_command_phase_id",
        "voice_assist_listening_for_command_phase_id",
        "voice_assist_thinking_phase_id",
        "voice_assist_replying_phase_id",
        "voice_assist_error_phase_id",
        "voice_assist_not_ready_phase_id",
        "else if (id(is_timer_active))",
        "else if (id(master_mute_switch).state)",
        "else if (id(external_media_player).volume == 0.0f",
        "else if (id(voice_assistant_phase) == ${voice_assist_idle_phase_id})",
        'bedside_display_intent).current_option() == "playing"',
        'bedside_display_intent).current_option() == "paused"',
        'bedside_display_intent).current_option() == "sleeping"',
    ]
    positions = [reducer.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert 'name: "Bedside Playing"' in source
    assert "id(bedside_playing_color)" in source
    assert 'name: "Bedside Paused"' in source
    assert "id(bedside_paused_color)" in source
    assert 'name: "Bedside Sleeping"' in source
    assert "id(bedside_sleeping_color)" in source
    assert "num_leds: 12" in source


def test_firmware_theme_payload_is_fixed_atomic_persisted_and_fail_closed() -> None:
    source = _source()
    assert 'initial_value: \'"v1|#00FF30@012|#FF7000@018|#6000A0@008|#18BBF2@010"\'' in source
    assert "max_restore_data_length: 50" in source
    assert 'name: "Bedside LED theme"' in source
    assert "min_length: 50" in source
    assert "max_length: 50" in source
    assert "restore_value: yes" in source

    parser_start = source.index("- id: apply_bedside_led_theme")
    parser_end = source.index("- id: request_led_refresh", parser_start)
    parser = source[parser_start:parser_end]
    assert "payload.size() != 50" in parser
    assert "brightness < 1 || brightness > 100" in parser
    assert "Invalid Bedside LED theme payload" in parser
    validate = parser.index("for (uint8_t style = 0; style < 4; style++)")
    assign = parser.index("id(bedside_playing_color) = colors[0]")
    persist = parser.index("id(bedside_led_theme_payload) = payload")
    assert validate < assign < persist


def test_all_configurable_effects_read_theme_and_button_never_reads_led_ring() -> None:
    source = _source()
    effect_start = source.index('name: "Center Button Touched"')
    effect_end = source.index('name: "Twinkle"', effect_start)
    button_effect = source[effect_start:effect_end]
    assert "id(bedside_button_press_color)" in button_effect
    assert "led_ring" not in button_effect

    script_start = source.index("- id: control_leds_center_button_touched")
    script_end = source.index("- id: control_leds_timer_ringing", script_start)
    button_script = source[script_start:script_end]
    assert "id(bedside_button_press_brightness)" in button_script
    assert "led_ring" not in button_script

    expected = (
        ("control_leds_bedside_playing", "bedside_playing_brightness"),
        ("control_leds_bedside_paused", "bedside_paused_brightness"),
        ("control_leds_bedside_sleeping", "bedside_sleeping_brightness"),
    )
    for script_id, brightness_id in expected:
        start = source.index(f"- id: {script_id}")
        end = source.index("\n  #", start)
        assert f"id({brightness_id})" in source[start:end]


def test_firmware_tracks_only_placeholders_and_documents_operator_gates() -> None:
    source = _source()
    example = (
        ROOT / "firmware" / "voice-pe" / "secrets.example.yaml"
    ).read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    readme_text = " ".join(readme.split())
    wifi = source[source.index("\nwifi:"):source.index("\nnetwork:")]
    assert "ssid:" not in wifi
    assert "password:" not in wifi
    assert "improv_serial:" in source
    assert "esp32_improv:" in source
    assert "authorizer: center_button" in source
    assert "!secret api_encryption_key" in source
    assert "id: ota_esphome\n    encryption:" in source
    assert "wifi_ssid" not in source
    assert "wifi_password" not in source
    assert "wifi_ssid" not in example
    assert "wifi_password" not in example
    assert example.count("REPLACE_") == 1
    assert "api_encryption_key:" in example
    assert "AQEBAQEBAQ" not in example
    assert "SUPERVISOR_TOKEN" not in source
    assert "access_token" not in source
    assert "does not embed Wi-Fi credentials" in readme_text
    assert "Improv" in readme_text
    assert "non-erasing serial flash" in readme_text
    assert "preserve previously stored Wi-Fi credentials" in readme_text
    assert "API encryption key also protects encrypted ESPHome OTA" in readme_text
    assert "operator-managed" in readme
    assert "separate operator approval" in readme
    assert "standard 16 MB" in readme
    assert "10-second previous-or-restart rule" in readme
    assert (ROOT / "firmware" / "voice-pe" / "UPSTREAM_LICENSE").is_file()
    assert (ROOT / "firmware" / "voice-pe" / "SOUNDS_LICENSE.md").is_file()
    for path in (ROOT / "firmware" / "voice-pe").glob("*"):
        if path.suffix not in {".md", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "tree/dev/" not in text
        assert "/raw/dev/" not in text
        assert "ref: dev" not in text
