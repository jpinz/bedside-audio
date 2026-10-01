from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bedside_audio.ha_addon import settings_from_options


def _settings(tmp_path: Path, **overrides: object):
    values: dict[str, object] = {
        "media_player_entity": "media_player.bedroom_voice",
        "dlna_source_id": "plex_media_server_example",
        "dlna_browse_player_entity_id": "media_player.example_tv",
        "dlna_owner_user_id": "",
        "max_volume": 50,
    }
    values.update(overrides)
    path = tmp_path / "options.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return settings_from_options(path)


def test_dlna_only_options_accept_pruned_and_legacy_supervisor_shapes(
    tmp_path: Path,
) -> None:
    clean = _settings(tmp_path)
    assert clean.voice.entity_id == "media_player.bedroom_voice"
    assert clean.voice.max_volume == 50
    assert clean.initial_volume == 15
    assert clean.dlna.source_id == "plex_media_server_example"
    assert clean.dlna.browse_player_entity_id == "media_player.example_tv"
    assert clean.dlna.owner_user_id is None
    assert clean.hardware.enabled is False
    assert clean.state_dir == Path("/data/bedside-audio")

    legacy = _settings(
        tmp_path,
        plex_networks="192.0.2.101/32",
        plex_ports="32400",
        plex_allow_http=True,
        plex_auth_mode="jwt",
        ha_allow_http_link=True,
        ha_http_link_origin="http://homeassistant.local:8123",
    )
    assert legacy == clean
    assert not hasattr(legacy, "plex_networks")
    assert not hasattr(legacy.voice, "allow_http_link")

    fresh = _settings(tmp_path, dlna_owner_user_id="trusted-ha-owner")
    assert fresh.dlna.owner_user_id == "trusted-ha-owner"


def test_configured_playlists_parse_ordered_dlna_title_paths(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        playlists=[
            {
                "name": "Quiet evening",
                "items": (
                    '["TV Shows","Example Show","Season 1"]\n'
                    '["TV Shows","Another Show","Season 2","Episode 3"]'
                ),
            },
            {
                "name": "One episode",
                "items": '["TV Shows","Example Show","Special"]',
            },
        ],
    )

    assert [playlist.name for playlist in settings.playlists] == [
        "Quiet evening",
        "One episode",
    ]
    assert settings.playlists[0].items == (
        ("TV Shows", "Example Show", "Season 1"),
        ("TV Shows", "Another Show", "Season 2", "Episode 3"),
    )


def test_library_folders_parse_exact_dlna_title_paths(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        library_folders=[
            {"path": '["Video","TV Shows","Example Show"]'},
            {"path": '["Video","TV Shows","Another Show"]'},
        ],
    )

    assert settings.dlna.allowed_folders == (
        ("Video", "TV Shows", "Example Show"),
        ("Video", "TV Shows", "Another Show"),
    )


def test_library_exclusions_parse_case_insensitive_glob_patterns(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        library_exclude_patterns=[
            {"pattern": "Specials"},
            {"pattern": "Season 0*"},
        ],
    )

    assert settings.dlna.excluded_title_patterns == (
        "Specials",
        "Season 0*",
    )


@pytest.mark.parametrize(
    ("patterns", "message"),
    [
        ({}, "library_exclude_patterns must be a list"),
        ([{}], "only pattern"),
        ([{"pattern": ""}], "1 to 256"),
        ([{"pattern": " Specials"}], "safe title glob patterns"),
        ([{"pattern": "/Specials"}], "safe title glob patterns"),
        (
            [{"pattern": "Specials"}, {"pattern": "SPECIALS"}],
            "unique",
        ),
    ],
)
def test_library_exclusion_patterns_fail_closed(
    tmp_path: Path, patterns: object, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, library_exclude_patterns=patterns)


