const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");

const staticDir = join(
  __dirname,
  "..",
  "bedside-audio",
  "bedside_audio",
  "static",
);
const app = readFileSync(join(staticDir, "app.js"), "utf8");
const html = readFileSync(join(staticDir, "index.html"), "utf8");
const css = readFileSync(join(staticDir, "style.css"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);

function contrastRatio(first, second) {
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => {
      const channel = Number.parseInt(value, 16) / 255;
      return channel <= 0.03928
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const [lighter, darker] = [luminance(first), luminance(second)]
    .sort((left, right) => right - left);
  return (lighter + 0.05) / (darker + 0.05);
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

class Element {
  constructor(document) {
    this.document = document;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty() {} };
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.isConnected = true;
    this._text = "";
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  querySelectorAll(selector) {
    const expectedClass = selector === "button.entry"
      ? "entry"
      : selector === "button.playlist-entry"
        ? "playlist-entry"
        : null;
    assert.ok(expectedClass, `Unsupported selector ${selector}`);
    return this.children.flatMap((item) =>
      item.children.filter((child) => child.className === expectedClass));
  }

  querySelector(selector) {
    const find = (node) => node.className === selector.slice(1)
      ? node : node.children.map(find).find(Boolean);
    return find(this);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ??= []).push(listener);
  }

  dispatch(type, properties = {}) {
    for (const listener of this.listeners[type] ?? []) {
      listener({ preventDefault() {}, key: "", ...properties });
    }
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  closest() {
    return this.document.safety;
  }

  reportValidity() {
    return true;
  }

  focus() {
    this.document.activeElement = this;
  }
}

async function launch(respond) {
  const document = {
    baseURI: "https://ha.example/ingress/",
    hidden: false,
    listeners: {},
    addEventListener(type, listener) {
      (this.listeners[type] ??= []).push(listener);
    },
    dispatch(type) {
      for (const listener of this.listeners[type] ?? []) listener();
    },
  };
  document.body = new Element(document);
  document.activeElement = document.body;
  document.safety = new Element(document);
  const ui = Object.fromEntries(ids.map((id) => [id, new Element(document)]));
  document.getElementById = (id) => ui[id];
  document.createElement = () => new Element(document);
  const calls = [];
  const ticks = [];

  async function fetch(url, options) {
    const address = new URL(url);
    const call = {
      endpoint: address.pathname.split("/api/")[1],
      path: address.searchParams.get("path") || "",
      method: options.method,
      body: options.body ? JSON.parse(options.body) : {},
      headers: options.headers || {},
    };
    calls.push(call);
    const reply = await respond(call);
    assert.ok(reply, `Unexpected ${call.method} ${call.endpoint} ${call.path}`);
    const status = reply.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => reply.text ?? JSON.stringify(reply.body),
    };
  }

  runInNewContext(app, {
    document,
    window: { location: { protocol: "https:", origin: "https://ha.example" } },
    fetch,
    URL,
    URLSearchParams,
    AbortController,
    setTimeout,
    clearTimeout,
    setInterval(callback) { ticks.push(callback); },
  });

  async function settle() {
    await new Promise((resolve) => setImmediate(resolve));
  }

  async function waitFor(predicate) {
    for (let attempt = 0; attempt < 40; attempt++) {
      await settle();
      if (predicate()) return;
    }
    assert.fail("UI did not reach the expected state");
  }

  return {
    ui,
    calls,
    settle,
    waitFor,
    clickEntry(path) {
      const entry = ui.entries.children.map((item) => item.children[0])
        .find((button) => button.dataset.path === path);
      assert.ok(entry, `Missing library entry ${path}`);
      entry.dispatch("click");
    },
    clickPlaylist(name) {
      const entry = ui["playlist-entries"].children.map((item) => item.children[0])
        .find((button) => button.dataset.name === name);
      assert.ok(entry, `Missing playlist ${name}`);
      entry.dispatch("click");
    },
    showTab() {
      document.dispatch("visibilitychange");
    },
    tick() {
      for (const callback of ticks) callback();
    },
    activeElement() {
      return document.activeElement;
    },
  };
}

const ROOT = {
  path: "",
  parent: null,
  display_path: "Library root",
  entries: [{ path: "dlna", name: "TV library", kind: "folder" }],
};
const TV = {
  path: "dlna",
  parent: "",
  display_path: "TV library",
  entries: [{ path: "dlna/opaque-folder", name: "Evening series", kind: "folder" }],
};
const VIDEOS = [
  { path: "dlna/opaque-one", name: "First video", kind: "file" },
  { path: "dlna/opaque-two", name: "Second video", kind: "file" },
];
const FOLDER = {
  path: "dlna/opaque-folder",
  parent: "dlna",
  display_path: "TV library / Evening series",
  folder_queue: true,
  entries: [{ path: "dlna/opaque-extra", name: "Extras", kind: "folder" }, ...VIDEOS],
};

function fixture({
  ownerAccess = true,
  legacyPresent = false,
  cleanupReady = false,
  readyAfterBrowse = false,
  ownerError = null,
  cleanupError = null,
  root = ROOT,
  stopPending: initialStopPending = false,
  playlists = [],
} = {}) {
  const owner = {
    owner_access: ownerAccess,
    dlna_enabled: true,
    legacy_credentials_present: legacyPresent,
    legacy_cleanup_ready: cleanupReady,
  };
  let ownerChecks = 0;
  let stateError = null;
  let playback = "stopped";
  let stopPending = initialStopPending;
  let index = -1;
  let timer = null;
  let volume = 8;
  let queueKind = "folder";
  let queueName = null;
  let nextOwnerError = null;
  let nextLibraryError = null;
  let nextPlaylistsError = null;

  function state() {
    return {
      stop_pending: stopPending,
      playback,
      current: index < 0 ? null : {
        path: VIDEOS[index].path,
        name: VIDEOS[index].name,
        display_path: `TV library / Evening series / ${VIDEOS[index].name}`,
      },
      queue: index < 0 ? null : {
        index,
        length: VIDEOS.length,
        previous: index > 0,
        next: index < VIDEOS.length - 1,
        kind: queueKind,
        name: queueName,
      },
      timer_remaining: timer,
      volume,
      playlists_configured: playlists.length > 0,
      capabilities: { max_volume: 50 },
      error: null,
    };
  }

  return {
    owner,
    get ownerChecks() { return ownerChecks; },
    failNextState(status = 403) { stateError = status; },
    failNextOwner(status = 503) { nextOwnerError = status; },
    failNextLibrary(path, status = 503) { nextLibraryError = { path, status }; },
    failNextPlaylists(status = 503) { nextPlaylistsError = status; },
    respond({ endpoint, path, method, body }) {
      if (endpoint === "owner/status") {
        ownerChecks++;
        const failure = nextOwnerError || ownerError;
        nextOwnerError = null;
        return failure
          ? { status: failure, body: { detail: "Access check unavailable" } }
          : { body: { ...owner } };
      }
      if (!owner.owner_access) return { status: 403, body: { detail: "Forbidden" } };
      if (endpoint === "state" && method === "GET") {
        if (stateError) {
          const denied = stateError;
          stateError = null;
          return { status: denied, body: { detail: "Forbidden" } };
        }
        return { body: state() };
      }
      if (endpoint === "playlists" && method === "GET") {
        if (nextPlaylistsError) {
          const failure = nextPlaylistsError;
          nextPlaylistsError = null;
          return { status: failure, body: { detail: "Playlist list unavailable" } };
        }
        return { body: { playlists: playlists.map((name) => ({ name })) } };
      }
      if (endpoint === "library" && method === "GET") {
        if (nextLibraryError && nextLibraryError.path === path) {
          const failure = nextLibraryError.status;
          nextLibraryError = null;
          return { status: failure, body: { detail: "TV browse unavailable" } };
        }
        if (path === "") return { body: root };
        if (path === "dlna") {
          if (readyAfterBrowse) owner.legacy_cleanup_ready = true;
          return { body: TV };
        }
        if (path === FOLDER.path) return { body: FOLDER };
      }
      if (endpoint === "owner/cleanup-legacy" && method === "POST") {
        if (cleanupError) return { status: cleanupError, body: { detail: "Cleanup unavailable" } };
        owner.legacy_credentials_present = false;
        owner.legacy_cleanup_ready = false;
        return { body: { deleted_files: 2, plex_revocation: "not_attempted" } };
      }
      if (endpoint === "play" && method === "POST") {
        index = VIDEOS.findIndex((video) => video.path === body.path);
        if (index < 0) return { status: 404, body: { detail: "Video unavailable" } };
        playback = "playing";
        queueKind = "folder";
        queueName = null;
      } else if (endpoint === "playlists/play" && method === "POST") {
        if (!playlists.includes(body.name)) {
          return { status: 404, body: { detail: "Configured playlist not found" } };
        }
        index = 0;
        playback = "playing";
        queueKind = "playlist";
        queueName = body.name;
      } else if (endpoint === "shuffle" && method === "POST") {
        if (body.path !== FOLDER.path) {
          return { status: 404, body: { detail: "Folder unavailable" } };
        }
        index = 1;
        playback = "playing";
        queueKind = "shuffle";
        queueName = "TV library / Evening series";
      } else if (endpoint === "skip" && method === "POST") {
        index += body.direction === "next" ? 1 : -1;
        playback = "playing";
      } else if (endpoint === "toggle-pause" && method === "POST") {
        playback = playback === "playing" ? "paused" : "playing";
      } else if (endpoint === "timer" && method === "POST") timer = body.minutes * 60;
      else if (endpoint === "timer" && method === "DELETE") timer = null;
      else if (endpoint === "stop" && method === "POST") {
        stopPending = false;
        playback = "stopped";
      }
      else if (endpoint === "volume" && method === "POST") volume = body.volume;
      else return { status: 404, body: { detail: "Unknown endpoint" } };
      return { body: state() };
    },
  };
}

test("owner status opens the TV library without any Plex account UI or API", async () => {
  const server = fixture();
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.calls.length > 0);
  assert.equal(browser.calls[0].endpoint, "owner/status");
  await browser.waitFor(() => browser.ui.entries.children.length === 1);

  assert.equal(browser.ui.remote.hidden, false);
  assert.match(html, /<h2 id="library-heading"[^>]*>TV library<\/h2>/);
  assert.equal(browser.ui.entries.children[0].children[0].dataset.path, "dlna");
  assert.equal(browser.ui.entries.children[0].children[0].dataset.name, "TV library");
  assert.equal(browser.ui["plex-connect"], undefined);
  assert.equal(browser.ui["plex-migration"], undefined);
  assert.ok(browser.calls.every((call) => !call.endpoint.startsWith("plex/")));
  assert.doesNotMatch(app, /\/api\/plex\//);
});

test("an install with no configured playlists keeps the playlist section hidden", async () => {
  const browser = await launch(fixture().respond);
  await browser.waitFor(() =>
    browser.calls.some((call) => call.endpoint === "playlists"));
  await browser.settle();

  assert.equal(browser.ui.playlists.hidden, true);
  assert.equal(browser.ui["playlist-entries"].children.length, 0);
  assert.equal(browser.ui["playlists-error"].hidden, true);
});

test("configured playlists show loading, keyboard-safe buttons, and queue identity", async () => {
  const server = fixture({ playlists: ["Quiet evening"] });
  const pending = deferred();
  const browser = await launch((call) => {
    if (call.endpoint === "playlists/play") {
      return pending.promise.then(() => server.respond(call));
    }
    return server.respond(call);
  });
  await browser.waitFor(() => browser.ui["playlist-entries"].children.length === 1);
  const button = browser.ui["playlist-entries"].children[0].children[0];
  assert.equal(browser.ui.playlists.hidden, false);
  assert.equal(button.dataset.name, "Quiet evening");
  assert.equal(button.getAttribute("aria-label"), "Play configured playlist Quiet evening");
  assert.match(html, /<button id="retry-playlists" type="button"/);

  button.focus();
  browser.clickPlaylist("Quiet evening");
  await browser.waitFor(() =>
    browser.calls.some((call) => call.endpoint === "playlists/play"));
  assert.equal(button.disabled, true);
  assert.equal(button.children[1].textContent, "Starting…");
  pending.resolve();
  await browser.waitFor(() =>
    browser.ui["queue-status"].textContent === "Playlist: Quiet evening. Item 1 of 2");
  assert.equal(browser.ui["queue-status"].hidden, false);
  assert.equal(browser.activeElement(), button);
  const request = browser.calls.find((call) => call.endpoint === "playlists/play");
  assert.deepEqual(request.body, { name: "Quiet evening" });
  assert.equal(request.headers["X-Bedside-Control"], "1");
});

test("configured playlist loading is visible without changing empty installs", async () => {
  const server = fixture({ playlists: ["Quiet evening"] });
  const pending = deferred();
  const browser = await launch((call) => {
    if (call.endpoint === "playlists") {
      return pending.promise.then(() => server.respond(call));
    }
    return server.respond(call);
  });
  await browser.waitFor(() =>
    browser.ui["playlists-message"].textContent === "Loading configured playlists…");
  assert.equal(browser.ui.playlists.hidden, false);
  assert.equal(browser.ui["playlist-entries"].children.length, 0);

  pending.resolve();
  await browser.waitFor(() => browser.ui["playlist-entries"].children.length === 1);
  assert.equal(browser.ui["playlists-message"].hidden, true);
});

test("playlist list errors are visible and Retry restores configured playlists", async () => {
  const server = fixture({ playlists: ["Quiet evening"] });
  server.failNextPlaylists();
  const browser = await launch(server.respond);
  await browser.waitFor(() =>
    browser.ui["playlists-error"].textContent.includes("Playlist list unavailable"));
  assert.equal(browser.ui.playlists.hidden, false);
  assert.equal(browser.ui["retry-playlists"].hidden, false);

  browser.ui["retry-playlists"].dispatch("click");
  await browser.waitFor(() => browser.ui["playlist-entries"].children.length === 1);
  assert.equal(browser.ui["playlists-error"].hidden, true);
  assert.equal(browser.ui["retry-playlists"].hidden, true);
});

for (const options of [
  { ownerAccess: false, legacyPresent: true, cleanupReady: true },
  { ownerError: 403, legacyPresent: true, cleanupReady: true },
]) {
  test(`denied owner status ${JSON.stringify(options)} hides all protected controls`, async () => {
    const server = fixture(options);
    const browser = await launch(server.respond);
    await browser.waitFor(() => browser.calls.length > 0);
    assert.equal(browser.calls[0].endpoint, "owner/status");
    await browser.waitFor(() => server.ownerChecks === 1);
    await browser.settle();

    assert.equal(browser.ui.remote.hidden, true);
    assert.equal(browser.ui["legacy-cleanup"].hidden, true);
    assert.equal(browser.calls.filter((call) => call.endpoint === "state").length, 0);
    assert.equal(browser.calls.filter((call) => call.endpoint === "library").length, 0);
    assert.equal(browser.ui["retry-owner"].hidden, false);
  });
}

test("a safe owner status error hides controls and allows Retry", async () => {
  const server = fixture();
  let first = true;
  const browser = await launch((call) => {
    if (call.endpoint === "owner/status" && first) {
      first = false;
      return { body: { ...server.owner, error: "Owner file unavailable" } };
    }
    return server.respond(call);
  });
  await browser.waitFor(() => browser.ui["owner-error"].textContent.includes("Owner file unavailable"));
  assert.equal(browser.ui.remote.hidden, true);
  assert.equal(browser.ui["legacy-cleanup"].hidden, true);
  assert.equal(browser.ui["retry-owner"].hidden, false);
  assert.equal(browser.calls.filter((call) => call.endpoint === "state").length, 0);
  browser.ui["retry-owner"].dispatch("click");
  await browser.waitFor(() => browser.ui.remote.hidden === false &&
    browser.ui.entries.children.length === 1);
});

for (const options of [
  { legacyPresent: false, cleanupReady: true },
  { legacyPresent: true, cleanupReady: false },
]) {
  test(`cleanup is hidden unless both legacy flags are true: ${JSON.stringify(options)}`, async () => {
    const server = fixture(options);
    const browser = await launch(server.respond);
    await browser.waitFor(() => browser.ui.entries.children.length === 1);

    assert.equal(browser.ui["legacy-cleanup"].hidden, true);
    assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
  });
}

test("one successful DLNA browse refreshes owner readiness once without starting cleanup", async () => {
  const server = fixture({ legacyPresent: true, readyAfterBrowse: true });
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  assert.equal(server.ownerChecks, 1);
  assert.equal(browser.ui["legacy-cleanup"].hidden, true);

  browser.clickEntry("dlna");
  await browser.waitFor(() => server.ownerChecks === 2 &&
    browser.ui.entries.children[0]?.children[0]?.dataset.path === "dlna/opaque-folder");
  await browser.settle();
  assert.equal(browser.ui["legacy-cleanup"].hidden, false);
  assert.equal(browser.ui["cleanup-confirm"].hidden, true);
  browser.clickEntry("dlna/opaque-folder");
  await browser.waitFor(() => browser.ui.entries.children.length === 3);
  assert.equal(server.ownerChecks, 2, "nested browse must not poll owner status again");
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
});

test("TV folder queue keeps always-on playback, manual transport, volume cap, timer and Stop", async () => {
  const browser = await launch(fixture().respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.clickEntry("dlna");
  await browser.waitFor(() => browser.ui.entries.children[0]?.children[0]?.dataset.path === FOLDER.path);
  browser.clickEntry(FOLDER.path);
  await browser.waitFor(() => browser.ui.entries.children.length === 3);
  assert.equal(browser.ui["library-path"].textContent, "TV library / Evening series");

  const extra = browser.ui.entries.children[0].children[0];
  const first = browser.ui.entries.children[1].children[0];
  const second = browser.ui.entries.children[2].children[0];
  assert.equal(extra.disabled, false);
  assert.equal(first.disabled, false);
  assert.equal(second.disabled, false);
  assert.equal(first.children[0].children[1].textContent, "Video 1 of 2");
  assert.equal(second.children[0].children[1].textContent, "Video 2 of 2");
  assert.match(second.getAttribute("aria-label"), /video 2 of 2 in folder/);
  assert.equal(browser.ui["play-folder"].hidden, false);
  assert.equal(browser.ui["play-folder"].disabled, false);
  assert.equal(browser.ui["shuffle-folder"].hidden, false);
  assert.equal(browser.ui["shuffle-folder"].disabled, false);

  browser.ui["play-folder"].dispatch("click");
  await browser.waitFor(() => browser.ui["current-name"].textContent === "First video" &&
    browser.ui.next.disabled === false);
  assert.equal(browser.ui["current-path"].textContent, "TV library / Evening series / First video");
  assert.equal(browser.ui.previous.disabled, true);
  browser.tick();
  await browser.settle();
  assert.equal(browser.ui["current-name"].textContent, "First video");
  assert.equal(browser.calls.filter((call) => call.endpoint === "skip").length, 0);

  browser.ui.next.dispatch("click");
  await browser.waitFor(() => browser.ui["current-name"].textContent === "Second video" &&
    browser.ui.previous.disabled === false);
  assert.equal(browser.ui.next.disabled, true);
  browser.ui.previous.dispatch("click");
  await browser.waitFor(() => browser.ui["current-name"].textContent === "First video" &&
    browser.ui.previous.disabled === true);
  browser.ui["play-pause"].dispatch("click");
  await browser.waitFor(() => browser.ui["playback-status"].textContent === "Paused");
  browser.ui["play-pause"].dispatch("click");
  await browser.waitFor(() => browser.ui["playback-status"].textContent === "Playing");

  assert.equal(browser.ui.volume.max, "50");
  assert.match(html, /id="volume"[^>]*max="50"/);
  assert.equal(browser.ui["volume-value"].textContent, "8%");
  browser.ui.volume.value = "10";
  browser.ui.volume.dispatch("input");
  browser.ui.volume.dispatch("change");
  await browser.waitFor(() => browser.ui["volume-value"].textContent === "10%" &&
    browser.calls.some((call) => call.endpoint === "volume" && call.body.volume === 10));
  browser.ui["timer-minutes"].value = "5";
  browser.ui["timer-form"].dispatch("submit");
  await browser.waitFor(() => browser.ui["timer-status"].textContent === "Stops playback in 5:00");
  browser.ui["cancel-timer"].dispatch("click");
  await browser.waitFor(() => browser.ui["timer-status"].textContent === "No timer set");
  browser.ui.stop.dispatch("click");
  await browser.waitFor(() => browser.ui["playback-status"].textContent === "Stopped");

  const actions = browser.calls.filter((call) => call.method !== "GET");
  assert.deepEqual(actions.map((call) => `${call.method} ${call.endpoint}`), [
    "POST play", "POST skip", "POST skip",
    "POST toggle-pause", "POST toggle-pause", "POST volume",
    "POST timer", "DELETE timer", "POST stop",
  ]);
  assert.equal(actions.find((call) => call.endpoint === "play").body.path, VIDEOS[0].path);
  assert.ok(actions.every((call) => call.headers["X-Bedside-Control"] === "1"));
});

test("explicit folder shuffle starts once and Next does not reshuffle", async () => {
  const server = fixture();
  const pending = deferred();
  const browser = await launch((call) => {
    if (call.endpoint === "shuffle") {
      return pending.promise.then(() => server.respond(call));
    }
    return server.respond(call);
  });
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.clickEntry("dlna");
  await browser.waitFor(() =>
    browser.ui.entries.children[0]?.children[0]?.dataset.path === FOLDER.path);
  browser.clickEntry(FOLDER.path);
  await browser.waitFor(() => browser.ui.entries.children.length === 3);

  browser.ui["shuffle-folder"].focus();
  browser.ui["shuffle-folder"].dispatch("click");
  await browser.waitFor(() =>
    browser.calls.filter((call) => call.endpoint === "shuffle").length === 1);
  assert.equal(browser.ui["shuffle-folder"].disabled, true);
  assert.equal(browser.ui["shuffle-folder"].textContent, "Shuffling folder…");
  pending.resolve();
  await browser.waitFor(() =>
    browser.ui["queue-status"].textContent === "Shuffled folder. Item 2 of 2");
  const shuffle = browser.calls.find((call) => call.endpoint === "shuffle");
  assert.deepEqual(shuffle.body, { path: FOLDER.path });
  assert.equal(shuffle.headers["X-Bedside-Control"], "1");
  assert.equal(browser.activeElement(), browser.ui["shuffle-folder"]);

  browser.ui.previous.dispatch("click");
  await browser.waitFor(() =>
    browser.ui["queue-status"].textContent === "Shuffled folder. Item 1 of 2");
  assert.equal(browser.calls.filter((call) => call.endpoint === "shuffle").length, 1);
  assert.equal(browser.calls.filter((call) => call.endpoint === "skip").length, 1);
});

test("state refresh clears stale disabled state from folders and playable rows", async () => {
  const browser = await launch(fixture({ playlists: ["Quiet evening"] }).respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.clickEntry("dlna");
  await browser.waitFor(() => browser.ui.entries.children[0]?.children[0]?.dataset.path === FOLDER.path);
  browser.clickEntry(FOLDER.path);
  await browser.waitFor(() => browser.ui.entries.children.length === 3);
  const buttons = browser.ui.entries.children.map((item) => item.children[0]);
  const playlistButton = browser.ui["playlist-entries"].children[0].children[0];

  for (const button of buttons) button.disabled = true;
  playlistButton.disabled = true;
  browser.ui["shuffle-folder"].disabled = true;
  const stateCalls = browser.calls.filter((call) => call.endpoint === "state").length;
  browser.showTab();
  await browser.waitFor(() =>
    browser.calls.filter((call) => call.endpoint === "state").length > stateCalls);
  await browser.settle();

  assert.deepEqual(buttons.map((button) => button.disabled), [false, false, false]);
  assert.equal(playlistButton.disabled, false);
  assert.equal(browser.ui["shuffle-folder"].disabled, false);
});

test("new controls retain the existing mobile tap-target and reflow rules", () => {
  assert.match(html, /<button id="shuffle-folder" type="button"/);
  assert.match(app, /button\.className = "playlist-entry"/);
  assert.match(css, /button \{[\s\S]*min-height: 48px;/);
  assert.match(css, /\.playlist-entry \{[\s\S]*min-height: 56px;/);
  assert.match(css, /\.folder-actions \{[\s\S]*display: grid;[\s\S]*gap: 8px;/);
  assert.match(css, /@media \(min-width: 520px\)[\s\S]*\.folder-actions \{[\s\S]*grid-template-columns: repeat\(2/);
  assert.match(css, /\.folder-actions button \{[\s\S]*width: 100%;/);
});

test("control boundaries and range thumbs use the accessible bedside palette", () => {
  const colors = Object.fromEntries(
    [...css.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6});/gi)]
      .map((match) => [match[1], match[2]]),
  );
  assert.ok(contrastRatio(colors.border, colors.panel) >= 3);
  assert.ok(contrastRatio(colors.border, colors.control) >= 3);
  assert.match(
    css,
    /input\[type="range"\] \{[\s\S]*-webkit-appearance: none;[\s\S]*appearance: none;/,
  );
  assert.match(
    css,
    /::-webkit-slider-thumb \{[\s\S]*background: var\(--accent\);/,
  );
  assert.match(
    css,
    /::-moz-range-thumb \{[\s\S]*background: var\(--accent\);/,
  );
});

test("explicit Stop retries an unconfirmed stop while browsing stays available", async () => {
  const browser = await launch(fixture({ stopPending: true }).respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  assert.equal(browser.ui.entries.children[0].children[0].disabled, false);
  assert.equal(browser.ui.stop.textContent, "Retry stop");
  assert.equal(browser.ui.stop.disabled, false);

  browser.ui.stop.dispatch("click");
  await browser.waitFor(() => browser.ui.stop.textContent === "Stop");
  assert.equal(browser.calls.filter((call) => call.endpoint === "stop").length, 1);
});

for (const responseOrder of ["stop-first", "ordinary-first"]) {
  test(`urgent Stop wins when play response stalls and resolves ${responseOrder}`, async () => {
    const server = fixture();
    const playResponse = deferred();
    const stopResponse = deferred();
    const browser = await launch((call) => {
      const reply = server.respond(call);
      if (call.endpoint === "play") {
        return playResponse.promise.then(() => reply);
      }
      if (call.endpoint === "stop") {
        return stopResponse.promise.then(() => reply);
      }
      return reply;
    });
    await browser.waitFor(() => browser.ui.entries.children.length === 1);
    browser.clickEntry("dlna");
    await browser.waitFor(() =>
      browser.ui.entries.children[0]?.children[0]?.dataset.path === FOLDER.path);
    browser.clickEntry(FOLDER.path);
    await browser.waitFor(() => browser.ui.entries.children.length === 3);

    browser.ui["play-folder"].dispatch("click");
    await browser.waitFor(() =>
      browser.calls.filter((call) => call.endpoint === "play").length === 1);
    assert.equal(browser.ui.stop.disabled, false);

    browser.ui.stop.dispatch("click");
    browser.ui.stop.dispatch("click");
    await browser.waitFor(() =>
      browser.calls.filter((call) => call.endpoint === "stop").length === 1);
    assert.equal(browser.ui.stop.textContent, "Stopping…");
    assert.equal(browser.ui.stop.disabled, true);
    assert.equal(browser.calls.find((call) => call.endpoint === "stop")
      .headers["X-Bedside-Control"], "1");

    if (responseOrder === "ordinary-first") {
      playResponse.resolve();
      await browser.settle();
      assert.notEqual(browser.ui["playback-status"].textContent, "Playing");
      stopResponse.resolve();
    } else {
      stopResponse.resolve();
      await browser.waitFor(() => browser.ui["playback-status"].textContent === "Stopped");
      playResponse.resolve();
    }
    await browser.waitFor(() => browser.ui["playback-status"].textContent === "Stopped");
    await browser.settle();

    assert.equal(browser.ui["playback-status"].textContent, "Stopped");
    assert.equal(browser.ui.stop.textContent, "Stop");
    assert.equal(browser.ui["action-error"].hidden, true);
    assert.equal(browser.calls.filter((call) => call.endpoint === "stop").length, 1);
  });
}

test("cleanup requires a second click and Cancel or Escape makes no request", async () => {
  const browser = await launch(fixture({ legacyPresent: true, cleanupReady: true }).respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  assert.equal(browser.ui["legacy-cleanup"].hidden, false);

  browser.ui["cleanup-start"].dispatch("click");
  assert.equal(browser.ui["cleanup-confirm"].hidden, false);
  assert.equal(browser.ui["cleanup-start"].getAttribute("aria-expanded"), "true");
  assert.equal(browser.activeElement(), browser.ui["cleanup-confirm-delete"]);
  assert.match(html, /Deletes only the local Plex credential files/);
  assert.match(html, /Plex access on servers is not revoked, and no app-data backup is made/);
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
  browser.ui["cleanup-cancel"].dispatch("click");
  assert.equal(browser.ui["cleanup-confirm"].hidden, true);
  assert.equal(browser.activeElement(), browser.ui["cleanup-start"]);

  browser.ui["cleanup-start"].dispatch("click");
  browser.ui["cleanup-confirm"].dispatch("keydown", { key: "Escape" });
  assert.equal(browser.ui["cleanup-confirm"].hidden, true);
  assert.equal(browser.ui["cleanup-start"].getAttribute("aria-expanded"), "false");
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
});

test("confirmed cleanup sends only the exact local-deletion request and hides the control", async () => {
  const server = fixture({ legacyPresent: true, cleanupReady: true });
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.ui["cleanup-start"].dispatch("click");
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
  browser.ui["cleanup-confirm-delete"].dispatch("click");
  await browser.waitFor(() => browser.ui["legacy-cleanup"].hidden &&
    browser.ui["cleanup-notice"].textContent.includes("Local Plex credentials deleted"));

  const requests = browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, "POST");
  assert.deepEqual(requests[0].body, { confirm: "delete-local-plex-credentials" });
  assert.equal(requests[0].headers["X-Bedside-Control"], "1");
  assert.equal(browser.ui["cleanup-error"].hidden, true);
  assert.equal(browser.activeElement(), browser.ui["owner-heading"]);
  assert.equal(server.owner.legacy_credentials_present, false);
  assert.ok(browser.calls.every((call) => !call.endpoint.startsWith("plex/")));
});

test("failed cleanup shows an error and requires a new confirmation before retrying", async () => {
  const browser = await launch(fixture({
    legacyPresent: true, cleanupReady: true, cleanupError: 503,
  }).respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.ui["cleanup-start"].dispatch("click");
  browser.ui["cleanup-confirm-delete"].dispatch("click");
  await browser.waitFor(() => browser.ui["cleanup-error"].textContent.includes("Cleanup unavailable"));

  assert.equal(browser.ui["cleanup-error"].hidden, false);
  assert.equal(browser.ui["legacy-cleanup"].hidden, false);
  assert.equal(browser.ui["cleanup-confirm"].hidden, true);
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 1);
  browser.tick();
  await browser.settle();
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 1);
  browser.ui["cleanup-start"].dispatch("click");
  assert.equal(browser.ui["cleanup-confirm"].hidden, false);
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 1);
});

test("revoked owner access clears library and closes pending cleanup confirmation", async () => {
  const server = fixture({ legacyPresent: true, cleanupReady: true });
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.ui["cleanup-start"].dispatch("click");
  assert.equal(browser.ui["cleanup-confirm"].hidden, false);

  server.owner.owner_access = false;
  browser.showTab();
  await browser.waitFor(() => server.ownerChecks === 2 && browser.ui.remote.hidden);
  await browser.settle();
  assert.equal(browser.ui.entries.children.length, 0);
  assert.equal(browser.ui["legacy-cleanup"].hidden, true);
  assert.equal(browser.ui["cleanup-confirm"].hidden, true);
  assert.equal(browser.ui["retry-owner"].hidden, false);
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 0);
});

test("owner status failure after TV browse hides playback and cleanup until Retry", async () => {
  const server = fixture({ legacyPresent: true, readyAfterBrowse: true });
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  server.failNextOwner();
  browser.clickEntry("dlna");
  await browser.waitFor(() => server.ownerChecks === 2 && browser.ui["owner-error"].hidden === false);

  assert.equal(browser.ui.remote.hidden, true);
  assert.equal(browser.ui["legacy-cleanup"].hidden, true);
  assert.equal(browser.ui.entries.children.length, 0);
  assert.equal(browser.ui["retry-owner"].hidden, false);
  browser.ui["retry-owner"].dispatch("click");
  await browser.waitFor(() => server.ownerChecks === 3 &&
    browser.ui.remote.hidden === false && browser.ui.entries.children.length === 1);
});

test("empty TV library and failed browse have distinct empty, error, and Retry states", async () => {
  const emptyBrowser = await launch(fixture({
    root: { path: "", parent: null, entries: [] },
  }).respond);
  await emptyBrowser.waitFor(() =>
    emptyBrowser.ui["library-message"].textContent === "No TV library is available yet.");
  assert.equal(emptyBrowser.ui["library-error"].hidden, true);
  assert.equal(emptyBrowser.ui["retry-library"].hidden, true);

  const server = fixture();
  server.failNextLibrary("");
  const browser = await launch(server.respond);
  await browser.waitFor(() => browser.ui["library-error"].textContent.includes("TV browse unavailable"));
  assert.equal(browser.ui["library-error"].hidden, false);
  assert.equal(browser.ui["retry-library"].hidden, false);
  browser.ui["retry-library"].dispatch("click");
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  assert.equal(browser.ui["library-error"].hidden, true);
});

test("server rejection of cleanup hides protected controls without retrying deletion", async () => {
  const browser = await launch(fixture({
    legacyPresent: true, cleanupReady: true, cleanupError: 403,
  }).respond);
  await browser.waitFor(() => browser.ui.entries.children.length === 1);
  browser.ui["cleanup-start"].dispatch("click");
  browser.ui["cleanup-confirm-delete"].dispatch("click");
  await browser.waitFor(() => browser.ui.remote.hidden &&
    browser.ui["legacy-cleanup"].hidden);
  assert.equal(browser.calls.filter((call) => call.endpoint === "owner/cleanup-legacy").length, 1);
  assert.equal(browser.ui.entries.children.length, 0);
});
