from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager, contextmanager
from html import escape
from pathlib import Path
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Callable, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .catalog import DlnaLibraryCatalog, MediaError
from .config import Settings
from .controller import ControlError, PlaybackController
from .dlna_catalog import DlnaCatalog
from .ha_voice import HomeAssistantClient, VoicePlayer
from .hardware_bridge import CoreHardwareBridge, DisabledHardwareBridge
from .owner import OwnerError, OwnerStore
from .persistence import StateError, StateStore
from .player import Player, PlayerError

if TYPE_CHECKING:
    from .dlna_catalog import DlnaBrowser


class HardwareBridge(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def status(self) -> dict[str, object]: ...

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
INGRESS_GATEWAY = "172.30.32.2"
logger = logging.getLogger(__name__)


class PlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class SkipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str


class PlaylistPlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)


class ShuffleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class VolumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    volume: int = Field(strict=True, ge=0, le=50)


class TimerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(strict=True, ge=1, le=720)


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["delete-local-plex-credentials"]


@contextmanager
def _single_instance(state_dir: Path):
    descriptor = os.open(state_dir / "service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another bedside-audio service already owns this state directory") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ingress_path(request: Request) -> str:
    paths = request.headers.getlist("x-ingress-path")
    path = paths[0] if len(paths) == 1 else ""
    if len(path) > 512 or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?", path):
        raise ControlError(403, "Home Assistant Ingress path is unavailable")
    return f"{path.rstrip('/')}/"


