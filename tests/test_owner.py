from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from bedside_audio import owner as owner_module
from bedside_audio.owner import OwnerError, OwnerStore


OWNER = "synthetic-ha-owner"
FAKE_TOKEN = "synthetic-fixture-token-not-for-plex"
LEGACY_RECORD = {
    "account_id": 42,
    "title": "Fixture Listener",
    "token": FAKE_TOKEN,
    "ha_user_id": OWNER,
}


def write_private(path: Path, record: object) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)


def test_fresh_owner_requires_explicit_configuration_and_survives_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = OwnerStore(state_dir, OWNER)

    assert owner.owner_user_id == OWNER
    assert owner.legacy_present() is False
    assert json.loads((state_dir / "owner.json").read_text()) == {
        "version": 1,
        "ha_user_id": OWNER,
    }
    assert json.loads((state_dir / "owner-bound.json").read_text()) == {
        "version": 1,
        "ha_user_id": OWNER,
    }
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((state_dir / "owner.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state_dir / "owner-bound.json").stat().st_mode) == 0o600
    assert OwnerStore(state_dir, None).owner_user_id == OWNER


@pytest.mark.parametrize("token_kind", [None, "jwt", "pin"])
def test_migration_extracts_only_ha_owner_from_synthetic_v040_record(
    tmp_path: Path, token_kind: str | None,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    record = {**LEGACY_RECORD}
    if token_kind is not None:
        record["token_kind"] = token_kind
    write_private(legacy, record)

    owner = OwnerStore(state_dir, None)

    assert owner.owner_user_id == OWNER
    assert owner.legacy_present() is True
    assert json.loads((state_dir / "owner.json").read_text()) == {
        "version": 1,
        "ha_user_id": OWNER,
    }
    assert FAKE_TOKEN not in (state_dir / "owner.json").read_text()
    assert FAKE_TOKEN not in (state_dir / "owner-bound.json").read_text()
    assert FAKE_TOKEN not in repr(owner)
    assert legacy.exists()
    assert OwnerStore(state_dir, None).owner_user_id == OWNER


def test_owner_marker_alone_blocks_another_configured_user_after_owner_loss(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    OwnerStore(state_dir, OWNER)
    (state_dir / "owner.json").unlink()

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, "different-ha-user")
    assert error.value.status_code == 503
    assert not (state_dir / "owner.json").exists()
    assert (state_dir / "owner-bound.json").exists()


def test_failed_marker_install_never_deletes_legacy_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    original = legacy.read_bytes()
    real_link = os.link

    def fail_marker(source: str, target: str, **kwargs: object) -> None:
        if target == "owner-bound.json":
            raise OSError(f"synthetic failure {FAKE_TOKEN}")
        real_link(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "link", fail_marker)
        with pytest.raises(OwnerError) as error:
            OwnerStore(state_dir, None)
    assert error.value.status_code == 503
    assert FAKE_TOKEN not in repr(error.value)
    assert legacy.read_bytes() == original
    assert not (state_dir / "owner-bound.json").exists()
    assert not list(state_dir.glob(".owner-*"))
    assert OwnerStore(state_dir, None).owner_user_id == OWNER


@pytest.mark.parametrize("tamper", ["symlink", "wrong_owner", "insecure_mode"])
def test_corrupt_owner_marker_cannot_be_repaired_by_an_option(
    tmp_path: Path, tamper: str,
) -> None:
    state_dir = tmp_path / "state"
    OwnerStore(state_dir, OWNER)
    marker = state_dir / "owner-bound.json"
    if tamper == "symlink":
        marker.unlink()
        marker.symlink_to(state_dir / "owner.json")
    elif tamper == "wrong_owner":
        write_private(marker, {"version": 1, "ha_user_id": "other-ha-user"})
    else:
        marker.chmod(0o644)

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert json.loads((state_dir / "owner.json").read_text())["ha_user_id"] == OWNER


def test_legacy_cleanup_requires_owner_survival_across_a_restart(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    first_start = OwnerStore(state_dir, None)

    with pytest.raises(OwnerError, match="Restart") as error:
        first_start.cleanup_legacy(OWNER, browse_verified=True)
    assert error.value.status_code == 409
    assert legacy.exists()

    restarted = OwnerStore(state_dir, None)
    assert restarted.cleanup_legacy(OWNER, browse_verified=True) == {
        "deleted_files": 1,
        "plex_revocation": "not_attempted",
    }
    assert restarted.owner_user_id == OWNER


def test_cleanup_needs_owner_and_verified_browse_and_removes_only_named_files(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    write_private(state_dir / "plex_auth.json", LEGACY_RECORD)
    named = (
        "plex_auth.json",
        "plex_pin.json",
        "plex-client-id",
        "plex-device-private.key",
        "plex-device-public.key",
    )
    for name in named[1:]:
        (state_dir / name).write_bytes(b"synthetic-legacy-data")
    retained = ("session.json", "service.lock", ".plex-auth-unrelated")
    for name in retained:
        (state_dir / name).write_bytes(b"keep-this-synthetic-data")
    (state_dir / "other-directory").mkdir()
    OwnerStore(state_dir, None)
    owner = OwnerStore(state_dir, None)

    with pytest.raises(OwnerError) as denied:
        owner.cleanup_legacy("other-ha-user", browse_verified=True)
    assert denied.value.status_code == 403
    with pytest.raises(OwnerError) as not_browsed:
        owner.cleanup_legacy(OWNER, browse_verified=False)
    assert not_browsed.value.status_code == 409
    assert all((state_dir / name).exists() for name in named)

    assert owner.cleanup_legacy(OWNER, browse_verified=True) == {
        "deleted_files": 5,
        "plex_revocation": "not_attempted",
    }
    assert owner.legacy_present() is False
    assert all(not (state_dir / name).exists() for name in named)
    assert all(
        (state_dir / name).read_bytes() == b"keep-this-synthetic-data"
        for name in retained
    )
    assert (state_dir / "other-directory").is_dir()
    assert json.loads((state_dir / "owner.json").read_text()) == {
        "version": 1,
        "ha_user_id": OWNER,
    }
    assert OwnerStore(state_dir, None).cleanup_legacy(
        OWNER, browse_verified=True,
    ) == {"deleted_files": 0, "plex_revocation": "not_attempted"}


def test_missing_owner_fails_closed_on_every_public_access(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    owner = OwnerStore(state_dir, OWNER)
    (state_dir / "owner.json").unlink()

    accesses = (
        lambda: owner.owner_user_id,
        lambda: owner.require_owner(OWNER),
        owner.legacy_present,
        lambda: owner.cleanup_legacy(OWNER, browse_verified=True),
    )
    for access in accesses:
        with pytest.raises(OwnerError) as error:
            access()
        assert error.value.status_code == 503


@pytest.mark.parametrize("configured", [None, "", "bad/user", "x" * 129])
def test_unowned_fresh_install_rejects_missing_or_invalid_configuration(
    tmp_path: Path, configured: str | None,
) -> None:
    state_dir = tmp_path / "state"
    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, configured)
    assert error.value.status_code == 503
    assert not (state_dir / "owner.json").exists()


def test_saved_session_without_owner_is_not_treated_as_a_fresh_install(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / "session.json").write_text("synthetic old session", encoding="utf-8")

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, "different-ha-user")
    assert error.value.status_code == 503
    assert not (state_dir / "owner.json").exists()


def test_existing_owner_rejects_config_and_legacy_mismatch_even_after_startup(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = OwnerStore(state_dir, OWNER)
    original = (state_dir / "owner.json").read_bytes()
    with pytest.raises(OwnerError) as config_error:
        OwnerStore(state_dir, "other-ha-user")
    assert config_error.value.status_code == 503

    write_private(
        state_dir / "plex_auth.json",
        {**LEGACY_RECORD, "ha_user_id": "other-ha-user"},
    )
    with pytest.raises(OwnerError) as legacy_error:
        OwnerStore(state_dir, OWNER)
    assert legacy_error.value.status_code == 503
    with pytest.raises(OwnerError) as live_error:
        owner.require_owner(OWNER)
    assert live_error.value.status_code == 503
    assert (state_dir / "owner.json").read_bytes() == original


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"version": True, "ha_user_id": OWNER},
        {"version": 2, "ha_user_id": OWNER},
        {"version": 1, "ha_user_id": "bad/user"},
        {"version": 1, "ha_user_id": OWNER, "token": FAKE_TOKEN},
    ],
)
def test_invalid_owner_state_is_not_replaced_with_configured_identity(
    tmp_path: Path, record: object,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    owner_file = state_dir / "owner.json"
    write_private(owner_file, record)
    original = owner_file.read_bytes()

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert FAKE_TOKEN not in repr(error.value)
    assert owner_file.read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "directory", "insecure_mode"])
def test_unsafe_owner_file_fails_closed(tmp_path: Path, kind: str) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    owner_file = state_dir / "owner.json"
    outside = tmp_path / "outside-owner"
    write_private(outside, {"version": 1, "ha_user_id": OWNER})
    if kind == "symlink":
        owner_file.symlink_to(outside)
    elif kind == "directory":
        owner_file.mkdir()
    else:
        write_private(owner_file, {"version": 1, "ha_user_id": OWNER})
        owner_file.chmod(0o644)

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert json.loads(outside.read_text())["ha_user_id"] == OWNER


def test_symlinked_state_directory_is_rejected_without_writing_through_it(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "outside-state"
    actual.mkdir(mode=0o700)
    state_dir = tmp_path / "state"
    state_dir.symlink_to(actual, target_is_directory=True)

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert not (actual / "owner.json").exists()


def test_case_variant_owner_file_is_not_treated_as_dedicated_owner(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    write_private(state_dir / "OWNER.JSON", {"version": 1, "ha_user_id": OWNER})

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, None)
    assert error.value.status_code == 503


def test_similarly_named_legacy_files_are_never_implicitly_cleaned(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = OwnerStore(state_dir, OWNER)
    unknown = ("PLEX_AUTH.JSON", "plex_auth.json.old", ".plex-auth-unfinished")
    for name in unknown:
        (state_dir / name).write_bytes(b"synthetic-unknown-file")

    assert owner.legacy_present() is False
    assert owner.cleanup_legacy(OWNER, browse_verified=True) == {
        "deleted_files": 0,
        "plex_revocation": "not_attempted",
    }
    assert all((state_dir / name).read_bytes() == b"synthetic-unknown-file" for name in unknown)


@pytest.mark.parametrize(
    "record",
    [
        {},
        {**LEGACY_RECORD, "account_id": 0},
        {**LEGACY_RECORD, "account_id": True},
        {**LEGACY_RECORD, "title": "   "},
        {**LEGACY_RECORD, "token": ""},
        {**LEGACY_RECORD, "token": "bad\ncontrol"},
        {**LEGACY_RECORD, "token": "x" * 4097},
        {**LEGACY_RECORD, "ha_user_id": "bad/user"},
        {**LEGACY_RECORD, "token_kind": "hosted_pin"},
        {**LEGACY_RECORD, "unexpected": "field"},
        {**LEGACY_RECORD, "title": "x" * 32768},
    ],
)
def test_invalid_legacy_record_cannot_be_overridden_by_config(
    tmp_path: Path, record: object,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, record)

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert not (state_dir / "owner.json").exists()
    assert legacy.exists()


def test_legacy_reader_uses_nofollow_nonblocking_and_presence_does_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    write_private(state_dir / "plex_auth.json", LEGACY_RECORD)
    real_open = os.open
    observed_flags: list[int] = []

    def audited_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "plex_auth.json":
            observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "open", audited_open)
        owner = OwnerStore(state_dir, None)
    assert observed_flags
    assert all(flags & os.O_NOFOLLOW for flags in observed_flags)
    assert all(flags & os.O_NONBLOCK for flags in observed_flags)

    def no_legacy_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "plex_auth.json":
            raise AssertionError("presence checks must not read legacy contents")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "open", no_legacy_open)
        assert owner.owner_user_id == OWNER
        owner.require_owner(OWNER)
        assert owner.legacy_present() is True


def test_invalid_legacy_json_and_insecure_mode_fail_without_leaking_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    legacy.write_bytes(b'{"token":"' + FAKE_TOKEN.encode() + b'",broken-json')
    legacy.chmod(0o600)
    with pytest.raises(OwnerError) as corrupt:
        OwnerStore(state_dir, OWNER)
    assert corrupt.value.status_code == 503
    assert FAKE_TOKEN not in repr(corrupt.value)
    assert FAKE_TOKEN not in caplog.text

    write_private(legacy, LEGACY_RECORD)
    legacy.chmod(0o644)
    with pytest.raises(OwnerError) as insecure:
        OwnerStore(state_dir, OWNER)
    assert insecure.value.status_code == 503
    assert FAKE_TOKEN not in repr(insecure.value)
    assert not (state_dir / "owner.json").exists()


@pytest.mark.parametrize("filename", ["owner.json", "plex_auth.json"])
def test_ambiguous_duplicate_identity_keys_are_rejected(
    tmp_path: Path, filename: str,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    if filename == "owner.json":
        text = (
            '{"version":1,"ha_user_id":"' + OWNER
            + '","ha_user_id":"' + OWNER + '"}'
        )
    else:
        text = json.dumps(LEGACY_RECORD)[:-1] + ',"ha_user_id":"' + OWNER + '"}'
    path = state_dir / filename
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    if filename == "plex_auth.json":
        assert not (state_dir / "owner.json").exists()


def test_orphaned_plex_state_cannot_be_taken_over_by_configuration(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    (state_dir / "plex_pin.json").write_bytes(b"synthetic-pin-state")

    with pytest.raises(OwnerError) as error:
        OwnerStore(state_dir, OWNER)
    assert error.value.status_code == 503
    assert not (state_dir / "owner.json").exists()


@pytest.mark.parametrize("browse_verified", [False, None, 0, 1, "true"])
def test_cleanup_rejects_unverified_or_nonboolean_browse_signal(
    tmp_path: Path, browse_verified: object,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    owner = OwnerStore(state_dir, None)

    with pytest.raises(OwnerError) as error:
        owner.cleanup_legacy(OWNER, browse_verified=browse_verified)
    assert error.value.status_code == 409
    assert legacy.exists()


@pytest.mark.parametrize("operation", ["write", "file_fsync", "directory_fsync"])
def test_failed_owner_persistence_preserves_legacy_for_safe_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    original_legacy = legacy.read_bytes()
    real_fsync = os.fsync

    def fail_write(descriptor: int, content: bytes) -> int:
        raise OSError(f"synthetic failure {FAKE_TOKEN}")

    def fail_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (operation == "file_fsync" and stat.S_ISREG(metadata.st_mode)) or (
            operation == "directory_fsync" and stat.S_ISDIR(metadata.st_mode)
        ):
            raise OSError(f"synthetic failure {FAKE_TOKEN}")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        if operation == "write":
            patch.setattr(owner_module.os, "write", fail_write)
        else:
            patch.setattr(owner_module.os, "fsync", fail_fsync)
        with pytest.raises(OwnerError) as error:
            OwnerStore(state_dir, None)

    assert error.value.status_code == 503
    assert FAKE_TOKEN not in error.value.safe_message
    assert FAKE_TOKEN not in repr(error.value)
    assert legacy.read_bytes() == original_legacy
    assert not list(state_dir.glob(".owner-*"))
    if operation != "directory_fsync":
        assert not (state_dir / "owner.json").exists()
    assert OwnerStore(state_dir, None).owner_user_id == OWNER


@pytest.mark.parametrize("same_owner", [True, False])
def test_atomic_install_never_overwrites_concurrent_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_owner: bool,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    real_link = os.link
    winner = OWNER if same_owner else "other-ha-user"

    def concurrent_install(source: str, target: str, **kwargs: object) -> None:
        write_private(
            state_dir / "owner.json",
            {"version": 1, "ha_user_id": winner},
        )
        real_link(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "link", concurrent_install)
        if same_owner:
            assert OwnerStore(state_dir, None).owner_user_id == OWNER
        else:
            with pytest.raises(OwnerError) as error:
                OwnerStore(state_dir, None)
            assert error.value.status_code == 503

    assert json.loads((state_dir / "owner.json").read_text()) == {
        "version": 1,
        "ha_user_id": winner,
    }
    assert legacy.exists()
    assert not list(state_dir.glob(".owner-*"))


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_unsafe_named_target_blocks_all_cleanup_before_first_unlink(
    tmp_path: Path, kind: str,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    owner = OwnerStore(state_dir, None)
    target = state_dir / "plex-device-public.key"
    outside = tmp_path / "outside-file"
    outside.write_bytes(b"synthetic-outside-data")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)

    with pytest.raises(OwnerError) as presence_error:
        owner.legacy_present()
    assert presence_error.value.status_code == 503
    with pytest.raises(OwnerError) as cleanup_error:
        owner.cleanup_legacy(OWNER, browse_verified=True)
    assert cleanup_error.value.status_code == 503
    assert legacy.exists()
    if kind == "symlink":
        assert target.is_symlink()
    else:
        assert target.exists()
    assert outside.read_bytes() == b"synthetic-outside-data"


def test_incomplete_pin_staging_blocks_migration_and_cleanup(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    staging = state_dir / ".plex-pin-incomplete-synthetic"
    staging.write_bytes(b"synthetic-staging")

    with pytest.raises(OwnerError) as startup_error:
        OwnerStore(state_dir, OWNER)
    assert startup_error.value.status_code == 503
    assert legacy.exists()
    assert not (state_dir / "owner.json").exists()

    staging.unlink()
    owner = OwnerStore(state_dir, None)
    staging.write_bytes(b"synthetic-staging")
    with pytest.raises(OwnerError) as cleanup_error:
        owner.cleanup_legacy(OWNER, browse_verified=True)
    assert cleanup_error.value.status_code == 503
    assert legacy.exists()
    assert staging.read_bytes() == b"synthetic-staging"


@pytest.mark.parametrize(
    "name", [".plex-auth-" + "a" * 32, ".plex-auth-" + "0" * 32],
)
def test_old_owner_token_staging_blocks_startup_and_cleanup(
    tmp_path: Path, name: str,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    OwnerStore(state_dir, None)
    owner = OwnerStore(state_dir, None)
    staging = state_dir / name
    staging.write_bytes(b"synthetic-token-bearing-staging")

    with pytest.raises(OwnerError) as presence:
        owner.legacy_present()
    assert presence.value.status_code == 503
    with pytest.raises(OwnerError) as cleanup:
        owner.cleanup_legacy(OWNER, browse_verified=True)
    assert cleanup.value.status_code == 503
    with pytest.raises(OwnerError) as restart:
        OwnerStore(state_dir, None)
    assert restart.value.status_code == 503
    assert legacy.exists()
    assert staging.read_bytes() == b"synthetic-token-bearing-staging"


def test_partial_cleanup_retries_remaining_files_with_persisted_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    write_private(state_dir / "plex_auth.json", LEGACY_RECORD)
    for name in (
        "plex_pin.json",
        "plex-client-id",
        "plex-device-private.key",
        "plex-device-public.key",
    ):
        (state_dir / name).write_bytes(b"synthetic-legacy-data")
    OwnerStore(state_dir, None)
    owner = OwnerStore(state_dir, None)
    real_unlink = os.unlink

    def fail_second_target(name: str, **kwargs: object) -> None:
        if name == "plex_pin.json":
            raise OSError(f"synthetic failure {FAKE_TOKEN}")
        real_unlink(name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "unlink", fail_second_target)
        with pytest.raises(OwnerError) as error:
            owner.cleanup_legacy(OWNER, browse_verified=True)
    assert error.value.status_code == 503
    assert FAKE_TOKEN not in repr(error.value)
    assert not (state_dir / "plex_auth.json").exists()
    assert (state_dir / "plex_pin.json").exists()

    restarted = OwnerStore(state_dir, None)
    assert restarted.owner_user_id == OWNER
    assert restarted.cleanup_legacy(OWNER, browse_verified=True) == {
        "deleted_files": 4,
        "plex_revocation": "not_attempted",
    }
    assert restarted.legacy_present() is False


def test_cleanup_requires_durable_owner_before_deleting_any_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    OwnerStore(state_dir, None)
    owner = OwnerStore(state_dir, None)
    real_fsync = os.fsync

    def fail_owner_sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"synthetic failure {FAKE_TOKEN}")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "fsync", fail_owner_sync)
        with pytest.raises(OwnerError) as error:
            owner.cleanup_legacy(OWNER, browse_verified=True)
    assert error.value.status_code == 503
    assert legacy.exists()
    assert owner.cleanup_legacy(OWNER, browse_verified=True) == {
        "deleted_files": 1,
        "plex_revocation": "not_attempted",
    }


def test_cleanup_reports_directory_sync_failure_and_retries_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    legacy = state_dir / "plex_auth.json"
    write_private(legacy, LEGACY_RECORD)
    OwnerStore(state_dir, None)
    owner = OwnerStore(state_dir, None)
    real_fsync = os.fsync
    real_unlink = os.unlink
    unlinked = False

    def track_unlink(name: str, **kwargs: object) -> None:
        nonlocal unlinked
        real_unlink(name, **kwargs)
        if name == "plex_auth.json":
            unlinked = True

    def fail_after_unlink(descriptor: int) -> None:
        if unlinked and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(f"synthetic failure {FAKE_TOKEN}")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(owner_module.os, "unlink", track_unlink)
        patch.setattr(owner_module.os, "fsync", fail_after_unlink)
        with pytest.raises(OwnerError) as error:
            owner.cleanup_legacy(OWNER, browse_verified=True)
    assert error.value.status_code == 503
    assert not legacy.exists()
    assert OwnerStore(state_dir, None).cleanup_legacy(
        OWNER, browse_verified=True,
    ) == {"deleted_files": 0, "plex_revocation": "not_attempted"}
