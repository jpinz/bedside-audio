"""Persist the Home Assistant owner independently of legacy Plex credentials."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_OWNER_FILE = "owner.json"
_OWNER_MARKER = "owner-bound.json"
_LEGACY_FILES = (
    "plex_auth.json",
    "plex_pin.json",
    "plex-client-id",
    "plex-device-private.key",
    "plex-device-public.key",
)
_MISSING = object()
_OWNER_ERROR = "Home Assistant owner state is unavailable"
_LEGACY_ERROR = "Legacy Plex credentials need operator review"
_OWNER_MISMATCH = "This player belongs to another Home Assistant user"
_BROWSE_REQUIRED = "Browse the DLNA library before removing legacy credentials"


def _valid_user_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _legacy_signature(metadata: os.stat_result | None) -> tuple[int, ...] | None:
    if metadata is None:
        return None
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns, stat.S_IMODE(metadata.st_mode),
    )


class OwnerError(Exception):
    def __init__(self, status_code: int, safe_message: str) -> None:
        self.status_code = status_code
        self.safe_message = safe_message
        super().__init__(safe_message)


class OwnerStore:
    """Bind HA ownership to private state, never to the first Ingress visitor."""

    def __init__(self, state_dir: Path, configured_user_id: str | None) -> None:
        if configured_user_id is not None and not _valid_user_id(configured_user_id):
            raise OwnerError(503, _OWNER_ERROR)

        self._state_dir = state_dir
        with self._directory(create=True) as directory:
            existing = self._read_owner(directory, durable=True)
            marker = self._read_owner(directory, name=_OWNER_MARKER, durable=True)
            legacy = self._scan_legacy(directory)
            if (
                (marker is not _MISSING and existing is _MISSING)
                or (
                    marker is not _MISSING and existing is not _MISSING
                    and marker != existing
                )
            ):
                raise OwnerError(503, _OWNER_ERROR)
            migrated_this_start = existing is _MISSING and "plex_auth.json" in legacy
            if "plex_auth.json" in legacy:
                legacy_user_id = self._read_legacy_owner(directory)
                if existing is not _MISSING and existing != legacy_user_id:
                    raise OwnerError(503, _LEGACY_ERROR)
                if configured_user_id is not None and configured_user_id != legacy_user_id:
                    raise OwnerError(503, _LEGACY_ERROR)
                expected = legacy_user_id
            elif legacy and existing is _MISSING:
                raise OwnerError(503, _LEGACY_ERROR)
            else:
                if existing is _MISSING and self._has_prior_state(directory):
                    raise OwnerError(503, _OWNER_ERROR)
                expected = existing if existing is not _MISSING else configured_user_id
            if existing is _MISSING:
                if expected is None:
                    raise OwnerError(503, _OWNER_ERROR)
                self._create_owner(directory, expected)
                existing = self._read_owner(directory, durable=True)
            if marker is _MISSING:
                self._create_record(directory, _OWNER_MARKER, existing)
                marker = self._read_owner(
                    directory, name=_OWNER_MARKER, durable=True,
                )
            if existing is _MISSING or (
                marker != existing or (
                    configured_user_id is not None and configured_user_id != existing
                )
            ):
                raise OwnerError(503, _OWNER_ERROR)
            self._owner_user_id = existing
            self._migration_needs_restart = migrated_this_start
            self._legacy_identity_signature = _legacy_signature(legacy.get("plex_auth.json"))

    @property
    def migration_needs_restart(self) -> bool:
        return self._migration_needs_restart

    @property
    def owner_user_id(self) -> str:
        with self._directory() as directory:
            self._verify_owner(directory, check_legacy=True)
        return self._owner_user_id

    def require_owner(self, ha_user_id: str) -> None:
        with self._directory() as directory:
            self._verify_owner(directory, check_legacy=True)
        if ha_user_id != self._owner_user_id:
            raise OwnerError(403, _OWNER_MISMATCH)

    def legacy_present(self) -> bool:
        with self._directory() as directory:
            self._verify_owner(directory)
            return bool(self._scan_legacy(directory))

    def cleanup_legacy(
        self, ha_user_id: str, *, browse_verified: bool,
    ) -> dict[str, object]:
        """Delete local Plex files only after a server-verified Ingress DLNA browse."""

        self.require_owner(ha_user_id)
        if browse_verified is not True:
            raise OwnerError(409, _BROWSE_REQUIRED)

        with self._directory() as directory:
            self._verify_owner(directory, durable=True)
            legacy = self._scan_legacy(directory)
            if legacy and self._migration_needs_restart:
                raise OwnerError(
                    409, "Restart Bedside and browse DLNA again before removing legacy credentials",
                )
            if (
                "plex_auth.json" in legacy
                and self._read_legacy_owner(directory) != self._owner_user_id
            ):
                raise OwnerError(503, _LEGACY_ERROR)

            deleted = 0
            try:
                for name, metadata in legacy.items():
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or (current.st_dev, current.st_ino) != (
                            metadata.st_dev, metadata.st_ino,
                        )
                    ):
                        raise OwnerError(503, _LEGACY_ERROR)
                    os.unlink(name, dir_fd=directory)
                    if name == "plex_auth.json":
                        self._legacy_identity_signature = None
                    deleted += 1
                os.fsync(directory)
            except (OSError, OwnerError) as exc:
                if deleted:
                    try:
                        os.fsync(directory)
                    except OSError:
                        pass
                if isinstance(exc, OwnerError):
                    raise
                raise OwnerError(503, _LEGACY_ERROR) from None
            return {"deleted_files": deleted, "plex_revocation": "not_attempted"}

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int]:
        if create:
            try:
                self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                raise OwnerError(503, _OWNER_ERROR) from None
        try:
            directory = os.open(
                self._state_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError:
            raise OwnerError(503, _OWNER_ERROR) from None
        try:
            mode = stat.S_IMODE(os.fstat(directory).st_mode)
            if mode != 0o700:
                if not create:
                    raise OwnerError(503, _OWNER_ERROR)
                os.fchmod(directory, 0o700)
                os.fsync(directory)
            yield directory
        except OSError:
            raise OwnerError(503, _OWNER_ERROR) from None
        finally:
            try:
                os.close(directory)
            except OSError:
                raise OwnerError(503, _OWNER_ERROR) from None

    @staticmethod
    def _read_json(
        directory: int, name: str, limit: int, error: str, *, durable: bool = False,
    ) -> object:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return _MISSING
        except OSError:
            raise OwnerError(503, error) from None

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OwnerError(503, error)
            if metadata.st_size > limit:
                raise OwnerError(503, error)
            data = bytearray()
            try:
                while len(data) <= limit:
                    chunk = os.read(descriptor, limit + 1 - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > limit:
                    raise OwnerError(503, error)
                result = json.loads(
                    data.decode("utf-8"), object_pairs_hook=_unique_object,
                )
                if durable:
                    os.fsync(descriptor)
                return result
            finally:
                data.clear()
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise OwnerError(503, error) from None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                raise OwnerError(503, error) from None

    def _read_owner(
        self, directory: int, *, name: str = _OWNER_FILE, durable: bool = False,
    ) -> str | object:
        try:
            if name not in os.listdir(directory):
                return _MISSING
        except OSError:
            raise OwnerError(503, _OWNER_ERROR) from None
        data = self._read_json(
            directory, name, 2048, _OWNER_ERROR, durable=durable,
        )
        if data is _MISSING:
            raise OwnerError(503, _OWNER_ERROR)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "ha_user_id"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not _valid_user_id(data["ha_user_id"])
        ):
            raise OwnerError(503, _OWNER_ERROR)
        if durable:
            try:
                os.fsync(directory)
            except OSError:
                raise OwnerError(503, _OWNER_ERROR) from None
        return data["ha_user_id"]

    @staticmethod
    def _has_prior_state(directory: int) -> bool:
        try:
            names = os.listdir(directory)
        except OSError:
            raise OwnerError(503, _OWNER_ERROR) from None
        return "session.json" in names or any(name.startswith(".owner-") for name in names)

    def _verify_owner(
        self, directory: int, *, durable: bool = False, check_legacy: bool = False,
    ) -> None:
        owner = self._read_owner(directory, durable=durable)
        marker = self._read_owner(directory, name=_OWNER_MARKER, durable=durable)
        if owner is _MISSING or owner != self._owner_user_id or marker != owner:
            raise OwnerError(503, _OWNER_ERROR)
        if check_legacy:
            legacy = self._scan_legacy(directory)
            if _legacy_signature(legacy.get("plex_auth.json")) != self._legacy_identity_signature:
                raise OwnerError(503, _LEGACY_ERROR)

    def _read_legacy_owner(self, directory: int) -> str:
        data = self._read_json(directory, "plex_auth.json", 32768, _LEGACY_ERROR)
        try:
            if (
                not isinstance(data, dict)
                or not {"account_id", "title", "token", "ha_user_id"} <= set(data)
                or not set(data) <= {
                    "account_id", "title", "token", "ha_user_id", "token_kind",
                }
                or type(data["account_id"]) is not int
                or data["account_id"] <= 0
                or not isinstance(data["title"], str)
                or not data["title"].strip()
                or not isinstance(data["token"], str)
                or not 0 < len(data["token"]) <= 4096
                or not all(33 <= ord(char) <= 126 for char in data["token"])
                or not _valid_user_id(data["ha_user_id"])
                or data.get("token_kind", "jwt") not in ("jwt", "pin")
            ):
                raise OwnerError(503, _LEGACY_ERROR)
            return data["ha_user_id"]
        finally:
            del data

    @staticmethod
    def _scan_legacy(directory: int) -> dict[str, os.stat_result]:
        try:
            names = set(os.listdir(directory))
            if any(
                name.startswith(".plex-pin-")
                or re.fullmatch(r"\.plex-auth-[0-9a-f]{32}", name)
                for name in names
            ):
                raise OwnerError(503, _LEGACY_ERROR)
            found = {}
            for name in _LEGACY_FILES:
                if name not in names:
                    continue
                try:
                    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    raise OwnerError(503, _LEGACY_ERROR) from None
                if not stat.S_ISREG(metadata.st_mode):
                    raise OwnerError(503, _LEGACY_ERROR)
                found[name] = metadata
            return found
        except OSError:
            raise OwnerError(503, _LEGACY_ERROR) from None

    def _create_owner(self, directory: int, user_id: str) -> None:
        self._create_record(directory, _OWNER_FILE, user_id)

    def _create_record(self, directory: int, name: str, user_id: str) -> None:
        temporary = f".owner-{secrets.token_hex(16)}"
        content = json.dumps({"version": 1, "ha_user_id": user_id}).encode("utf-8") + b"\n"
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            temporary_exists = True
            try:
                os.fchmod(descriptor, 0o600)
                offset = 0
                while offset < len(content):
                    written = os.write(descriptor, content[offset:])
                    if written == 0:
                        raise OwnerError(503, _OWNER_ERROR)
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(
                    temporary, name,
                    src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False,
                )
            except FileExistsError:
                if self._read_owner(directory, name=name) != user_id:
                    raise OwnerError(503, _OWNER_ERROR)
            os.unlink(temporary, dir_fd=directory)
            temporary_exists = False
            os.fsync(directory)
        except OSError:
            raise OwnerError(503, _OWNER_ERROR) from None
        finally:
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
                except OSError:
                    raise OwnerError(503, _OWNER_ERROR) from None
