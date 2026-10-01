# Migrating from a local App

Home Assistant treats an App from a custom repository as a different package
from one installed from `/addons`. The existing local slug
`local_bedside_audio` is not overwritten by adding this repository, and its
private `/data` directory is not copied automatically.

Before installing the repository App:

1. Record the existing Bedside options through Home Assistant's supported App
   configuration UI. Do not copy Supervisor private files or tokens.
2. Decide whether the local App's owner and session state must be preserved.
   There is no automatic or documented cross-App data migration in this
   release.
3. Keep the local App stopped while evaluating a separately configured
   repository App. Do not run both against the same Voice media player.
4. Configure the repository App with the intended owner, DLNA source, browse
   player, volume cap, playlists, LED styles, and optional Voice bridge
   entities. Leave `voice_led_theme_text_entity` blank until custom firmware
   `26.9.0-bedside.5` is installed and its exact text entity is visible.
5. Verify signed-in owner access and read-only library browsing before any
   playback test. Playback, timer, firmware, and cleanup tests require their
   own operator approval.
6. Remove the local App only after the repository App is verified and its
   rollback implications are accepted.

Removing a local source directory is not an App-data backup. Installing this
repository also does not flash Voice PE firmware or change Home Assistant
integrations.
