FROM python:3.12-slim-bookworm

LABEL io.hass.version="0.7.0" \
      io.hass.type="app" \
      io.hass.arch="aarch64|amd64"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/bedside-audio
COPY pyproject.toml DOCS.md ./
COPY bedside_audio ./bedside_audio
RUN pip install --no-cache-dir .

CMD ["python", "-m", "bedside_audio.ha_addon"]
