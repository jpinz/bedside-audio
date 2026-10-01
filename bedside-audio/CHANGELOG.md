# Changelog

## Unreleased

## 0.9.0

- Add an optional exact-path library allowlist that preserves only configured
  show folders, their ancestors and their descendants.
- Add case-insensitive title glob exclusions for hiding folders such as
  `Specials` or `Season 0*` and their complete subtrees.
- Keep the hardware bridge reconnecting with a 30-second maximum backoff and
  reset the budget after an authenticated Core WebSocket session.
- Advance Bedside-owned queues after confirmed Voice playback settles to
  `idle`, while keeping unavailable and error states fail-closed.

## 0.8.0

- Add strict color and brightness options for the playing, paused, sleeping,
  and shared physical button-press LED styles.
- Synchronize one compact versioned theme payload to an exact custom-firmware
  `text.*` entity on startup, reconnect, and reported mismatch.
- Keep theme writes idempotent and tightly allowlisted without replaying
  gestures or transport actions from snapshots.
- Add atomic fail-closed firmware parsing, persisted last-valid themes, and a
  dim 10 percent button-press default that no longer inherits LED Ring
  brightness.
- Preserve the existing LED reducer and priority of startup, connectivity,
  jack, dial, timer, Assist, mute, error, and diagnostic states.

## 0.7.0

- Preserve exact Bedside media provenance across ESPHome reconnects and
  filtered `play_media` service events so hardware gestures fail closed after
  foreign or unverifiable transport changes.
- Add validated custom playlists to Home Assistant app options.
- Resolve configured DLNA files and folders into controller-owned queues while
  preserving playlist order and Core browse order.
- Add an explicit one-time shuffle action for the currently browsed show or
  season folder.
- Preserve shuffled and configured queues across Next, Previous, restart,
  reconnect and state refresh.
- Add owner-protected API routes, accessible web controls, responsive layout,
  visible pending and error states, and regression coverage for the feature.
- Publish the App from the official custom-repository layout with pre-built
  `amd64` and `aarch64` GHCR images behind one multi-architecture manifest.

## 0.6.0

- Add always-available player controls and the optional Voice hardware bridge.
