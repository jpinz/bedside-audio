# Voice Preview Edition DLNA playback feasibility

**Updated September 29, 2026.** Bedside runs as a Home Assistant OS app.
Its signed-in Ingress browser is a remote; wired headphones plugged into
Home Assistant Voice Preview Edition (Voice PE) are the sound output. The HA
host audio jack and the browser are not renderers. The user chose DLNA-only
Bedside playback and declined an audio or timer test for this refactor.

The listener heard a short HA Cloud TTS prompt through the headphones at
15%, then heard about eight seconds of an episode when HA natively played
an exact Plex DLNA Media Source ID through Voice at 15%. `media_stop`
returned the player to `idle`. That was **not** a Bedside-controlled
playback test. Bedside v0.4.0, installed on HA, has successfully browsed
Video / TV Shows through Ingress as the saved HA owner. Its read-only
browse returned TV folders without an error; the configured browse TV
was not sent a playback action. Voice remained idle at 15%. A whole
episode, sleep timer, manual queue and Assist continuity still need
separate owner approval and hardware validation.

## First-party route and limits

- [HA OS apps](https://www.home-assistant.io/getting-started/concepts-terminology/#apps)
  run alongside Core. [Ingress](https://developers.home-assistant.io/docs/apps/presentation/#ingress)
  authenticates the browser and supplies its HA user identity. The app
  calls Core using [Supervisor's Core API](https://developers.home-assistant.io/docs/apps/communication/#home-assistant-core),
  not a browser token. App configuration can enable Ingress and
  `homeassistant_api` without host audio, host networking or published
  ports. [App security](https://developers.home-assistant.io/docs/apps/security/).
- Voice PE provides a [3.5 mm stereo output](https://www.home-assistant.io/voice-pe/#specs).
  Its network-fed native media player receives audio from Core. Firmware
  changes affect advertised formats; do not assume a codec found in a
  newer firmware is on this device.
  [Reported media player](https://github.com/esphome/home-assistant-voice-pe/blob/a163e7b980c572df9d3811ff98094350f5aae541/home-assistant-voice.yaml#L1555-L1626),
  [later firmware](https://github.com/esphome/home-assistant-voice-pe/blob/2644f4c794271d735d182ca7ebf899ed46164f4e/home-assistant-voice.yaml#L1595-L1667).
- HA's built-in [DLNA Digital Media Server integration](https://www.home-assistant.io/integrations/dlna_dms/)
  exposes `media-source://dlna_dms/...` items. Core's response-only
  [`media_player.browse_media` REST service](https://developers.home-assistant.io/docs/api/rest/#post-apiservicesdomainservice)
  uses `?return_response` and returns the selected entity's tree under
  `service_response[entity_id]`. A video-capable Cast TV exposed playable
  Plex video items; Voice's own audio-only browse hid those same videos.
  Bedside therefore uses an explicitly configured video-capable entity
  **only to browse**, then sends the validated Media Source ID to Voice.
  The live Bedside v0.4.0 Ingress browse confirmed that Core REST route.
- In Core 2026.9.1, the
  [ESPHome media-player path](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/esphome/media_player.py#L133-L226)
  resolves the Media Source ID and can proxy audio to Voice. Its
  [FFmpeg proxy](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/esphome/ffmpeg_proxy.py#L137-L191)
  removes video (`-vn`). The native entity has no reliable episode
  position, duration or seek:
  [ESPHome traits](https://github.com/esphome/esphome/blob/c6e4c87e525dd343e470d8dea368957a002f6505/esphome/components/speaker_source/speaker_source_media_player.cpp#L757-L771).
- Stock Voice firmware ducks media for Assist and restores volume afterward,
  but that does not prove exact resume, missed-word replay or continued
  playback if the media player becomes `idle`.
  [Firmware handlers](https://github.com/esphome/home-assistant-voice-pe/blob/a163e7b980c572df9d3811ff98094350f5aae541/home-assistant-voice.yaml#L1848-L1891),
  [HA state meanings](https://www.home-assistant.io/integrations/media_player/#the-state-of-a-media-player).

## Security and owner migration

Bedside's DLNA-only design accepts Ingress controls from one HA user.
A fresh installation needs an explicit trusted `dlna_owner_user_id`.
An upgrade reads the same HA user ID from v0.4.0's private Plex owner
record, writes a separate `0600` owner file and a durable `0600` ownership
guard, and fails closed on absent, corrupt or mismatched ownership. Loss
of the owner file after local credential deletion cannot turn a previously
owned installation into a fresh one merely by changing an option. Normal
controls verify the new owner and legacy-file metadata without rereading
the old token. Incomplete `.plex-pin-*` or v0.4.0
`.plex-auth-<32 hex digits>` token staging
blocks cleanup rather than expanding the deletion list. The old
credential files remain on disk and unused until the new owner file
survives an app restart, the owner completes a fresh verified DLNA
Ingress browse and separately approves a local, owner-bound cleanup.
The stopped check and deletion serialize with playback start/skip, while
Stop/End and timer responses remain available. The app never
authorizes with Plex or revokes a Plex Authorized Device.

Core receives only an exact configured-source Media Source ID selected
from its browse response. Bedside exposes opaque browser handles,
re-browses a file's parent before playing, and rejects off-source IDs,
unsupported video metadata, raw URLs and traversal. It publishes no
control or media port and mounts no NAS path. DLNA delivery may use plain
HTTP within the trusted home network; Bedside cannot inspect Core's
resolved URL or redirects. Core owns the FFmpeg URL, so Bedside requests
`media_stop` and checks Voice `idle` but cannot revoke that URL. Even
`idle` does not establish physical silence.

Home Assistant's [app configuration guide](https://developers.home-assistant.io/docs/apps/configuration/#options--schema)
warns that removed option keys can remain in Supervisor's stored options
and produce warnings until an operator deletes them. The DLNA-only
parser tolerates v0.4.0's known Plex option keys as inert migration data;
it does not enable their old behavior. Updating Bedside, verifying its
new owner, removing obsolete Supervisor options and deleting old local
credential files are distinct, separately authorized stages. The user
declined an app-data backup. An old source archive cannot restore deleted
owner or token files.

## Remaining validation

Offline tests must cover owner migration and durability, fail-closed
identity/configuration mismatches, one-shot local cleanup, HA Core browse
response parsing, Voice playback requests, timer/Stop/End behavior,
Ingress and phone-sized UI. They use synthetic credentials only.

On HA, a separately approved DLNA-only update must first verify the
same signed-in owner can browse TV folders after an app restart, without
playing audio or deleting any legacy credential. Only after that proof
and separate local-deletion consent may the owner use the cleanup control.
The user has explicitly deferred the Bedside-controlled headphone,
sleep-timer, whole-episode and Assist tests; do not infer those outcomes
from the short native HA sound test or from read-only browsing.