@pytest.mark.parametrize(
    ("library_folders", "message"),
    [
        ({}, "library_folders must be a list"),
        ([{}], "only path"),
        ([{"path": ""}], "1 to 1024"),
        ([{"path": "not-json"}], "JSON arrays"),
        ([{"path": "{}"}], "JSON arrays"),
        ([{"path": "[]"}], "valid DLNA title paths"),
        ([{"path": '["https://example.invalid/show"]'}], "valid DLNA title paths"),
        ([{"path": ' ["Video","TV Shows"]'}], "outer whitespace"),
        ([{"path": '["Video"]', "extra": True}], "only path"),
        (
            [
                {"path": '["Video","TV Shows","Example Show"]'},
                {"path": '["Video","TV Shows","Example Show"]'},
            ],
            "unique",
        ),
    ],
)
def test_library_folders_fail_closed(
    tmp_path: Path, library_folders: object, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, library_folders=library_folders)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dlna_source_id": ""}, "dlna_source_id"),
        ({"dlna_browse_player_entity_id": ""}, "dlna_browse_player_entity_id"),
        ({"dlna_source_id": "other/../server"}, "dlna_source_id"),
        ({"dlna_browse_player_entity_id": "media_player.bedroom_voice"}, "browse player"),
        ({"dlna_owner_user_id": "../other"}, "dlna_owner_user_id"),
        ({"max_volume": 51}, "50"),
        ({"max_volume": True}, "integer"),
        ({"media_player_entity": "media_player.bad/name"}, "media_player_entity"),
        ({"unexpected": True}, "unrecognized"),
    ],
)
def test_dlna_only_options_fail_closed(
    tmp_path: Path, overrides: dict[str, object], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, **overrides)


