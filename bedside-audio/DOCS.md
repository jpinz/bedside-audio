# Bedside hardware

Bedside hardware is a headless Home Assistant App for using Home Assistant
Voice Preview Edition as a physical controller and headphone output for Music
Assistant.

Music Assistant must already be installed, configured with the local media
source, and connected to Home Assistant through the official integration. The
integration creates a Music Assistant `media_player.*` entity for the Voice PE
player. Configure that entity as the playback target.

The App does not contain a media browser, queue, sleep timer, playlist manager,
saved playback state, or web interface. Music Assistant owns those behaviors.

## Hardware behavior

The custom firmware exposes button events and optional LED theme and volume-cap
entities. The App subscribes to the configured entities through the Supervisor
Core WebSocket and sends tightly allowlisted Core actions through the
Supervisor REST interface.

| Gesture | Music Assistant action |
| --- | --- |
| Single press | Toggle play and pause |
| Double press | Next queue item |
| Triple press after 10 seconds | Seek to the start of the current item |
| Triple press at or before 10 seconds | Previous queue item |

Gestures are ignored unless the Music Assistant player is playing or paused,
the player is available, and Assist is idle and unmuted. Duplicate Home
Assistant events are ignored.

The firmware still handles higher-priority local behavior before publishing a
Bedside event. A single press first stops a ringing Voice timer, active Assist
run, or announcement. Long press, reset, and the easter egg retain their
firmware behavior.

## LED behavior

The App maps the Music Assistant player state to the Voice LED intent:

| Player state | LED intent |
| --- | --- |
| Playing or buffering | Playing |
| Paused | Paused |
| Idle or stopped | Sleeping |
| Native Voice player unavailable | Off |

Assist, mute, timers, setup, connectivity, button feedback, and firmware
diagnostics keep priority over the Bedside display. If another controller
changes the LED, the App waits until Assist returns to idle before restoring
the playback display.

For stock firmware, configure the Voice LED `light.*` entity. For the custom
firmware, configure the Bedside display `select.*` entity. Configure exactly
one of those outputs.

## Volume safety

`max_volume` is limited to 1 through 50 percent. The App observes the native
Voice PE media player, not the Music Assistant duplicate, and corrects any
reported volume above the configured cap.

The custom firmware also exposes a `number.*` entity that enforces the cap
inside the device for dial changes and restored volume. When configured, the
App synchronizes that number to `max_volume`. This firmware-level cap is the
preferred protection. Because the firmware number moves in 5 percent steps,
`max_volume` must also use a 5 percent step when that entity is configured.

## Configuration

| Option | Purpose |
| --- | --- |
| `music_assistant_player_entity` | Music Assistant media-player entity used for playback state and button actions. |
| `voice_media_player_entity` | Native ESPHome Voice PE media-player entity used for availability and volume enforcement. |
| `max_volume` | Maximum native Voice volume from 1 to 50 percent. |
| `voice_button_event_entity` | Custom-firmware `event.*` entity for center-button gestures. |
| `voice_assist_satellite_entity` | Voice PE `assist_satellite.*` entity used to preserve Assist priority. |
| `voice_led_light_entity` | Stock Voice PE uniform LED `light.*` entity. Configure this or the select entity. |
| `voice_led_select_entity` | Custom-firmware Bedside display `select.*` entity. Configure this or the light entity. |
| `voice_led_theme_text_entity` | Optional custom-firmware `text.*` entity for the LED theme payload. |
| `voice_volume_cap_number_entity` | Optional custom-firmware `number.*` entity for the device-level volume cap. |
| `led_*_color` | Canonical uppercase `#RRGGBB` color for each Bedside state. |
| `led_*_brightness` | Brightness from 1 through 100 percent for each Bedside state. |

After upgrading from the previous DLNA App, remove the old DLNA, library,
playlist, and owner options. The loader temporarily tolerates those stored
keys, but they have no effect. `media_player_entity` is accepted only as a
migration fallback for `voice_media_player_entity`.

## Music Assistant setup

1. Add the local folder as a Music Assistant Local files provider.
2. Select **Podcasts** as the content type for episodic TV audio.
3. Add the native Voice PE media player to Music Assistant.
4. Install the official Music Assistant integration in Home Assistant.
5. Find the Music Assistant duplicate of the Voice player and configure it as
   `music_assistant_player_entity`.
6. Keep the original ESPHome player as `voice_media_player_entity`.

Music Assistant supports `.m4a` files in `/media` on Home Assistant OS.

## Validation

From the repository root:

```sh
uv sync --project bedside-audio --extra test
uv run --project bedside-audio pytest
docker build bedside-audio
```

Firmware build and operator safety instructions are documented separately in
[`firmware/voice-pe/README.md`](../firmware/voice-pe/README.md).
