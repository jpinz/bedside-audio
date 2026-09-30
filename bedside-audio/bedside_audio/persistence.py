from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class StateError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SavedSession:
    path: str | None
    volume: int
    name: str | None = None
    display_path: str | None = None


class StateStore:
    def __init__(self, state_dir: Path, initial_volume: int):
        self.state_dir = state_dir
        self.initial_volume = initial_volume
        self.path = state_dir / "session.json"

    def prepare_dir(self) -> None:
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not stat.S_ISDIR(self.state_dir.lstat().st_mode):
                raise StateError("Invalid private state directory")
            self.state_dir.chmod(0o700)
        except OSError as exc:
            raise StateError(f"Cannot prepare private state directory: {exc}") from exc

    def load(self) -> SavedSession:
        try:
            with self.path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return SavedSession(None, self.initial_volume)
        except (OSError, ValueError) as exc:
            raise StateError(f"Cannot read saved session: {exc}") from exc

        if not isinstance(data, dict) or data.get("version") != 1:
            raise StateError("Saved session has an invalid format")
        path = data.get("path")
        volume = data.get("volume")
        name = data.get("name")
        display_path = data.get("display_path")
        if (
            (path is not None and (not isinstance(path, str) or not path))
            or (name is not None and (not isinstance(name, str) or not name))
            or (display_path is not None and not isinstance(display_path, str))
            or type(data.get("position")) not in (int, float)
            or not math.isfinite(data["position"])
            or data["position"] < 0
            or type(volume) is not int
            or not 0 <= volume <= 100
        ):
            raise StateError("Saved session has invalid values")
        return SavedSession(path, volume, name, display_path)

    def save(self, session: SavedSession) -> None:
        self.prepare_dir()
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.state_dir, prefix=".session-",
                delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(
                    {
                        # Keep the installed Voice app's saved-state format readable on rollback.
                        "version": 1,
                        "path": session.path,
                        "position": 0.0,
                        "volume": session.volume,
                        "name": session.name,
                        "display_path": session.display_path,
                    },
                    stream,
                )
                stream.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StateError(f"Cannot save session: {exc}") from exc
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