@pytest.mark.parametrize(
    ("playlists", "message"),
    [
        ({}, "playlists must be a list"),
        ([{"name": "Empty", "items": ""}], "items must be"),
        ([{"name": "Empty", "items": "[]"}], "invalid DLNA title path"),
        ([{"name": "Bad", "items": '["TV Shows", 1]'}], "JSON arrays"),
        ([{"name": "Bad", "items": '["https://example.invalid/video"]'}], "invalid DLNA"),
        ([{"name": "Bad", "items": '["/media/tv"]'}], "invalid DLNA"),
        ([{"name": "Bad", "items": '["media-source://dlna_dms/source/:1"]'}], "invalid DLNA"),
        ([{"name": "Bad", "items": '["TV Shows"]\n\n["Other"]'}], "non-empty lines"),
        ([{"name": "Bad", "items": ' ["TV Shows"]'}], "outer whitespace"),
        ([{"name": "Bad", "items": '["TV Shows"]', "extra": True}], "only name and items"),
        (
            [
                {"name": "Evening", "items": '["TV Shows"]'},
                {"name": "EVENING", "items": '["TV Shows"]'},
            ],
            "unique",
        ),
    ],
)
def test_configured_playlists_fail_closed(
    tmp_path: Path, playlists: object, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _settings(tmp_path, playlists=playlists)


def test_configured_playlist_item_and_playlist_limits(tmp_path: Path) -> None:
    too_many_items = "\n".join(
        f'["TV Shows","Episode {index}"]' for index in range(65)
    )
    with pytest.raises(ValueError, match="1 to 64"):
        _settings(
            tmp_path,
            playlists=[{"name": "Too long", "items": too_many_items}],
        )

    with pytest.raises(ValueError, match="at most 16"):
        _settings(
            tmp_path,
            playlists=[
                {"name": f"Playlist {index}", "items": '["TV Shows"]'}
                for index in range(17)
            ],
        )


def test_manifest_does_not_expose_inert_plex_options() -> None:
    app_root = Path(__file__).parents[1] / "bedside-audio"
    manifest = (app_root / "config.yaml").read_text(encoding="utf-8")
    dockerfile = (app_root / "Dockerfile").read_text(encoding="utf-8")
    pyproject = tomllib.loads(
        (app_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "dlna_source_id:" in manifest
    assert "dlna_browse_player_entity_id:" in manifest
    assert "dlna_owner_user_id:" in manifest
    assert "plex_" not in manifest
    assert "ha_allow_http_link:" not in manifest
    assert "ha_http_link_origin:" not in manifest
    assert "max_volume: 50" in manifest
    assert "max_volume: \"int(1,50)\"" in manifest
    assert 'version: "0.7.0"' in manifest
    assert "image: ghcr.io/jpinz/bedside-audio" in manifest
    assert 'io.hass.version="0.7.0"' in dockerfile
    assert pyproject["project"]["version"] == "0.7.0"
    assert "library_exclude_patterns: []" in manifest
    assert 'pattern: "str(1,256)"' in manifest
    assert "library_folders: []" in manifest
    assert 'path: "str(1,1024)"' in manifest
    assert "playlists: []" in manifest
    assert 'name: "str(1,64)"' in manifest
    assert 'items: "str(1,8192)"' in manifest
    assert 'voice_button_event_entity: ""' in manifest
    assert 'voice_assist_satellite_entity: ""' in manifest
    assert 'voice_led_light_entity: ""' in manifest
    assert 'voice_led_select_entity: ""' in manifest
    assert 'voice_volume_cap_number_entity: ""' in manifest


def test_official_app_repository_layout_is_installable() -> None:
    root = Path(__file__).parents[1]
    app_root = root / "bedside-audio"
    repository = (root / "repository.yaml").read_text(encoding="utf-8")
    assert "name: Bedside Audio" in repository
    assert "url: https://github.com/jpinz/bedside-audio" in repository
    assert not (root / "config.yaml").exists()
    assert not (root / "Dockerfile").exists()
    for name in (
        "config.yaml",
        "Dockerfile",
        "README.md",
        "DOCS.md",
        "CHANGELOG.md",
        "pyproject.toml",
    ):
        assert (app_root / name).is_file()
    assert (app_root / "bedside_audio" / "ha_addon.py").is_file()


def test_hardware_options_require_exact_complete_entities_or_disable_fail_closed(
    tmp_path: Path,
) -> None:
    enabled = _settings(
        tmp_path,
        voice_button_event_entity="event.bedroom_voice_button",
        voice_assist_satellite_entity="assist_satellite.bedroom_voice",
        voice_led_light_entity="light.bedroom_voice_ring",
        voice_led_select_entity="",
        voice_volume_cap_number_entity="number.bedroom_voice_volume_cap",
    )
    assert enabled.hardware.enabled is True
    assert (
        enabled.hardware.volume_cap_number_entity
        == "number.bedroom_voice_volume_cap"
    )

    for overrides in (
        {"voice_button_event_entity": "event.bedroom_voice_button"},
        {
            "voice_button_event_entity": "event.bad/name",
            "voice_assist_satellite_entity": "assist_satellite.bedroom_voice",
            "voice_led_light_entity": "light.bedroom_voice_ring",
        },
        {
            "voice_button_event_entity": "event.bedroom_voice_button",
            "voice_assist_satellite_entity": "assist_satellite.bedroom_voice",
            "voice_led_light_entity": "light.bedroom_voice_ring",
            "voice_led_select_entity": "select.bedroom_voice_ring_mode",
        },
        {
            "voice_button_event_entity": "event.bedroom_voice_button",
            "voice_assist_satellite_entity": "assist_satellite.bedroom_voice",
            "voice_led_light_entity": "light.bedroom_voice_ring",
            "voice_volume_cap_number_entity": "number.bad/name",
        },
    ):
        disabled = _settings(tmp_path, **overrides)
        assert disabled.hardware.enabled is False
        assert disabled.hardware.disabled_reason


def test_runtime_dependencies_do_not_install_plex_credentials_stack() -> None:
    pyproject = tomllib.loads(
        (
            Path(__file__).parents[1]
            / "bedside-audio"
            / "pyproject.toml"
        ).read_text(encoding="utf-8")
    )
    dependencies = pyproject["project"]["dependencies"]
    assert any(
        dependency.lower().startswith("websockets")
        for dependency in dependencies
    )
    assert not any(
        dependency.lower().startswith(("plexapi", "cryptography", "pyjwt", "urllib3"))
        for dependency in dependencies
    )
