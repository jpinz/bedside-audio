from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from bedside_audio.catalog import DlnaMedia
from bedside_audio.player import PlayerError, PlayerSnapshot


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePlayer:
    def __init__(self):
        self.loads: list[DlnaMedia] = []
        self.active = False
        self.paused = False
        self.starting = False
        self.media_content_id: str | None = None
        self.volume = 15
        self.error: str | None = None
        self.stops = 0
        self.closes = 0
        self.fail_load = False
        self.fail_stop = False
        self.stopped_event = Event()

    def load(self, media: DlnaMedia) -> None:
        if self.fail_load:
            raise PlayerError("file could not be decoded")
        self.loads.append(media)
        self.media_content_id = media.media_content_id
        self.active = True
        self.paused = False
        self.error = None
        self.stopped_event.clear()

    def pause(self, paused: bool) -> None:
        self.paused = paused

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def stop(self) -> None:
        if self.fail_stop:
            raise PlayerError("Voice stop failed")
        self.stops += 1
        self.active = False
        self.error = None
        self.stopped_event.set()

    def snapshot(self) -> PlayerSnapshot:
        return PlayerSnapshot(
            active=self.active,
            paused=self.paused,
            starting=self.starting,
            error=self.error,
        )

    def close(self) -> None:
        self.closes += 1
        self.active = False
        self.stopped_event.set()

    def transport_provenance(
        self, state: str, attributes: dict[str, object],
    ) -> bool | None:
        media_content_id = attributes.get("media_content_id")
        if not isinstance(media_content_id, str) or self.media_content_id is None:
            return None
        return media_content_id == self.media_content_id
