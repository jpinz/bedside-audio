# Bedside audio

Bedside audio is a Home Assistant App for listening to TV audio on wired
headphones plugged into Home Assistant Voice Preview Edition. A signed-in
Home Assistant page on a phone or Kindle is the remote, not the audio output.
The app uses Home Assistant's already-configured DLNA Digital Media Server
integration. It does not sign in to Plex, browse the Plex account, or use a
Plex token for playback.

This is experimental. Short native Home Assistant playback and read-only
Bedside browsing have been verified, but **Bedside-controlled playback, its
timer and a whole episode have not been tested on the hardware.** Installing
or updating this app does not install the optional Voice PE firmware.

## Playback and controls

1. Ingress supplies the signed-in Home Assistant user ID. Bedside admits
   controls only for its privately saved owner. On an upgrade, it migrates
   the HA user ID from the old owner record without using the Plex token.
   A fresh install requires an explicit `dlna_owner_user_id`; the first
   visitor never becomes owner automatically.
2. Bedside calls Core's response-only `media_player.browse_media` through
   the Supervisor Core API, targeting one configured video-capable media
   player. It never plays or changes that browse player. The browser sees
   folder names and opaque, in-memory handles, not raw DLNA URLs, media IDs
   or credentials. Before playback, Bedside re-browses the parent folder
   and checks the exact configured DLNA source, item, MIME type and order.
3. Bedside calls `media_player.play_media` for the native Voice media player
   with the verified `media-source://dlna_dms/...` ID. Core's ESPHome FFmpeg
   proxy removes video (`-vn`) before sending audio to Voice. Bedside never
   opens the HA host audio device or an app-owned media port.

Open **TV library** to follow the DLNA Video / TV Shows folders, then a show,
season and episode. A folder's **Play first video in folder** button starts
its first playable item. Next and Previous move through that folder in
the order Core returned, including duplicate items. After Voice has reported
active playback, a sustained `idle` advances to the next queue item. Voice
does not distinguish a completed episode from an interruption, so an
interruption that settles to `idle` can also advance. The authenticated owner
can browse folders and start a video whenever Voice and Core are available. A
1-720 minute sleep timer is optional. Stop sends `media_stop` and waits briefly
for Voice to report `idle`; a failed stop blocks another play until Retry stop
succeeds. The timer runs in the app when the browser is closed, but is canceled
by an app restart.

Configured playlists appear above the TV library only when at least one valid
playlist is present in the App options. Starting one creates a Bedside-owned
queue. Each configured file contributes one queue item. Each configured
folder expands recursively in the exact order returned by Core, with folders
visited where they occur in that order. Playlist lines remain in their
configured order, and repeated lines intentionally repeat those videos.
Missing, ambiguous, incomplete or unplayable entries stop the playlist with a
visible error. Bedside never skips a bad entry and never falls back to an
arbitrary URL or filesystem path.

While browsing inside a show or season folder, **Shuffle this folder**
recursively resolves its playable videos, applies one fresh standard-library
shuffle for that explicit action, and starts the first shuffled item. Next,
Previous and restart keep that queue and index. Reloading, reconnecting and
polling do not reshuffle it. Browsing or selecting a folder never starts
audio. Confirmed playback followed by a sustained `idle` advances to the next
item automatically.

Voice PE does not report a reliable TV episode position, duration or seek
capability. Bedside offers no seek or saved-resume action. While Bedside owns
the active transport, it keeps a local monotonic position tied to that
Bedside item generation and its own pause/resume commands. The estimate is
used only for the hardware triple-press boundary; any contradictory player
state makes it unavailable and fails closed to Previous. Bedside does not
start audio on service startup and does not replay on an Assist interruption.
Stock Voice firmware may duck playback for Assist, but continuity after a
real Voice command still needs a separately approved hardware test.
Core owns the resolved DLNA/FFmpeg URL: Bedside can request a stop but
**cannot revoke that URL or Plex's DLNA resource**. An `idle` state does
not prove physical silence. A manual Stop/End can also stop audio from
another controller using the same Voice entity.

## Install and configure

Add `https://github.com/jpinz/bedside-audio` as a custom App repository in
Home Assistant, refresh the App store, and install **Bedside audio (Voice)**.
Review every option before starting it. A repository installation is separate
from an app previously installed from `/addons`; see the
[local-app migration guide](../docs/migrating-local-app.md) before changing an
existing installation. The remote uses versioned static assets and no-store
responses to avoid running stale JavaScript through Ingress.

