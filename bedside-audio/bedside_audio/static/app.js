(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const ui = {
    remote: $("remote"),
    ownerHeading: $("owner-heading"),
    ownerStatus: $("owner-status"),
    ownerError: $("owner-error"),
    retryOwner: $("retry-owner"),
    legacyCleanup: $("legacy-cleanup"),
    cleanupStart: $("cleanup-start"),
    cleanupConfirm: $("cleanup-confirm"),
    cleanupConfirmDelete: $("cleanup-confirm-delete"),
    cleanupCancel: $("cleanup-cancel"),
    cleanupNotice: $("cleanup-notice"),
    cleanupError: $("cleanup-error"),
    connection: $("connection-status"),
    retryState: $("retry-state"),
    actionError: $("action-error"),
    playbackStatus: $("playback-status"),
    currentName: $("current-name"),
    currentPath: $("current-path"),
    queueStatus: $("queue-status"),
    previous: $("previous"),
    playPause: $("play-pause"),
    next: $("next"),
    stop: $("stop"),
    volume: $("volume"),
    volumeLabel: $("volume-label"),
    volumeValue: $("volume-value"),
    timerStatus: $("timer-status"),
    timerForm: $("timer-form"),
    timerMinutes: $("timer-minutes"),
    setTimer: $("set-timer"),
    cancelTimer: $("cancel-timer"),
    playerErrors: $("player-errors"),
    libraryHeading: $("library-heading"),
    libraryPath: $("library-path"),
    playlists: $("playlists"),
    playlistsMessage: $("playlists-message"),
    playlistsError: $("playlists-error"),
    retryPlaylists: $("retry-playlists"),
    playlistEntries: $("playlist-entries"),
    libraryGuidance: $("library-guidance"),
    folderActions: $("folder-actions"),
    playFolder: $("play-folder"),
    shuffleFolder: $("shuffle-folder"),
    folderUp: $("folder-up"),
    libraryMessage: $("library-message"),
    libraryError: $("library-error"),
    retryLibrary: $("retry-library"),
    entries: $("entries"),
  };

  let ownerStatus = null;
  let ownerChecking = true;
  let ownerStatusError = "";
  let ownerStatusRequest = null;
  let ownerStatusVersion = 0;
  let accessDenied = false;
  let cleanupReadinessChecked = false;
  let cleanupConfirmOpen = false;
  let cleanupPending = false;
  let cleanupRequestId = 0;
  let cleanupNotice = "";
  let cleanupError = "";

  let state = null;
  let connection = "loading";
  let connectionError = "";
  let actionError = "";
  let stateRequest = null;
  let stateVersion = 0;
  let actionId = 0;
  let controlPending = null;
  let controlPendingKind = "";
  let stopRequest = null;
  let stopping = false;
  let changingVolume = false;

  let library = null;
  let libraryLoading = false;
  let libraryError = "";
  let requestedPath = "";
  let libraryRequestId = 0;

  let playlists = null;
  let playlistsLoading = false;
  let playlistsError = "";
  let playlistsRequestId = 0;

  class ApiError extends Error {
    constructor(message, kind, status = null) {
      super(message);
      this.kind = kind;
      this.status = status;
    }
  }

  async function request(path, method = "GET", body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), method === "GET" ? 8000 : 15000);
    const options = { method, cache: "no-store", credentials: "same-origin", signal: controller.signal };
    if (method !== "GET") {
      options.headers = {
        "Content-Type": "application/json",
        "X-Bedside-Control": "1",
      };
      if (method === "POST") options.body = JSON.stringify(body === undefined ? {} : body);
    }

    let response;
    let text;
    try {
      const url = new URL(path.startsWith("/api/") ? path.slice(1) : path, document.baseURI);
      response = await fetch(url, options);
      text = await response.text();
    } catch (error) {
      if (error.name === "AbortError") {
        throw new ApiError("The player did not respond in time.", "offline");
      }
      throw new ApiError("Connection to the player was lost.", "offline");
    } finally {
      clearTimeout(timeout);
    }

    let data;
    try {
      data = JSON.parse(text);
    } catch {
      throw new ApiError(
        response.status === 401
          ? "Home Assistant session expired."
          : "Home Assistant session may have expired.",
        response.status === 401 ? "unauthorized" : "server",
        response.status,
      );
    }
    if (!response.ok) {
      throw new ApiError(
        data && typeof data.detail === "string" ? data.detail : `The player returned ${response.status}.`,
        response.status === 401 ? "unauthorized" : "server",
        response.status,
      );
    }
    return data;
  }

  function requireOwnerStatus(data) {
    if (!data || typeof data.owner_access !== "boolean" || data.dlna_enabled !== true ||
        typeof data.legacy_credentials_present !== "boolean" ||
        typeof data.legacy_cleanup_ready !== "boolean" ||
        (data.error != null && typeof data.error !== "string")) {
      throw new ApiError("Unexpected player access status.", "server");
    }
    if (data.error) throw new ApiError(data.error, "server");
    return data;
  }

  function requireState(data) {
    if (!data || typeof data.playback !== "string") {
      throw new ApiError("Unexpected player state.", "server");
    }
    return data;
  }

  function isDlnaPath(path) {
    return typeof path === "string" && (path === "dlna" || path.startsWith("dlna/"));
  }

  function requireListing(data, path) {
    if (!data || data.path !== path || !Array.isArray(data.entries) ||
        !(data.parent === null || typeof data.parent === "string") ||
        (path && !isDlnaPath(path)) ||
        data.entries.some((entry) => !entry || typeof entry.name !== "string" ||
          !entry.name.trim() || !["folder", "file"].includes(entry.kind) ||
          (path === ""
            ? entry.path !== "dlna" || entry.kind !== "folder"
            : typeof entry.path !== "string" || !entry.path.startsWith("dlna/")))) {
      throw new ApiError("Unexpected TV library response.", "server");
    }
    return data;
  }

  function requirePlaylists(data) {
    if (!data || !Array.isArray(data.playlists) ||
        data.playlists.some((playlist) => !playlist ||
          typeof playlist.name !== "string" || !playlist.name.trim()) ||
        new Set(data.playlists.map((playlist) => playlist.name)).size !== data.playlists.length) {
      throw new ApiError("Unexpected playlist response.", "server");
    }
    return data.playlists;
  }

  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = String(total % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${remaining}` : `${minutes}:${remaining}`;
  }

  function setRange(input, value, maximum) {
    input.value = String(value);
    input.style.setProperty("--progress", `${maximum > 0 ? Math.min(100, Math.max(0, value / maximum * 100)) : 0}%`);
  }

  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }

  function canUseRemote() {
    return Boolean(ownerStatus && ownerStatus.owner_access && ownerStatus.dlna_enabled && !accessDenied);
  }

  function canCleanup() {
    return Boolean(canUseRemote() && !ownerChecking &&
      ownerStatus.legacy_credentials_present && ownerStatus.legacy_cleanup_ready);
  }

  function clearProtectedData() {
    stateVersion++;
    actionId++;
    state = null;
    connection = "loading";
    connectionError = "";
    actionError = "";
    controlPending = null;
    controlPendingKind = "";
    stopRequest = null;
    stopping = false;
    changingVolume = false;
    libraryRequestId++;
    library = null;
    libraryLoading = false;
    libraryError = "";
    requestedPath = "";
    playlistsRequestId++;
    playlists = null;
    playlistsLoading = false;
    playlistsError = "";
    cleanupRequestId++;
    cleanupReadinessChecked = false;
    cleanupConfirmOpen = false;
    cleanupPending = false;
    cleanupNotice = "";
    cleanupError = "";
    ui.entries.textContent = "";
    ui.playlistEntries.textContent = "";
    renderState();
  }

  function renderOwner() {
    ui.remote.hidden = !canUseRemote();
    setText(ui.ownerStatus, ownerChecking && !ownerStatus
      ? "Checking player access…"
      : ownerStatusError || accessDenied
        ? "Player access unavailable."
        : ownerStatus && ownerStatus.owner_access
          ? "TV library available."
          : "Use the Home Assistant account that owns this player.");
    setText(ui.ownerError, ownerStatusError);
    ui.ownerError.hidden = !ownerStatusError;
    ui.retryOwner.hidden = ownerChecking ||
      (!ownerStatusError && !accessDenied && (!ownerStatus || ownerStatus.owner_access));
    ui.retryOwner.disabled = ownerChecking;

    const available = canCleanup();
    if (!available) cleanupConfirmOpen = false;
    ui.legacyCleanup.hidden = !available;
    ui.cleanupStart.disabled = cleanupPending;
    ui.cleanupStart.setAttribute("aria-expanded", String(available && cleanupConfirmOpen));
    ui.cleanupConfirm.hidden = !available || !cleanupConfirmOpen;
    ui.cleanupConfirmDelete.disabled = cleanupPending;
    ui.cleanupCancel.disabled = cleanupPending;
    setText(ui.cleanupNotice, cleanupPending ? "Deleting local Plex credentials…" : cleanupNotice);
    ui.cleanupNotice.hidden = !cleanupPending && !cleanupNotice;
    setText(ui.cleanupError, cleanupError);
    ui.cleanupError.hidden = !cleanupError;
    renderState();
  }

  async function refreshOwnerStatus(force = false) {
    if (ownerStatusRequest && !force) return ownerStatusRequest;
    const version = ++ownerStatusVersion;
    ownerChecking = true;
    ownerStatusError = "";
    renderOwner();
    const pending = (async () => {
      try {
        const next = requireOwnerStatus(await request("/api/owner/status"));
        if (version !== ownerStatusVersion) return;
        const wasAvailable = canUseRemote();
        ownerStatus = next;
        if (canUseRemote()) {
          if (!wasAvailable || !state || !library || playlists === null) {
            void refreshState(true);
            void loadLibrary("");
            void loadPlaylists();
          }
        } else {
          if (wasAvailable || state || library || cleanupPending || cleanupConfirmOpen) {
            clearProtectedData();
          }
          if (!next.owner_access) {
            cleanupNotice = "";
            cleanupError = "";
          }
        }
      } catch (error) {
        if (version !== ownerStatusVersion) return;
        ownerStatus = null;
        ownerStatusError = `Could not check player access: ${error.message}`;
        if (error.kind === "unauthorized" || error.status === 403) accessDenied = true;
        clearProtectedData();
      } finally {
        if (version === ownerStatusVersion) {
          ownerChecking = false;
          renderOwner();
        }
      }
    })();
    ownerStatusRequest = pending;
    try {
      await pending;
    } finally {
      if (ownerStatusRequest === pending) ownerStatusRequest = null;
      renderOwner();
    }
  }

  function handleAccessDenied() {
    if (accessDenied || !canUseRemote()) return;
    accessDenied = true;
    ownerStatus = null;
    clearProtectedData();
    void refreshOwnerStatus(true);
  }

  function cancelCleanup() {
    if (cleanupPending || !cleanupConfirmOpen) return;
    cleanupConfirmOpen = false;
    renderOwner();
    ui.cleanupStart.focus();
  }

  async function cleanupLegacy() {
    if (cleanupPending || !cleanupConfirmOpen || !canCleanup()) return;
    const id = ++cleanupRequestId;
    cleanupPending = true;
    cleanupNotice = "";
    cleanupError = "";
    renderOwner();
    try {
      const result = await request("/api/owner/cleanup-legacy", "POST",
        { confirm: "delete-local-plex-credentials" });
      if (!result || !Number.isInteger(result.deleted_files) || result.deleted_files < 0 ||
          result.plex_revocation !== "not_attempted") {
        throw new ApiError("The cleanup was not confirmed.", "server");
      }
      if (id !== cleanupRequestId || !canUseRemote()) return;
      ownerStatus = { ...ownerStatus, legacy_credentials_present: false, legacy_cleanup_ready: false };
      cleanupNotice = "Local Plex credentials deleted. Plex server access was not revoked; no app-data backup was made.";
    } catch (error) {
      if (id !== cleanupRequestId) return;
      if (error.kind === "unauthorized" || error.status === 403) {
        handleAccessDenied();
        return;
      }
      cleanupError = `Could not delete local Plex credentials: ${error.message}`;
    } finally {
      if (id === cleanupRequestId) {
        cleanupPending = false;
        cleanupConfirmOpen = false;
        renderOwner();
        if (cleanupNotice) ui.ownerHeading.focus();
        else if (canCleanup()) ui.cleanupStart.focus();
      }
    }
  }

  function renderEntries() {
    if (!library) return;
    const canPlay = canUseRemote() && connection === "online" && state &&
      state.playback !== "error" && !state.stop_pending && controlPending === null &&
      !stopping;
    for (const button of ui.entries.querySelectorAll("button.entry")) {
      const isFile = button.dataset.kind === "file";
      const disabled = isFile && !canPlay;
      if (button.disabled !== disabled) button.disabled = disabled;
      if (!isFile) continue;
      const current = Boolean(state && state.current && state.current.path === button.dataset.path);
      if (button.dataset.current !== String(current)) {
        button.dataset.current = String(current);
        const occurrence = button.dataset.folderPosition
          ? `, video ${button.dataset.folderPosition} of ${button.dataset.folderLength} in folder` : "";
        button.setAttribute(
          "aria-label",
          `Play ${current ? "current video " : ""}${button.dataset.name}${occurrence} from beginning`,
        );
        if (current) button.setAttribute("aria-current", "true");
        else button.removeAttribute("aria-current");
        button.querySelector(".entry-current").hidden = !current;
      }
    }
  }

  function renderPlaylists() {
    const configured = Boolean(state && state.playlists_configured);
    const visible = Boolean(
      playlistsError || (playlists && playlists.length) ||
      (configured && playlistsLoading),
    );
    ui.playlists.hidden = !visible;
    setText(
      ui.playlistsMessage,
      configured && playlistsLoading ? "Loading configured playlists…" : "",
    );
    ui.playlistsMessage.hidden = !ui.playlistsMessage.textContent;
    setText(
      ui.playlistsError,
      playlistsError ? `Could not load playlists: ${playlistsError}` : "",
    );
    ui.playlistsError.hidden = !playlistsError;
    ui.retryPlaylists.hidden = !playlistsError;
    ui.retryPlaylists.disabled = playlistsLoading;
    const ready = canUseRemote() && connection === "online" && state &&
      state.playback !== "error" && !state.stop_pending && controlPending === null &&
      !stopping;
    for (const button of ui.playlistEntries.querySelectorAll("button.playlist-entry")) {
      button.disabled = !ready;
      const pending = controlPendingKind === `playlist:${button.dataset.name}`;
      button.querySelector(".playlist-action").textContent = pending ? "Starting…" : "Play";
    }
  }

  function renderState() {
    const online = connection === "online";
    if (canUseRemote()) {
      setText(ui.connection, online
        ? "Connected to bedside controller"
        : connection === "loading"
          ? "Connecting to player…"
          : connection === "offline"
            ? state ? "Offline. Last known state is shown." : "Offline. Player state unavailable."
            : `Player unavailable: ${connectionError}`);
      ui.retryState.hidden = connection === "loading" || online;
      ui.retryState.disabled = Boolean(stateRequest);
    } else {
      setText(ui.connection, ownerChecking && !ownerStatus
        ? "Checking player access…"
        : "Player access unavailable.");
      ui.retryState.hidden = true;
    }
    setText(ui.actionError, actionError);
    ui.actionError.hidden = !actionError;

    const stopPending = Boolean(state && state.stop_pending);
    const ready = canUseRemote() && online && state && state.playback !== "error" &&
      !stopPending && controlPending === null && !stopping;
    const canStop = canUseRemote() && online && state && !stopping;

    const current = state && state.current;
    const playback = state && state.playback;
    const statusLabels = {
      stopped: "Stopped",
      playing: "Playing",
      paused: "Paused",
      buffering: "Waiting for Voice playback…",
      error: "Playback error",
    };
    setText(ui.playbackStatus, state ? statusLabels[playback] || "Player status unavailable" : "Waiting for player…");
    ui.currentName.textContent = current ? current.name : "No video selected";
    ui.currentPath.textContent = current
      ? typeof current.display_path === "string" && current.display_path
        ? current.display_path : "TV library"
      : "Choose a video or folder below.";
    const queue = state && state.queue;
    const queuePosition = queue && Number.isInteger(queue.index) &&
      Number.isInteger(queue.length) ? `Item ${queue.index + 1} of ${queue.length}` : "";
    const queueText = queue && queue.kind === "playlist" && typeof queue.name === "string"
      ? `Playlist: ${queue.name}. ${queuePosition}`
      : queue && queue.kind === "shuffle"
        ? `Shuffled folder. ${queuePosition}`
        : "";
    setText(ui.queueStatus, queueText);
    ui.queueStatus.hidden = !queueText;

    ui.previous.disabled = !ready || !state.queue || !state.queue.previous;
    ui.next.disabled = !ready || !state.queue || !state.queue.next;
    ui.playPause.textContent = playback === "playing"
      ? "Pause"
      : playback === "paused"
        ? "Play"
        : playback === "error"
          ? "Play again"
          : "Play from start";
    ui.playPause.disabled = !ready || !current || playback === "buffering";
    ui.stop.textContent = stopping ? "Stopping…" : stopPending ? "Retry stop" : "Stop";
    ui.stop.disabled = !canStop ||
      (!stopPending && controlPending === null && (!current || playback === "stopped"));

    const maxVolume = state && state.capabilities &&
      Number.isInteger(state.capabilities.max_volume)
      ? Math.max(1, Math.min(100, state.capabilities.max_volume))
      : 50;
    ui.volume.max = String(maxVolume);
    ui.volumeLabel.textContent = `Volume (up to ${maxVolume}%)`;
    if (!changingVolume && state && Number.isFinite(state.volume)) {
      const volume = Math.max(0, Math.min(maxVolume, Math.round(state.volume)));
      setRange(ui.volume, volume, maxVolume);
      setText(ui.volumeValue, `${volume}%`);
    } else if (!state) {
      setText(ui.volumeValue, "–");
    }
    ui.volume.disabled = !ready;

    const timerRemaining = state && Number.isFinite(state.timer_remaining) ? state.timer_remaining : null;
    ui.timerStatus.textContent = !state
      ? "Waiting for player…"
      : timerRemaining === null
        ? "No timer set"
        : `Stops playback in ${formatTime(Math.ceil(timerRemaining))}`;
    ui.timerMinutes.disabled = !ready;
    ui.setTimer.disabled = !ready;
    ui.cancelTimer.hidden = timerRemaining === null;
    ui.cancelTimer.disabled = !ready;

    const errors = [];
    if (state && state.error) errors.push(`Player: ${state.error}`);
    if (state && state.media_error && state.media_error !== state.error) {
      errors.push(`Media: ${state.media_error}`);
    }
    setText(ui.playerErrors, errors.join(" "));
    ui.playerErrors.hidden = errors.length === 0;

    renderPlaylists();
    renderLibrary();
  }

  async function refreshState(force = false) {
    if (!canUseRemote()) return;
    if (stateRequest && !force) return stateRequest;
    if (force) stateVersion++;
    const version = stateVersion;
    const pending = (async () => {
      try {
        const next = requireState(await request("/api/state"));
        if (version !== stateVersion || !canUseRemote()) return;
        state = next;
        connection = "online";
        connectionError = "";
      } catch (error) {
        if (version !== stateVersion || !canUseRemote()) return;
        if (error.kind === "unauthorized" || error.status === 403) {
          handleAccessDenied();
          return;
        }
        connection = error.kind === "offline" ? "offline" : "error";
        connectionError = error.message;
      }
      renderState();
    })();
    stateRequest = pending;
    try {
      await pending;
    } finally {
      if (stateRequest === pending) stateRequest = null;
      renderState();
    }
  }

  async function control(path, body, label, options = {}) {
    if (controlPending !== null || stopping || !canUseRemote() ||
        connection !== "online" || !state) return;
    const id = ++actionId;
    controlPending = id;
    controlPendingKind = options.pending || "";
    stateVersion++;
    actionError = "";
    renderState();
    try {
      const result = await request(path, options.method || "POST", body);
      if (id !== actionId || !canUseRemote()) return;
      state = requireState(result);
      connection = "online";
      connectionError = "";
    } catch (error) {
      if (id === actionId) {
        if (error.kind === "unauthorized" || error.status === 403) {
          handleAccessDenied();
          return;
        }
        actionError = `Could not ${label}: ${error.message}`;
        if (error.kind === "offline") {
          connection = "offline";
          connectionError = error.message;
        }
      }
    } finally {
      if (controlPending === id) {
        controlPending = null;
        controlPendingKind = "";
      }
      if (id === actionId) {
        stateVersion++;
        renderState();
        if (canUseRemote()) void refreshState(true);
        if (options.focus && options.focus.isConnected && !options.focus.disabled &&
            document.activeElement === document.body) {
          options.focus.focus();
        }
      }
    }
  }

  async function stopPlayback() {
    if (stopRequest) return stopRequest;
    if (!canUseRemote() || connection !== "online" || !state) return;
    const id = ++actionId;
    stateVersion++;
    controlPending = null;
    controlPendingKind = "";
    changingVolume = false;
    stopping = true;
    actionError = "";
    renderState();

    const pending = (async () => {
      try {
        const result = await request("/api/stop", "POST");
        if (id !== actionId || !canUseRemote()) return;
        state = requireState(result);
        connection = "online";
        connectionError = "";
      } catch (error) {
        if (id !== actionId) return;
        if (error.kind === "unauthorized" || error.status === 403) {
          handleAccessDenied();
          return;
        }
        actionError = `Could not stop playback: ${error.message}`;
        if (error.kind === "offline") {
          connection = "offline";
          connectionError = error.message;
        }
      }
    })();
    stopRequest = pending;
    try {
      await pending;
    } finally {
      if (stopRequest === pending) stopRequest = null;
      if (id === actionId) {
        stopping = false;
        stateVersion++;
        renderState();
        if (canUseRemote()) void refreshState(true);
        if (ui.stop.isConnected && !ui.stop.disabled &&
            document.activeElement === document.body) {
          ui.stop.focus();
        }
      }
    }
  }

  function makeEntry(entry, folderPosition, folderLength) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const nameWrap = document.createElement("span");
    const name = document.createElement("span");
    const order = document.createElement("span");
    const current = document.createElement("span");
    const action = document.createElement("span");

    button.type = "button";
    button.className = "entry";
    button.dataset.path = entry.path;
    button.dataset.name = entry.name;
    button.dataset.kind = entry.kind;
    nameWrap.className = "entry-name-wrap";
    name.className = "entry-name";
    name.textContent = entry.name;
    order.className = "entry-order";
    order.hidden = entry.kind !== "file" || !folderLength;
    if (!order.hidden) {
      button.dataset.folderPosition = String(folderPosition);
      button.dataset.folderLength = String(folderLength);
      order.textContent = `Video ${folderPosition} of ${folderLength}`;
    }
    current.className = "entry-current";
    current.textContent = "Current video";
    current.hidden = true;
    action.className = "entry-action";
    action.textContent = entry.kind === "folder" ? "Open" : "Play";

    nameWrap.append(name, order, current);
    button.append(nameWrap, action);
    item.append(button);
    if (entry.kind === "folder") {
      button.setAttribute("aria-label", `Open folder ${entry.name}`);
      button.addEventListener("click", () => { void loadLibrary(entry.path, true); });
    } else {
      button.addEventListener("click", () => {
        void control("/api/play", { path: entry.path }, "play file", { focus: button });
      });
    }
    return item;
  }

  function makePlaylist(playlist) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const name = document.createElement("span");
    const action = document.createElement("span");

    button.type = "button";
    button.className = "playlist-entry";
    button.dataset.name = playlist.name;
    name.className = "playlist-name";
    name.textContent = playlist.name;
    action.className = "playlist-action";
    action.textContent = "Play";
    button.append(name, action);
    item.append(button);
    button.setAttribute("aria-label", `Play configured playlist ${playlist.name}`);
    button.addEventListener("click", () => {
      void control(
        "/api/playlists/play",
        { name: playlist.name },
        "start playlist",
        { focus: button, pending: `playlist:${playlist.name}` },
      );
    });
    return item;
  }

  function renderLibrary() {
    ui.libraryGuidance.textContent = !state
      ? "Waiting for player to enable playback."
      : connection !== "online"
        ? "Connection lost. Playback controls are unavailable."
        : state.stop_pending
          ? "Retry stop before choosing another file."
          : state.playback === "error"
            ? "Stop playback before choosing another file."
            : library && library.folder_queue
              ? "Use Next and Previous to move through videos in this folder manually."
              : "Choose a video or open a folder.";
    ui.libraryPath.textContent = library && typeof library.display_path === "string" && library.display_path
      ? library.display_path
      : library && isDlnaPath(library.path)
        ? "TV library" : "Library root";
    ui.folderUp.hidden = !library || library.parent === null;
    ui.folderUp.disabled = libraryLoading;
    const firstFile = library && library.folder_queue === true
      ? library.entries.find((entry) => entry.kind === "file") : null;
    const shuffleAvailable = Boolean(
      library && library.entries.length && typeof library.path === "string" &&
        library.path.startsWith("dlna/"),
    );
    ui.playFolder.hidden = !library || library.folder_queue !== true;
    ui.playFolder.disabled = !firstFile || libraryLoading || !canUseRemote() ||
      connection !== "online" || !state || state.playback === "error" ||
      state.stop_pending || controlPending !== null || stopping;
    ui.shuffleFolder.hidden = !shuffleAvailable;
    ui.shuffleFolder.textContent = controlPendingKind === "shuffle-folder"
      ? "Shuffling folder…" : "Shuffle this folder";
    ui.shuffleFolder.disabled = !shuffleAvailable || libraryLoading || !canUseRemote() ||
      connection !== "online" || !state || state.playback === "error" ||
      state.stop_pending || controlPending !== null || stopping;
    ui.folderActions.hidden = ui.playFolder.hidden && ui.shuffleFolder.hidden;
    ui.entries.hidden = libraryLoading || !library;
    ui.libraryMessage.textContent = libraryLoading
      ? "Loading files…"
      : library && library.entries.length === 0
        ? library.path === "" ? "No TV library is available yet." : "This folder is empty."
        : "";
    ui.libraryMessage.hidden = !ui.libraryMessage.textContent;
    ui.libraryError.textContent = libraryError ? `Could not load files: ${libraryError}` : "";
    ui.libraryError.hidden = !libraryError;
    ui.retryLibrary.hidden = !libraryError;
    ui.retryLibrary.disabled = libraryLoading;
    renderEntries();
  }

  async function loadPlaylists(force = false) {
    if (!canUseRemote() || (playlistsLoading && !force)) return;
    const id = ++playlistsRequestId;
    playlistsLoading = true;
    playlistsError = "";
    renderPlaylists();
    try {
      const result = requirePlaylists(await request("/api/playlists"));
      if (id !== playlistsRequestId || !canUseRemote()) return;
      playlists = result;
      ui.playlistEntries.textContent = "";
      for (const playlist of result) {
        ui.playlistEntries.append(makePlaylist(playlist));
      }
    } catch (error) {
      if (id !== playlistsRequestId) return;
      if (error.kind === "unauthorized" || error.status === 403) {
        handleAccessDenied();
        return;
      }
      playlists = null;
      playlistsError = error.message;
    } finally {
      if (id === playlistsRequestId) {
        playlistsLoading = false;
        renderPlaylists();
      }
    }
  }

  async function loadLibrary(path, moveFocus = false) {
    if (!canUseRemote() || (path && !isDlnaPath(path))) return;
    const id = ++libraryRequestId;
    requestedPath = path;
    libraryLoading = true;
    libraryError = "";
    renderLibrary();
    try {
      const url = path ? `/api/library?path=${encodeURIComponent(path)}` : "/api/library";
      const result = requireListing(await request(url), path);
      if (id !== libraryRequestId || !canUseRemote()) return;
      library = result;
      ui.entries.textContent = "";
      const folderLength = result.folder_queue === true
        ? result.entries.filter((entry) => entry.kind === "file").length : 0;
      let folderPosition = 0;
      for (const entry of result.entries) {
        if (folderLength && entry.kind === "file") folderPosition++;
        ui.entries.append(makeEntry(entry, folderPosition, folderLength));
      }
      if (isDlnaPath(path) && !cleanupReadinessChecked) {
        cleanupReadinessChecked = true;
        void refreshOwnerStatus(true);
      }
    } catch (error) {
      if (id !== libraryRequestId) return;
      if (error.kind === "unauthorized" || error.status === 403) {
        handleAccessDenied();
        return;
      }
      libraryError = error.message;
    } finally {
      if (id === libraryRequestId) {
        libraryLoading = false;
        renderLibrary();
        if (moveFocus && !libraryError && canUseRemote()) ui.libraryHeading.focus();
      }
    }
  }

  ui.retryOwner.addEventListener("click", () => {
    if (accessDenied && ownerStatus && ownerStatus.owner_access) {
      accessDenied = false;
      ownerStatusError = "";
      renderOwner();
      void refreshState(true);
      void loadLibrary("");
    } else {
      void refreshOwnerStatus(true);
    }
  });
  ui.cleanupStart.addEventListener("click", () => {
    if (!canCleanup() || cleanupPending) return;
    cleanupConfirmOpen = true;
    cleanupError = "";
    renderOwner();
    ui.cleanupConfirmDelete.focus();
  });
  ui.cleanupConfirmDelete.addEventListener("click", () => { void cleanupLegacy(); });
  ui.cleanupCancel.addEventListener("click", cancelCleanup);
  ui.cleanupConfirm.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancelCleanup();
    }
  });
  ui.retryState.addEventListener("click", () => { void refreshState(true); });
  ui.playPause.addEventListener("click", () => {
    if (!state || !state.current) return;
    if (state.playback === "playing" || state.playback === "paused") {
      void control("/api/toggle-pause", undefined, "change playback", { focus: ui.playPause });
    } else {
      void control("/api/play", { path: state.current.path }, "play file", { focus: ui.playPause });
    }
  });
  ui.previous.addEventListener("click", () => {
    void control("/api/skip", { direction: "previous" }, "skip to previous file", { focus: ui.previous });
  });
  ui.next.addEventListener("click", () => {
    void control("/api/skip", { direction: "next" }, "skip to next file", { focus: ui.next });
  });
  ui.stop.addEventListener("click", () => {
    void stopPlayback();
  });
  ui.volume.addEventListener("input", () => {
    changingVolume = true;
    setRange(ui.volume, Number(ui.volume.value), Number(ui.volume.max));
    ui.volumeValue.textContent = `${ui.volume.value}%`;
  });
  ui.volume.addEventListener("change", () => {
    const volume = Number(ui.volume.value);
    void control("/api/volume", { volume }, "set volume", { focus: ui.volume }).finally(() => {
      changingVolume = false;
      renderState();
    });
  });
  ui.timerForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!ui.timerForm.reportValidity()) return;
    void control("/api/timer", { minutes: Number(ui.timerMinutes.value) }, "set timer", { focus: ui.timerMinutes });
  });
  ui.cancelTimer.addEventListener("click", () => {
    void control("/api/timer", undefined, "cancel timer", { method: "DELETE", focus: ui.cancelTimer });
  });
  ui.folderUp.addEventListener("click", () => {
    if (library && library.parent !== null) void loadLibrary(library.parent, true);
  });
  ui.playFolder.addEventListener("click", () => {
    const first = library && library.folder_queue === true &&
      library.entries.find((entry) => entry.kind === "file");
    if (first) void control("/api/play", { path: first.path }, "play folder", { focus: ui.playFolder });
  });
  ui.shuffleFolder.addEventListener("click", () => {
    if (!library || !library.path.startsWith("dlna/")) return;
    void control(
      "/api/shuffle",
      { path: library.path },
      "shuffle folder",
      { focus: ui.shuffleFolder, pending: "shuffle-folder" },
    );
  });
  ui.retryLibrary.addEventListener("click", () => { void loadLibrary(requestedPath, true); });
  ui.retryPlaylists.addEventListener("click", () => { void loadPlaylists(true); });

  renderOwner();
  void refreshOwnerStatus();
  setInterval(() => {
    if (canUseRemote()) void refreshState();
  }, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (canUseRemote()) void refreshState(true);
    else if (!ownerStatus && !ownerChecking) void refreshOwnerStatus();
  });
})();
