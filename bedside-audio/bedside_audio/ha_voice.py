from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Literal
from urllib.parse import quote

import requests

from .catalog import DlnaMedia
from .player import PlayerError, PlayerSnapshot

logger = logging.getLogger(__name__)
_CORE_API = "http://supervisor/core/api"
_START_SECONDS = 12.0
_IDLE_SECONDS = 2.0
_STATE_TIMEOUT = (1.0, 3.0)
_ACTION_TIMEOUT = (3.0, 10.0)
_BROWSE_TIMEOUT = (2.0, 5.0)
_STOP_TIMEOUT = (2.0, 5.0)
_STOP_STATE_TIMEOUT = (0.25, 0.5)
_STOP_SETTLE_SECONDS = 1.0
_STOP_POLL_SECONDS = 0.2
_MediaAction = Literal["play_media", "media_pause", "media_play", "media_stop", "volume_set"]


class VoicePlayerError(PlayerError):
    pass


class HomeAssistantClient:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = _CORE_API,
    ) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("Home Assistant Supervisor token is unavailable")
        if base_url != _CORE_API and session is None:
            raise ValueError("Home Assistant Core endpoint cannot be changed")
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._base_url = base_url

    @classmethod
    def from_env(cls) -> HomeAssistantClient:
        return cls(os.environ.get("SUPERVISOR_TOKEN", ""))

    def _request(
        self, method: str, path: str, *, body: dict[str, object] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> object:
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                json=body,
                timeout=timeout or (_STATE_TIMEOUT if method == "GET" else _ACTION_TIMEOUT),
                allow_redirects=False,
            )
        except (requests.RequestException, OSError) as exc:
            raise VoicePlayerError(
                f"Home Assistant Core could not be reached ({type(exc).__name__})"
            ) from None
        try:
            if not 200 <= response.status_code < 300:
                raise VoicePlayerError(
                    f"Home Assistant Core rejected the player request (HTTP {response.status_code})"
                )
            try:
                return response.json()
            except ValueError:
                raise VoicePlayerError("Home Assistant Core returned invalid player data") from None
        finally:
            response.close()

    def state(
        self, entity_id: str, *, timeout: tuple[float, float] | None = None,
    ) -> dict[str, object]:
        data = self._request(
            "GET", f"/states/{quote(entity_id, safe='')}", timeout=timeout,
        )
        if (
            not isinstance(data, dict)
            or data.get("entity_id") != entity_id
            or not isinstance(data.get("state"), str)
            or not isinstance(data.get("attributes"), dict)
        ):
            raise VoicePlayerError("Home Assistant Core returned an invalid media player state")
        return data

    def media_action(
        self, entity_id: str, action: _MediaAction, **data: object,
    ) -> None:
        if action not in ("play_media", "media_pause", "media_play", "media_stop", "volume_set"):
            raise ValueError("Unsupported Home Assistant media player action")
        response = self._request(
            "POST",
            f"/services/media_player/{action}",
            body={"entity_id": entity_id, **data},
            timeout=_STOP_TIMEOUT if action == "media_stop" else None,
        )
        if not isinstance(response, list):
            raise VoicePlayerError("Home Assistant Core did not confirm the player action")

    def browse_media(
        self, entity_id: str, media_content_id: str, media_content_type: str,
    ) -> dict[str, object]:
        if not isinstance(entity_id, str) or not re.fullmatch(
            r"media_player\.[a-z0-9_]+", entity_id,
        ):
            raise ValueError("DLNA browse player is invalid")
        if (
            not isinstance(media_content_id, str)
            or not media_content_id.startswith("media-source://dlna_dms/")
            or not isinstance(media_content_type, str)
            or not re.fullmatch(
                r"object\.container(?:\.[A-Za-z0-9_-]+)*", media_content_type,
            )
        ):
            raise ValueError("DLNA browse selection is invalid")
        data = self._request(
            "POST", "/services/media_player/browse_media?return_response",
            body={
                "entity_id": entity_id,
                "media_content_id": media_content_id,
                "media_content_type": media_content_type,
            },
            timeout=_BROWSE_TIMEOUT,
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("changed_states"), list)
            or not isinstance(data.get("service_response"), dict)
            or set(data["service_response"]) != {entity_id}
            or not isinstance(data["service_response"][entity_id], dict)
        ):
            raise VoicePlayerError("Home Assistant Core returned an invalid DLNA browse response")
        return data["service_response"][entity_id]

    def close(self) -> None:
        self._session.close()


