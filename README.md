# Bedside Audio

Bedside Audio is an experimental Home Assistant App that sends television
audio from Home Assistant's configured DLNA media source to wired headphones
on Home Assistant Voice Preview Edition. Its Ingress page is the remote;
the App does not expose a host media port or store Plex credentials.

## Install

[![Open your Home Assistant instance and add this repository](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fjpinz%2Fbedside-audio)

Or add this URL in **Settings > Apps > App store > Repositories**:

```text
https://github.com/jpinz/bedside-audio
```

Refresh the App store, open **Bedside audio (Voice)**, review the configuration,
and install it. The optional custom Voice PE firmware is separate and is never
flashed or updated by the App.

**Existing local installation:** a custom-repository App does not overwrite
`local_bedside_audio` or automatically inherit its data. Read the
[local-app migration guide](docs/migrating-local-app.md) before installing a
second copy.

## Documentation

- [App configuration and behavior](bedside-audio/DOCS.md)
- [Release and image publishing](docs/releasing.md)
- [Local-app migration](docs/migrating-local-app.md)
- [Custom Voice PE firmware](firmware/voice-pe/README.md)
- [Changelog](bedside-audio/CHANGELOG.md)

The App supports `amd64` and `aarch64`. Bedside Audio remains fail-closed:
Home Assistant supplies identity, browsing, and media transport; the App owns
queue, timer, provenance, and transport policy; the firmware remains a thin
event and display adapter.
