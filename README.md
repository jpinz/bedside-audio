# Bedside hardware

Bedside hardware is a headless Home Assistant App that maps Home Assistant
Voice Preview Edition controls to one Music Assistant player.

Music Assistant owns the media library, queue, playback history, seeking, and
streaming. This App owns only the Voice PE hardware behavior:

- Single press toggles play and pause.
- Double press advances to the next item.
- Triple press restarts after 10 seconds or goes to the previous item.
- The LED ring reflects Music Assistant playback without overriding Assist,
  mute, timer, setup, or diagnostic states.
- The native Voice media player and optional firmware number entity enforce a
  configured volume cap.

## Install

[![Open your Home Assistant instance and add this repository](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fjpinz%2Fbedside-audio)

Or add this URL in **Settings > Apps > App store > Repositories**:

```text
https://github.com/jpinz/bedside-audio
```

Install Music Assistant and its Home Assistant integration first. Then install
**Bedside hardware (Music Assistant)** and configure the Music Assistant
player, native Voice player, button, Assist satellite, and LED entities.

## Documentation

- [App configuration and behavior](bedside-audio/DOCS.md)
- [Custom Voice PE firmware](firmware/voice-pe/README.md)
- [Release process](docs/releasing.md)
- [Changelog](bedside-audio/CHANGELOG.md)

The App supports `amd64` and `aarch64`. It has no Ingress page, media library,
queue, public port, or Music Assistant credentials.