class VoicePlayer:
    def __init__(
        self,
        client: HomeAssistantClient,
        entity_id: str,
        initial_volume: int,
        max_volume: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(initial_volume) is not int or not 0 <= initial_volume <= max_volume:
            raise ValueError("Initial Voice volume exceeds its configured limit")
        self._client = client
        self._entity_id = entity_id
        self._max_volume = max_volume
        self._volume = initial_volume
        self._clock = clock
        self._core_media_active = False
        self._verify_core_stop = False
        self._started_at = 0.0
        self._idle_since: float | None = None
        self._heard_playing = False
        self._stop_pending = False
        self._closed = False
        self._media_content_id: str | None = None

    def load(self, path: DlnaMedia) -> None:
        if self._closed:
            raise VoicePlayerError("Voice player is closed")
        if self._stop_pending:
            raise VoicePlayerError("Retry stopping Voice before starting another stream")
        if not isinstance(path, DlnaMedia) or (
            not isinstance(path.media_content_id, str)
            or not path.media_content_id.startswith("media-source://dlna_dms/")
            or any(ord(char) <= 32 or ord(char) == 127 or char == "\\"
                   for char in path.media_content_id)
            or not isinstance(path.media_content_type, str)
            or not re.fullmatch(r"video/[a-z0-9.+_-]+", path.media_content_type)
        ):
            raise VoicePlayerError("DLNA video selection is invalid")
        if self._core_media_active:
            self.stop()
        status = self._client.state(self._entity_id)["state"]
        if status in ("unavailable", "unknown", "off"):
            raise VoicePlayerError("Voice media player is unavailable")

        play_attempted = False
        try:
            self._client.media_action(
                self._entity_id, "volume_set", volume_level=self._volume / 100,
            )
            play_attempted = True
            self._client.media_action(
                self._entity_id, "play_media",
                media_content_id=path.media_content_id,
                media_content_type=path.media_content_type,
            )
        except PlayerError as exc:
            stop_error: PlayerError | None = None
            if play_attempted:
                try:
                    self._client.media_action(self._entity_id, "media_stop")
                except PlayerError as failure:
                    stop_error = failure
            if play_attempted:
                self._verify_core_stop = True
                if stop_error is None:
                    try:
                        self._confirm_idle()
                    except VoicePlayerError as failure:
                        stop_error = failure
                    else:
                        self._verify_core_stop = False
            self._stop_pending = stop_error is not None
            if stop_error is not None:
                logger.error("Voice playback failed and stop could not be confirmed: %s", exc)
                raise VoicePlayerError(
                    "Voice playback failed; Voice stop could not be confirmed"
                ) from stop_error
            raise
        self._core_media_active = True
        self._media_content_id = path.media_content_id
        self._started_at = self._clock()
        self._idle_since = None
        self._heard_playing = False

    def pause(self, paused: bool) -> None:
        if not self._core_media_active:
            raise VoicePlayerError("No Bedside stream is active on Voice")
        self._client.media_action(
            self._entity_id, "media_pause" if paused else "media_play",
        )

    def set_volume(self, volume: int) -> None:
        if type(volume) is not int or not 0 <= volume <= self._max_volume:
            raise VoicePlayerError(f"Voice volume must be 0 to {self._max_volume}")
        if self._core_media_active:
            self._client.media_action(
                self._entity_id, "volume_set", volume_level=volume / 100,
            )
        self._volume = volume

    def stop(self) -> None:
        self._stop_pending = True
        self._verify_core_stop = True
        self._core_media_active = False
        self._media_content_id = None
        self._idle_since = None
        self._heard_playing = False
        try:
            self._client.media_action(self._entity_id, "media_stop")
            self._confirm_idle()
        except VoicePlayerError as exc:
            raise VoicePlayerError("Voice stop was not confirmed") from exc
        self._stop_pending = False
        self._verify_core_stop = False

    def _confirm_idle(self) -> None:
        deadline = time.monotonic() + _STOP_SETTLE_SECONDS
        while True:
            try:
                state = self._client.state(
                    self._entity_id, timeout=_STOP_STATE_TIMEOUT,
                )["state"]
            except VoicePlayerError:
                raise VoicePlayerError("Voice stop was not confirmed") from None
            if state == "idle":
                return
            remaining = deadline - time.monotonic()
            if state not in ("playing", "paused", "buffering") or remaining <= 0:
                raise VoicePlayerError("Voice stop was not confirmed")
            time.sleep(min(_STOP_POLL_SECONDS, remaining))

    def snapshot(self) -> PlayerSnapshot:
        if not self._core_media_active:
            return PlayerSnapshot()
        current = self._client.state(self._entity_id)
        status = current["state"]
        provenance = self.transport_provenance(status, current["attributes"])
        now = self._clock()
        if status == "playing":
            self._heard_playing = True
            self._idle_since = None
            return PlayerSnapshot(active=True, provenance=provenance)
        if status == "paused":
            self._heard_playing = True
            self._idle_since = None
            return PlayerSnapshot(
                active=True, paused=True, provenance=provenance,
            )
        if status == "buffering":
            if self._idle_since is None:
                self._idle_since = now
            if (
                now - self._idle_since >= _START_SECONDS
                or not self._heard_playing and now - self._started_at >= _START_SECONDS
            ):
                return PlayerSnapshot(error="Voice media stream remained buffering")
            return PlayerSnapshot(starting=True, provenance=provenance)
        if status == "idle":
            if self._idle_since is None:
                self._idle_since = now
            grace = _IDLE_SECONDS if self._heard_playing else _START_SECONDS
            if now - self._idle_since < grace and (
                self._heard_playing or now - self._started_at < _START_SECONDS
            ):
                return PlayerSnapshot(starting=True, provenance=provenance)
            if self._heard_playing:
                self._core_media_active = False
                self._media_content_id = None
                self._idle_since = None
                self._heard_playing = False
                return PlayerSnapshot(ended=True, provenance=provenance)
            return PlayerSnapshot(error="Voice stopped before playback started")
        return PlayerSnapshot(error="Voice media player state is unavailable")

    def transport_provenance(
        self, state: str, attributes: dict[str, object],
    ) -> bool | None:
        media_content_id = attributes.get("media_content_id")
        if self._media_content_id is None or not isinstance(media_content_id, str):
            return None
        return media_content_id == self._media_content_id

    def close(self) -> None:
        if self._closed:
            return
        try:
            if (
                self._core_media_active or self._stop_pending or self._verify_core_stop
            ):
                self.stop()
        except VoicePlayerError as exc:
            logger.error("Voice stop during shutdown: %s", exc)
        finally:
            self._closed = True
            self._client.close()
