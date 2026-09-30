# Releasing Bedside Audio

The App version is kept in these files:

- `bedside-audio/config.yaml`
- `bedside-audio/pyproject.toml`
- `bedside-audio/uv.lock`
- `bedside-audio/Dockerfile`
- `bedside-audio/CHANGELOG.md`

For a release:

1. Choose the semantic version and update every location above.
2. Run the repository validation commands documented in
   `bedside-audio/DOCS.md`.
3. Build the production image from `bedside-audio/`.
4. Merge the reviewed change to the default branch. The builder workflow uses
   `home-assistant/builder` 2026.09.0 to publish versioned `amd64` and
   `aarch64` images, then publishes
   `ghcr.io/jpinz/bedside-audio:<version>` as their multi-architecture
   manifest. It also updates `latest`.
5. Confirm the multi-architecture manifest exists before installing or
   updating the App in Home Assistant.

Do not advertise an architecture until the Docker base image and all runtime
dependencies build for it. Do not reuse a released version for different
source. The first GHCR package may need its package visibility changed to
public before Home Assistant can pull it without credentials.