| Option | Purpose |
| --- | --- |
| `media_player_entity` | Native ESPHome media-player entity for Voice PE and its headphones, not a Music Assistant entity. |
| `dlna_source_id` | Exact HA `dlna_dms` source ID, for example `plex_media_server_example`; never a URL or browser-provided ID. |
| `dlna_browse_player_entity_id` | Exact video-capable HA media player used for read-only browsing, for example `media_player.example_tv`; it must differ from the Voice player. |
| `dlna_owner_user_id` | Trusted HA Ingress user ID, required on a fresh install. On an upgrade it may be blank only when a valid legacy owner record supplies the same HA user ID. |
| `library_exclude_patterns` | Optional case-insensitive glob patterns matched against each folder and video title. Matching folders and their descendants are hidden. |
| `library_folders` | Optional allowlist of exact DLNA folder title paths. The default empty list exposes the full video library. |
| `max_volume` | Voice cap from 1 to 50 percent; default is 50. Startup and saved volume remain at their existing value (15 on a fresh install) unless that value exceeds the configured cap. Do not raise Voice volume for a test. |
| `playlists` | Optional list of configured playlists. Each record has a unique `name` and a multiline `items` string as documented below. The default empty list preserves the previous interface and behavior. |
| `voice_button_event_entity` | Optional exact Voice PE `event.*` entity. Leave blank unless the complete hardware bridge is configured. |
| `voice_assist_satellite_entity` | Optional exact Voice PE `assist_satellite.*` entity used to defer controls and preserve local Assist, mute and error display priority. |
| `voice_led_light_entity` | Optional exact stock uniform `light.*` LED entity. Configure this or the custom select, never both. |
| `voice_led_select_entity` | Optional exact custom-firmware `select.*` LED entity with the fixed options `off`, `playing`, `paused` and `sleeping`. |
| `voice_volume_cap_number_entity` | Optional exact custom-firmware `number.*` entity for `Bedside volume cap`; range `0.0` to `0.50`, step `0.05`, initial `0.50`, restore disabled. |

### Custom playlist options

Supervisor app schemas support nested arrays and dictionaries only to a depth
of two. Each `library_folders` record therefore stores one exact folder path
as a JSON array of titles:

```yaml
library_folders:
  - path: '["Video","TV Shows","<show folder>"]'
  - path: '["Video","TV Shows","<another show folder>"]'
```

When the list is non-empty, browsing preserves the source hierarchy but shows
only the ancestors needed to reach an allowed folder, the allowed folder, and
everything below it. Sibling libraries, movies and shows remain hidden.
Configured playlists and shuffle actions use the same allowlist. Missing
configured paths are simply absent; duplicate matching folder titles or a path
that resolves to a playable file fail browsing closed with a visible error. An
installation may configure at most 32 unique folder paths, each with at most 16
segments.

Exclusions apply after the allowlist and always win. Patterns match one folder
or video title, not a full path, without regard to capitalization. `*` matches
any text, `?` matches one character, and bracket expressions such as `[0-2]`
match one listed character:

```yaml
library_exclude_patterns:
  - pattern: "Specials"
  - pattern: "Season 0*"
```

The example hides a folder titled `Specials`, any title beginning with
`Season 0`, and every item below matching folders. An installation may
configure at most 32 unique patterns of at most 256 safe characters.

Bedside keeps each playlist's ordered item paths in one multiline string. Each
non-empty line must be a JSON array of exact DLNA titles, starting below the
configured DLNA source root:

```yaml
playlists:
  - name: "Quiet evening"
    items: |-
      ["TV Shows", "<show folder>", "<season folder>"]
      ["TV Shows", "<another show folder>", "<season folder>", "<episode file>"]
  - name: "One episode"
    items: |-
      ["TV Shows", "<show folder>", "<episode file>"]
```

Replace every placeholder in library folders and playlists with the exact title
shown while browsing the same configured DLNA source in Home Assistant. Do not
include `DLNA`, `TV library`, the source ID, a `media-source://` value, a URL or
a filesystem path. JSON arrays keep titles containing `/` unambiguous.

Playlist names are 1 to 64 safe characters and must be unique without relying
on capitalization differences. An installation may configure at most 16
playlists, with at most 64 item lines per playlist. Each title path may contain
at most 16 segments. A resolved Bedside queue may contain at most 256 playable
videos and may descend through at most 16 folder levels. Empty item blocks,
blank lines, non-string path segments, unsafe names, extra record fields and
duplicate playlist names prevent startup with a clear option error.

At playback time Bedside re-browses every configured segment through the
configured browse player and requires one exact match at each level. A file
adds one item. A folder adds every playable video below it in Core's returned
order. Later playlist lines follow after that expansion. Repeating a file or
folder line repeats its queue items. If a title was renamed, removed,
duplicated, truncated by Core or became unplayable, playback fails visibly
without starting a partial queue.

The five hardware options are disabled by default. Existing v0.5 installs can
update without activating hardware controls. The bridge starts only when the
button and Assist entities plus exactly one LED output are valid; missing,
partial or malformed settings leave the Ingress remote available and report a
disabled bridge status in `/api/state`. The volume-cap number is optional; a
malformed configured number entity disables the bridge, while a blank value
preserves stock-firmware compatibility.

