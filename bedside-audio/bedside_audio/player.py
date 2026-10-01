from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .catalog import DlnaMedia


class PlayerError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    active: bool = False
    paused: bool = False
    starting: bool = False
    ended: bool = False
    error: str | None = None
    provenance: bool | None = None


class Player(Protocol):
    def load(self, media: DlnaMedia) -> None: ...

    def pause(self, paused: bool) -> None: ...

    def set_volume(self, volume: int) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> PlayerSnapshot: ...

    def transport_provenance(
        self, state: str, attributes: dict[str, object],
    ) -> bool | None: ...

    def close(self) -> None: ...
