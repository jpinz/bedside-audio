# Bedside Voice PE firmware

This directory contains a standalone custom firmware configuration for the
standard 16 MB Home Assistant Voice Preview Edition. It is a thin hardware
adapter for the Bedside hardware App. Music Assistant remains authoritative
for the queue, playback history, seeking, and transport policy. The App maps
the firmware events to the configured Music Assistant player and applies the
10-second previous-or-restart rule.

## Source and licensing

The behavioral base is the official Voice PE `26.9.0` tag at commit
`2644f4c794271d735d182ca7ebf899ed46164f4e`. The unmodified upstream
`home-assistant-voice.yaml` at that commit has SHA-256
`e69845fd24011c37969ffd8c373a87babaead41d8785a12a85f51b6b672103c4`.
`bedside-voice-pe.yaml` vendors that complete configuration rather than
layering button, dial, or LED lists through ESPHome packages.

The upstream ESPHome license is in `UPSTREAM_LICENSE`. The official sound
attribution is in `SOUNDS_LICENSE.md`. Sound URLs and the `voice_kit` external
component are pinned to the same immutable source commit. The XMOS firmware is
the upstream pinned `voice-kit-xmos-firmware` v1.3.1 binary and checksum.
The official Jarvis, Mycroft, and VAD manifests are pinned to
`esphome/micro-wake-word-models` commit
`05b65922cc433c9df13e98e32a7fe520758c837e`, matching the manifests resolved
by the validated build.

## Home Assistant contract

The firmware exposes:

- `event.<device>_button_press`, with `single_press`, `double_press`,
  `triple_press`, `long_press`, and `easter_egg_press`.
- `select.<device>_bedside_display_intent`, with exactly `off`, `playing`,
  `paused`, and `sleeping`. It boots to `off` and does not restore stale state.
- `number.<device>_bedside_volume_cap`, bounded from `0.0` through `0.50`,
  stepping by `0.05`. It boots to `0.50` and does not restore stale state.
- `text.<device>_bedside_led_theme`, a fixed 50-character versioned payload
  carrying the playing, paused, sleeping, and shared button-press color and
  brightness styles.

Configure the event and select entities through the Bedside hardware App
options. Configure the number entity through
`voice_volume_cap_number_entity`; the app then synchronizes the number to its
`max_volume` option. Leaving that option blank preserves earlier behavior and
the firmware keeps its safe 50 percent default.

After this firmware is installed, configure its exact text entity through
`voice_led_theme_text_entity`. The App sends one payload in this format:

```text
v1|#RRGGBB@PPP|#RRGGBB@PPP|#RRGGBB@PPP|#RRGGBB@PPP
```

The fields are playing, paused, sleeping, and button press. The firmware
requires the exact length, uppercase canonical hex colors, separators, and
brightness from `001` through `100`. It validates all four styles before
assigning any of them, persists only the complete valid payload, and otherwise
keeps the last valid theme or built-in defaults.

Single press first stops a ringing Voice timer, an active Assist run, or an
announcement. With Bedside intent `playing` or `paused`, it emits
`single_press` and does not start Assist. With intent `off` or `sleeping`, the
official local media and Assist flow remains in place. Double and triple press
only emit events. Their feedback sounds were removed because an announcement
sound invokes the official 20 dB media duck; the physical gesture and button
LED response remain. Long press, reset, and the easter egg retain official
behavior.

Normal unheld dial steps use `media_player.volume_set` and clamp immediately
to `0.0..bedside_volume_cap`. The official held-button group-volume or hue
gesture, transient volume display, one-second timing, and encoder reset remain.
The App still observes the native Voice volume and may correct external calls.

Bedside LED effects use all 12 internal pixels:

- `playing`: clockwise moving `#00FF30` at 12 percent by default.
- `paused`: static four-point `#FF7000` at 18 percent by default.
- `sleeping`: breathing `#6000A0` at 8 percent by default.
- physical button press: full-ring `#18BBF2` at 10 percent by default.

The shared button style is active only while the physical center button is
held. Release returns through the existing LED reducer. It does not create
separate single, double, or triple gesture feedback.

They run only at the final normal-idle tier. Voice-kit startup failure,
provisioning/startup, no HA connection, button, jack, dial, ringing timer,
Assist phases/errors, Voice timer progress, mute, and zero volume all take
priority. The diagnostic LED Ring can affect official idle display only; it cannot
supersede those states or an active Bedside display. Music Assistant playback
state determines whether the base intent is playing, paused, or sleeping.

Initial publication of the Bedside number and select routes through one
restart-mode deferred refresh. This coalesces their setup callbacks, waits
until addressable-light setup has completed, and then renders the current
intent without changing the LED priority reducer. The restored theme is
strictly parsed before that render; the text entity publishes only the valid
stored or default payload.