def create_app(
    settings: Settings,
    player: Player | None = None,
    *,
    run_worker: bool = True,
    dlna_browser: DlnaBrowser | None = None,
    hardware_bridge_factory: (
        Callable[[PlaybackController], HardwareBridge] | None
    ) = None,
    queue_shuffler: Callable[[list[str]], None] | None = None,
) -> FastAPI:
    store = StateStore(settings.state_dir, settings.initial_volume)
    asset_version = hashlib.sha256(
        INDEX_HTML.encode("utf-8")
        + (STATIC_DIR / "app.js").read_bytes()
        + (STATIC_DIR / "style.css").read_bytes()
    ).hexdigest()[:12]
    playback_transition = RLock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.prepare_dir()
        with _single_instance(settings.state_dir):
            controller: PlaybackController | None = None
            browse_client: HomeAssistantClient | None = None
            voice_client: HomeAssistantClient | None = None
            stopped = Event()
            worker: Thread | None = None
            hardware_bridge: HardwareBridge | None = None
            try:
                owner = OwnerStore(settings.state_dir, settings.dlna.owner_user_id)
                browser = dlna_browser
                if browser is None:
                    browse_client = HomeAssistantClient.from_env()
                    browser = browse_client
                catalog = DlnaLibraryCatalog(DlnaCatalog(settings.dlna, browser))
                if player is None:
                    voice_client = HomeAssistantClient.from_env()
                    active_player: Player = VoicePlayer(
                        voice_client, settings.voice.entity_id,
                        settings.initial_volume, settings.voice.max_volume,
                    )
                else:
                    active_player = player
                controller = PlaybackController(
                    catalog,
                    active_player,
                    store,
                    max_volume=settings.voice.max_volume,
                    playlists=settings.playlists,
                    queue_shuffler=queue_shuffler,
                )
                app.state.owner = owner
                app.state.controller = controller
                app.state.browse_verified_owner = None
                if hardware_bridge_factory is not None:
                    hardware_bridge = hardware_bridge_factory(controller)
                elif settings.hardware.enabled:
                    try:
                        hardware_bridge = CoreHardwareBridge.from_env(
                            settings.hardware, settings.voice.entity_id, controller,
                        )
                    except ValueError as exc:
                        logger.warning("Voice hardware bridge is disabled: %s", exc)
                        hardware_bridge = DisabledHardwareBridge(str(exc))
                else:
                    hardware_bridge = DisabledHardwareBridge(
                        settings.hardware.disabled_reason
                        or "Hardware bridge is not configured"
                    )
                app.state.hardware_bridge = hardware_bridge
                await hardware_bridge.start()
                if run_worker:
                    worker = Thread(
                        target=lambda: _poll(controller, stopped, 1.0),
                        name="bedside-audio-poller",
                        daemon=True,
                    )
                    worker.start()
                yield
            finally:
                stopped.set()
                if worker is not None:
                    worker.join(timeout=5)
                try:
                    if hardware_bridge is not None:
                        await hardware_bridge.close()
                finally:
                    try:
                        if controller is not None:
                            controller.shutdown()
                        elif voice_client is not None:
                            voice_client.close()
                    finally:
                        if browse_client is not None:
                            browse_client.close()

    app = FastAPI(title="Bedside audio", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def ingress_only(request: Request, call_next):
        client = request.client.host if request.client else ""
        users = request.headers.getlist("x-remote-user-id")
        ha_user_id = users[0] if len(users) == 1 else ""
        if client != INGRESS_GATEWAY or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", ha_user_id,
        ):
            return JSONResponse({"detail": "Home Assistant Ingress is required"}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.getlist("x-bedside-control") != ["1"]:
                return JSONResponse({"detail": "Control header is required"}, 403)
            origins = request.headers.getlist("origin")
            if len(origins) > 1:
                return JSONResponse({"detail": "Cross-origin controls are not allowed"}, 403)
            origin = origins[0] if origins else None
            if origin:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    return JSONResponse({"detail": "Cross-origin controls are not allowed"}, 403)
                try:
                    valid_origin = (
                        parsed.scheme in ("http", "https")
                        and parsed.hostname is not None
                        and parsed.port != 0
                        and parsed.username is None
                        and parsed.password is None
                        and not parsed.path
                        and not parsed.query
                        and not parsed.fragment
                    )
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    return JSONResponse({"detail": "Cross-origin controls are not allowed"}, 403)
            if request.headers.getlist("sec-fetch-site") not in (
                [], ["same-origin"], ["none"],
            ):
                return JSONResponse({"detail": "Cross-origin controls are not allowed"}, 403)
        owner: OwnerStore = request.app.state.owner
        if request.url.path.startswith("/api/") and request.url.path != "/api/owner/status":
            try:
                owner.require_owner(ha_user_id)
            except OwnerError as exc:
                return JSONResponse({"detail": exc.safe_message}, exc.status_code)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if (
            request.url.path.startswith("/api/")
            or request.url.path.startswith("/static/")
            or request.url.path == "/"
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(MediaError)
    async def media_error(request: Request, exc: MediaError):
        return JSONResponse({"detail": str(exc)}, exc.status_code)

    @app.exception_handler(ControlError)
    async def control_error(request: Request, exc: ControlError):
        return JSONResponse({"detail": str(exc)}, exc.status_code)

    @app.exception_handler(PlayerError)
    async def player_error(request: Request, exc: PlayerError):
        return JSONResponse({"detail": str(exc)}, 502)

    @app.exception_handler(StateError)
    async def state_error(request: Request, exc: StateError):
        return JSONResponse({"detail": str(exc)}, 500)

    @app.exception_handler(OwnerError)
    async def owner_error(request: Request, exc: OwnerError):
        return JSONResponse({"detail": exc.safe_message}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'][1:]))}: {error['msg']}"
            for error in exc.errors()
        )
        return JSONResponse({"detail": details or "Invalid request"}, 422)

    def controller(request: Request) -> PlaybackController:
        return request.app.state.controller

    def owner(request: Request) -> OwnerStore:
        return request.app.state.owner

    @app.get("/")
    def index(request: Request):
        html = (
            INDEX_HTML.replace("{{BASE_PATH}}", escape(_ingress_path(request), quote=True))
            .replace("{{ASSET_VERSION}}", asset_version)
        )
        return HTMLResponse(html)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/owner/status")
    def owner_status(request: Request):
        ha_user_id = request.headers["x-remote-user-id"]
        permitted = owner(request).owner_user_id == ha_user_id
        legacy_present = owner(request).legacy_present() if permitted else False
        return {
            "owner_access": permitted,
            "dlna_enabled": True,
            "legacy_credentials_present": legacy_present,
            "legacy_cleanup_ready": (
                legacy_present and not owner(request).migration_needs_restart
                and request.app.state.browse_verified_owner == ha_user_id
            ),
        }

    @app.get("/api/state")
    def state(request: Request):
        owner(request).require_owner(request.headers["x-remote-user-id"])
        result = controller(request).state()
        result["hardware_bridge"] = request.app.state.hardware_bridge.status()
        return result

    @app.get("/api/library")
    def library(request: Request, path: str = ""):
        ha_user_id = request.headers["x-remote-user-id"]
        owner(request).require_owner(ha_user_id)
        listing = controller(request).catalog.list_dir(path)
        if path == "dlna" or path.startswith("dlna/"):
            request.app.state.browse_verified_owner = ha_user_id
        return listing

    @app.get("/api/playlists")
    def playlists(request: Request):
        owner(request).require_owner(request.headers["x-remote-user-id"])
        return {"playlists": controller(request).configured_playlists()}

    @app.post("/api/owner/cleanup-legacy")
    def cleanup_legacy(request: Request, body: CleanupRequest):
        with playback_transition:
            ha_user_id = request.headers["x-remote-user-id"]
            owner(request).require_owner(ha_user_id)
            state = controller(request).state()
            if state["playback"] != "stopped" or state["stop_pending"]:
                raise ControlError(409, "Stop playback and confirm Voice is stopped first")
            return owner(request).cleanup_legacy(
                ha_user_id,
                browse_verified=request.app.state.browse_verified_owner == ha_user_id,
            )

    @app.post("/api/play")
    def play(request: Request, body: PlayRequest):
        with playback_transition:
            return controller(request).play(body.path)

    @app.post("/api/playlists/play")
    def play_playlist(request: Request, body: PlaylistPlayRequest):
        with playback_transition:
            return controller(request).play_playlist(body.name)

    @app.post("/api/shuffle")
    def shuffle(request: Request, body: ShuffleRequest):
        with playback_transition:
            return controller(request).shuffle_folder(body.path)

    @app.post("/api/toggle-pause")
    def toggle_pause(request: Request):
        with playback_transition:
            return controller(request).toggle_pause()

    @app.post("/api/skip")
    def skip(request: Request, body: SkipRequest):
        with playback_transition:
            return controller(request).skip(body.direction)

    @app.post("/api/volume")
    def volume(request: Request, body: VolumeRequest):
        return controller(request).set_volume(body.volume)

    @app.post("/api/stop")
    def stop(request: Request):
        state = controller(request).stop()
        if state["error"]:
            raise PlayerError(str(state["error"]))
        return state

    @app.post("/api/timer")
    def timer(request: Request, body: TimerRequest):
        return controller(request).set_timer(body.minutes)

    @app.delete("/api/timer")
    def cancel_timer(request: Request):
        return controller(request).cancel_timer()

    return app


def _poll(controller: PlaybackController, stopped: Event, interval: float) -> None:
    while not stopped.wait(interval):
        controller.tick()
