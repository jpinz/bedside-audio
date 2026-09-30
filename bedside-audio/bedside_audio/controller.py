from __future__ import annotations

import logging
import random
import time
from threading import RLock
from typing import Callable

from .catalog import MediaError, MediaSource
from .config import PlaylistSettings
from .persistence import SavedSession, StateError, StateStore
from .player import Player, PlayerError

logger = logging.getLogger(__name__)


class ControlError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class PlaybackController:
    def __init__(
        self,
        catalog: MediaSource,
        player: Player,
        store: StateStore,
        *,
        max_volume: int,
        clock: Callable[[], float] = time.monotonic,
        playlists: tuple[PlaylistSettings, ...] = (),
        queue_shuffler: Callable[[list[str]], None] | None = None,
    ) -> None:
        if type(max_volume) is not int or not 1 <= max_volume <= 100:
            raise ValueError("Voice maximum volume must be 1 to 100")
        self.catalog = catalog
        self.player = player
        self.store = store
        self.clock = clock
        self.max_volume = max_volume
        self.playlists = {playlist.name: playlist for playlist in playlists}
        self.queue_shuffler = queue_shuffler or random.shuffle
        self._lock = RLock()
        try:
            saved = store.load()
            self._error: str | None = None
        except StateError as exc:
            saved = SavedSession(None, store.initial_volume)
            self._error = str(exc)
            logger.error("Saved session could not be loaded: %s", exc)
        if saved.volume > max_volume:
            self._error = f"Saved volume exceeded this player's limit and was reduced to {max_volume}%"
            saved = SavedSession(saved.path, max_volume, saved.name, saved.display_path)

        self._current = None
        self._current_name = None
        self._display_path = None
        self._volume = saved.volume
        self._playback = "stopped"
        self._queue: list[str] = []
        self._queue_index = -1
        self._queue_kind = "folder"
        self._queue_name: str | None = None
        self._timer_deadline: float | None = None
        self._stop_pending = False
        self._transport_generation = 0
        self._owns_transport = False
        self._position_elapsed = 0.0
        self._position_started_at: float | None = None
        self._position_reliable = False
        self._expected_paused = False

    def _persist(self) -> None:
        try:
            self.store.save(SavedSession(
                self._current, self._volume, self._current_name, self._display_path,
            ))
        except StateError as exc:
            self._error = str(exc)
            raise

    def _silence(self) -> str | None:
        try:
            self.player.stop()
        except PlayerError as exc:
            self._stop_pending = True
            return f"Voice stop was not confirmed: {exc}"
        self._stop_pending = False
        self._revoke_transport_ownership()
        return None

    def _revoke_transport_ownership(self) -> None:
        self._owns_transport = False
        self._position_started_at = None
        self._position_reliable = False

    def _expire_timer(self) -> None:
        self._timer_deadline = None
        failure = self._silence()
        self._playback = "error" if failure else "stopped"
        self._error = failure
        if failure:
            logger.error("Sleep timer could not stop playback: %s", failure)
        self._persist()

    def _load_prepared(
        self,
        target,
        queue: list[str],
        index: int,
        *,
        queue_kind: str,
        queue_name: str | None,
    ) -> bool:
        relative = queue[index]
        if self._timer_deadline is not None and self.clock() >= self._timer_deadline:
            self._expire_timer()
            return False
        self.player.set_volume(self._volume)
        if self._timer_deadline is not None and self.clock() >= self._timer_deadline:
            self._expire_timer()
            return False
        try:
            self.player.load(target)
        except PlayerError:
            self._silence()
            raise
        if self._timer_deadline is not None and self.clock() >= self._timer_deadline:
            self._expire_timer()
            return False

        self._current = relative
        self._current_name = target.name
        self._display_path = target.display_path
        self._queue = list(queue)
        self._queue_index = index
        self._queue_kind = queue_kind
        self._queue_name = queue_name
        self._playback = "playing"
        self._transport_generation += 1
        self._owns_transport = True
        self._position_elapsed = 0.0
        self._position_started_at = None
        self._position_reliable = True
        self._expected_paused = False
        self._error = None
        self._persist()
        return True

    def _play_file(self, relative: str) -> bool:
        target, queue, index = self.catalog.prepare(relative)
        return self._load_prepared(
            target,
            queue,
            index,
            queue_kind="folder",
            queue_name=None,
        )

    def _play_queue_item(
        self,
        queue: list[str],
        index: int,
        *,
        queue_kind: str,
        queue_name: str | None,
    ) -> bool:
        relative = queue[index]
        target = self.catalog.file(relative)
        return self._load_prepared(
            target,
            queue,
            index,
            queue_kind=queue_kind,
            queue_name=queue_name,
        )

    def _tick(self) -> None:
        if self._timer_deadline is not None and self.clock() >= self._timer_deadline:
            self._expire_timer()
            return
        if self._playback not in ("playing", "paused", "buffering"):
            return

        snapshot = self.player.snapshot()
        if snapshot.provenance is False:
            self._revoke_transport_ownership()
        if snapshot.error:
            logger.error("Player reported playback failure: %s", snapshot.error)
            self._playback = "error"
            self._error = snapshot.error
            failure = self._silence()
            if failure:
                self._error = f"{snapshot.error}. {failure}"
            self._persist()
            return
        if snapshot.starting:
            self._playback = "buffering"
            return
        if not snapshot.active:
            self._playback = "stopped"
            self._revoke_transport_ownership()
            self._persist()
            return
        if snapshot.paused != self._expected_paused:
            self._position_reliable = False
        elif not snapshot.paused and self._position_started_at is None:
            self._position_started_at = self.clock()
        self._playback = "paused" if snapshot.paused else "playing"

    def tick(self) -> None:
        with self._lock:
            try:
                self._tick()
            except (MediaError, PlayerError, StateError) as exc:
                self._error = str(exc)
                self._playback = "error"
                failure = self._silence()
                if failure:
                    self._error = f"{exc}. {failure}"
                logger.error("Playback polling failed: %s", self._error)

    def state(self) -> dict[str, object]:
        self.tick()
        with self._lock:
            queue = (
                {
                    "index": self._queue_index,
                    "length": len(self._queue),
                    "previous": self._queue_index > 0,
                    "next": self._queue_index + 1 < len(self._queue),
                    "kind": self._queue_kind,
                    "name": self._queue_name,
                }
                if self._queue and self._queue_index >= 0 else None
            )
            remaining = (
                max(0.0, self._timer_deadline - self.clock())
                if self._timer_deadline is not None else None
            )
            current = (
                {
                    "path": self._current,
                    "name": self._current_name,
                    "display_path": self._display_path,
                }
                if self._current else None
            )
            position = None
            if self._owns_transport and self._position_reliable:
                position = self._position_elapsed
                if self._position_started_at is not None:
                    position += max(0.0, self.clock() - self._position_started_at)
            result: dict[str, object] = {
                "playback": self._playback,
                "current": current,
                "position": position,
                "duration": None,
                "volume": self._volume,
                "queue": queue,
                "timer_remaining": remaining,
                "stop_pending": self._stop_pending,
                "resume_available": False,
                "playlists_configured": bool(self.playlists),
                "owns_transport": self._owns_transport,
                "transport_generation": self._transport_generation,
                "capabilities": {
                    "seek": False,
                    "resume": False,
                    "auto_advance": False,
                    "position": self._position_reliable,
                    "max_volume": self.max_volume,
                },
                "error": self._error,
            }
        result["media_error"] = self.catalog.health(self._current)
        return result

    def observe_transport(
        self,
        state: str,
        attributes: dict[str, object],
        *,
        live: bool,
    ) -> None:
        with self._lock:
            provenance = self.player.transport_provenance(state, attributes)
            if (
                provenance is False
                or (
                    provenance is None
                    and (not live or state not in ("playing", "paused", "buffering"))
                )
            ):
                self._revoke_transport_ownership()

    def observe_play_media(
        self,
        media_content_id: object,
        *,
        owned_call: bool,
    ) -> None:
        with self._lock:
            provenance = self.player.transport_provenance(
                "playing",
                {"media_content_id": media_content_id},
            )
            if (
                not owned_call
                or provenance is not True
                or self._current is None
                or self._playback not in ("playing", "paused", "buffering")
                or self._stop_pending
            ):
                self._revoke_transport_ownership()
                return
            self._owns_transport = True

    def play(self, relative: str) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            try:
                if not self._play_file(relative):
                    raise ControlError(409, "The sleep timer expired; choose playback again")
            except (MediaError, PlayerError) as exc:
                self._error = str(exc)
                raise
        return self.state()

    def configured_playlists(self) -> list[dict[str, str]]:
        return [{"name": playlist.name} for playlist in self.playlists.values()]

    def play_playlist(self, name: str) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            playlist = self.playlists.get(name)
            if playlist is None:
                raise ControlError(404, "Configured playlist not found")
            try:
                queue = self.catalog.configured_queue(playlist.items)
                if not self._play_queue_item(
                    queue,
                    0,
                    queue_kind="playlist",
                    queue_name=playlist.name,
                ):
                    raise ControlError(409, "The sleep timer expired; choose playback again")
            except (MediaError, PlayerError) as exc:
                self._error = str(exc)
                raise
        return self.state()

    def shuffle_folder(self, relative: str) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            try:
                queue, display_path = self.catalog.folder_queue(relative)
                self.queue_shuffler(queue)
                if not self._play_queue_item(
                    queue,
                    0,
                    queue_kind="shuffle",
                    queue_name=display_path,
                ):
                    raise ControlError(409, "The sleep timer expired; choose playback again")
            except (MediaError, PlayerError) as exc:
                self._error = str(exc)
                raise
        return self.state()

    def toggle_pause(self) -> dict[str, object]:
        with self._lock:
            self.tick()
            if self._playback not in ("playing", "paused"):
                raise ControlError(409, "Nothing is playing or paused")
            paused = self._playback == "playing"
            self.player.pause(paused)
            now = self.clock()
            if paused and self._position_started_at is not None:
                self._position_elapsed += max(0.0, now - self._position_started_at)
                self._position_started_at = None
            elif not paused:
                self._position_started_at = now
            self._expected_paused = paused
            self._playback = "paused" if paused else "playing"
            self._persist()
        return self.state()

    def restart_current(self) -> dict[str, object]:
        with self._lock:
            self._require_ready()
            if not self._owns_transport or not self._current:
                raise ControlError(409, "No Bedside episode is active")
            try:
                if not self._play_queue_item(
                    self._queue,
                    self._queue_index,
                    queue_kind=self._queue_kind,
                    queue_name=self._queue_name,
                ):
                    raise ControlError(409, "The sleep timer expired; choose playback again")
            except (MediaError, PlayerError) as exc:
                self._error = str(exc)
                raise
        return self.state()

    def skip(self, direction: str) -> dict[str, object]:
        with self._lock:
            if self._timer_deadline is not None and self.clock() >= self._timer_deadline:
                self._tick()
                raise ControlError(409, "The sleep timer expired; choose playback again")
            self._require_ready()
            if direction not in ("previous", "next"):
                raise ControlError(422, "Direction must be previous or next")
            if not self._queue or self._queue_index < 0:
                raise ControlError(409, "Select an episode before skipping")
            index = self._queue_index + (-1 if direction == "previous" else 1)
            if not 0 <= index < len(self._queue):
                raise ControlError(409, f"No {direction} video in this queue")
            try:
                if not self._play_queue_item(
                    self._queue,
                    index,
                    queue_kind=self._queue_kind,
                    queue_name=self._queue_name,
                ):
                    raise ControlError(409, "The sleep timer expired; choose playback again")
            except (MediaError, PlayerError) as exc:
                self._error = str(exc)
                raise
        return self.state()

    def set_volume(self, volume: int) -> dict[str, object]:
        if type(volume) is not int or not 0 <= volume <= self.max_volume:
            raise ControlError(
                422, f"Volume must be an integer from 0 to {self.max_volume}",
            )
        with self._lock:
            self.player.set_volume(volume)
            self._volume = volume
            self._persist()
        return self.state()

    def stop(self) -> dict[str, object]:
        with self._lock:
            failure = self._silence()
            self._playback = "error" if failure else "stopped"
            self._error = failure
            if failure:
                logger.error("Stop could not be confirmed: %s", failure)
            self._persist()
        return self.state()

    def set_timer(self, minutes: int) -> dict[str, object]:
        if type(minutes) is not int or not 1 <= minutes <= 720:
            raise ControlError(422, "Sleep timer must be 1 to 720 whole minutes")
        with self._lock:
            self._require_ready()
            self._timer_deadline = self.clock() + minutes * 60
        return self.state()

    def cancel_timer(self) -> dict[str, object]:
        with self._lock:
            self._timer_deadline = None
        return self.state()

    def _require_ready(self) -> None:
        if self._stop_pending:
            raise ControlError(409, "Retry stopping Voice before playing audio")
        if self._playback == "error":
            raise ControlError(409, "Stop Voice before starting playback again")

    def shutdown(self) -> None:
        with self._lock:
            self._timer_deadline = None
            try:
                self._persist()
            except StateError as exc:
                logger.error("Could not save session during shutdown: %s", exc)
            finally:
                self.player.close()