When enabled, the App authenticates to
`ws://supervisor/core/websocket` with `SUPERVISOR_TOKEN`, subscribes to state
triggers for only the configured Voice media player, button, Assist and LED
entities, and adds one event trigger filtered to Core
`call_service`/`media_player.play_media` events. It resolves the token's HA
user through `auth/current_user`, then locally accepts only calls targeting
the exact configured Voice entity. Snapshots after startup or reconnect may
restore availability, volume and LED display, but never replay button events,
service calls or transport actions. Live event tuples are deduplicated.
Connection failures retry indefinitely with exponential backoff capped at 30
seconds. A successfully authenticated Core session resets the backoff budget,
so separate outages do not accumulate toward a permanent disconnect.

The validated custom firmware event is named `Button press` and may report
`single_press`, `double_press`, `triple_press`, `long_press` or
`easter_egg_press`; Bedside intentionally acts only on the first three.
Hardware button behavior is fixed: single press toggles Bedside pause/play,
double press manually selects Next, and triple press restarts the current
Bedside episode only after 10 seconds. At or before 10 seconds, or when the
Bedside-owned position is unavailable, triple press selects Previous. Gestures
are ignored while Assist is not idle, Voice is unavailable, stop/error
recovery is active, or Bedside does not own the active transport. Hardware
snapshots never replay gestures or directly advance the queue. Controller
polling advances only after Bedside-owned playback was observed active and
then remained `idle`; unavailable and error states do not advance.

Hardware transport gestures also require exact provenance. When the Voice
state exposes `media_content_id`, it must match the item loaded for the
current Bedside controller generation. Voice PE's ESPHome media player may
omit that attribute: a live `playing`, `paused` or `buffering` update then
preserves only an already-owned generation, while a startup or reconnect
snapshot without the ID still revokes ownership. The filtered Core service
event closes the same-state replacement gap. An exact current media ID called
by the authenticated Bedside app user can confirm the current generation;
a different user, different ID or missing ID on a `play_media` call targeting
the Voice entity revokes ownership immediately. Calls for other media players
are ignored. The Ingress remote remains available, and a new successful
Bedside Play can establish a new generation, but firmware gestures otherwise
remain fail-closed.

The stock dial path observes `volume_level`, calls the same
`PlaybackController.set_volume` path as the web remote, and caps output at the
lower of 50 percent or `max_volume`. Stock firmware can transiently overshoot
by one 5 percent dial step before the bridge sends one tightly allowlisted
`media_player.volume_set` correction. Duplicate observations and correction
echoes do not repeat commands.

When `voice_volume_cap_number_entity` is configured, startup/reconnect
snapshots and live number events validate the fixed `0.0..0.50`/`0.05`
contract and idempotently synchronize it to the lower of `0.50` or the
controller's configured maximum through the exact allowlisted
`number.set_value` service. Entity IDs remain operator-configured because the
firmware keeps its MAC suffix enabled.

The reproducible custom image and its exact source, build, recovery, and
licensing record are in [`firmware/voice-pe`](../firmware/voice-pe). Its unheld
dial path clamps locally before applying each step; the bridge remains
authoritative for external volume correction.

The LED reducer uses `playing`, `paused` and `sleeping`; stopped or idle means
`sleeping`, including while a sleep timer is active. `off` is reserved for
startup or unavailable output. The stock light receives a best-effort uniform
color/brightness representation, while custom firmware receives only the
fixed select options. Bedside never addresses individual pixels and does not
override local Assist, mute or error indications; it reapplies its display
only after Assist returns idle.

The app is Ingress-only, publishes no host or media ports, requires no
filesystem mount and keeps the HA owner in `/data/bedside-audio/owner.json`
with mode `0600` under a `0700` directory. Controls require the internal
Ingress gateway, an HA user identity, a control header and origin/fetch-site
checks. Other HA users cannot browse or play. DLNA itself may deliver
media over plain HTTP inside the trusted home network; this is separate
from the HA browser session. Bedside rejects off-source and malformed
media IDs, but cannot inspect Core's resolved URL or its redirects.

## Migrating an existing local installation

Home Assistant namespaces Apps by their source repository. Adding this custom
repository does not replace a local `local_bedside_audio` installation or
move its `/data` directory. Stop before installing if preserving local app
state matters, and follow the
[local-app migration guide](../docs/migrating-local-app.md). Do not delete the
local installation until the repository app is configured, ownership is
verified, and rollback requirements are understood.

## Offline validation and remaining hardware gates

From the repository root, run
`uv lock --project bedside-audio --check`,
`uv run --project bedside-audio --locked --extra test pytest -q tests`,
`node --test tests/*.test.js`, and
`node --check bedside-audio/bedside_audio/static/app.js`.
`docker build bedside-audio` checks the production image without installing
it on Home Assistant.
Tests use synthetic legacy owner records and fake Core browse/player
responses; they do not read real Plex credentials or play audio.

A separate owner-approved hardware trial still needs to establish
Bedside-controlled headphone sound for a whole episode, both channels,
browser-closed timer expiry, manual Next/Previous, Stop during Assist
and Voice continuity. The approved short native HA test and Bedside's
read-only DLNA browse do not prove those behaviors. The
[Voice feasibility note](../docs/voice-preview-feasibility.md) links to
first-party Ingress, DLNA and Core playback evidence.