For upgrades, update the App with the theme entity blank, install
firmware `26.9.0-bedside.5` through the separately approved operator process,
confirm the exact new `text.*` entity ID in Home Assistant, then add that ID to
the App options.

## First-flash provisioning

The factory image does not embed Wi-Fi credentials. Its credential-free
`wifi:` configuration retains the official Voice PE `improv_serial` and
button-authorized BLE Improv flows, so the operator provisions the 2.4 GHz
network after the serial flash. The only build secret is one unique 32-byte
base64 API encryption key. That API encryption key also protects encrypted
ESPHome OTA; no separate OTA password or key is required.

A non-erasing serial flash can preserve previously stored Wi-Fi credentials
in the ESP32 NVS partition. That still avoids embedding the password in the
firmware image, but it does not guarantee a fresh Improv onboarding flow.
Guaranteeing fresh onboarding requires separately approved erasure or reset,
which destroys the stored Wi-Fi credentials and existing Home Assistant API
key.

## Build only

Use Python 3.11 or newer and a pinned ESPHome 2026.9.x environment outside the
repository:

```sh
python3 -m venv /tmp/bedside-esphome-2026.9
/tmp/bedside-esphome-2026.9/bin/pip install "esphome==2026.9.1"
cp firmware/voice-pe/secrets.example.yaml firmware/voice-pe/secrets.yaml
# Replace the API encryption key placeholder. Never commit secrets.yaml.
/tmp/bedside-esphome-2026.9/bin/esphome config \
  firmware/voice-pe/bedside-voice-pe.yaml
/tmp/bedside-esphome-2026.9/bin/esphome compile \
  firmware/voice-pe/bedside-voice-pe.yaml
```

Compilation may download public ESPHome, PlatformIO, component, model, sound,
and toolchain artifacts. Caches and `secrets.yaml` are ignored. This repository
does not add ESPHome as an app runtime dependency.

## Recorded validation

On 2026-09-30, firmware `26.9.0-bedside.5` passed both `esphome config` and
`esphome compile` with ESPHome 2026.9.1, ESP-IDF 5.5.5, and a temporary
non-deployable API/OTA key used only for secret-free build validation. It did
not connect to Home Assistant or hardware. ESPHome reported config hash
`0x357752e4`, 3,194,747 bytes of image content, 51.1 percent DIRAM use, and
39.3 percent app-partition use. The generated binaries remain ignored and
must not be flashed because the validation key is not an operator key.

The previous `26.9.0-bedside.4` build was validated on 2026-09-29 with:

```sh
uvx --python 3.12 --from 'esphome==2026.9.1' \
  esphome config firmware/voice-pe/bedside-voice-pe.yaml
uvx --python 3.12 --from 'esphome==2026.9.1' \
  esphome compile firmware/voice-pe/bedside-voice-pe.yaml
```

The build reused the unique API/OTA encryption key in the ignored
`secrets.yaml` and no compile-time Wi-Fi credentials; it did not connect to HA
or hardware. ESPHome reported firmware version `26.9.0-bedside.4`, config hash
`0x2b863c31`, ESP-IDF 5.5.5, 3,188,715 bytes of image content, 51.0 percent
DIRAM use, and 39.2 percent app-partition use.

| Artifact | Size | SHA-256 |
| --- | ---: | --- |
| `firmware.factory.bin` | 3,254,368 bytes | `17a3d31dca7309cc1212efa3a4c8eabfb5c12504fb992852bb33aa15b6dcecaa` |
| `firmware.ota.bin` | 3,188,832 bytes | `3e2a93c501d6222eb350f1ffa58453be2f73c8ef83f52e86af81ecfa8054ee5b` |

The generated binaries remain ignored build artifacts and are not committed.
Warnings were limited to upstream behavior: the external `voice_kit.flash`
action declaration, official strapping-pin assignments, and the upstream
`rgb_order` deprecation. Compilation completed successfully.

## Flash and recovery gates

No command in this layer installs, flashes, updates, or discovers a device.
Flashing requires separate operator approval after reviewing the binary and
backing up the current device configuration. The stock production HTTP updater
and Beta firmware switch are intentionally absent, so HA cannot replace this
custom image through the ordinary Voice PE firmware update entity. Firmware
updates are operator-managed.

To return to stock, obtain the official Voice PE installer linked from
<https://voice-pe.home-assistant.io/> and use its documented USB recovery
procedure for the standard 16 MB device. That erases this custom firmware and
may require reprovisioning. Opening the installer is not approval to connect,
erase, or flash hardware; each action remains a separate explicit gate.
