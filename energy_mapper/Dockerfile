# https://developers.home-assistant.io/docs/apps/dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.24
FROM ${BUILD_FROM}

# Standard library only: no pip install. tzdata gives zoneinfo the local
# time zone of Home Assistant (day boundaries of the reports).
RUN apk add --no-cache tzdata

WORKDIR /app
COPY app/ /app/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# s6-overlay (the /init entrypoint) clears the environment of CMD:
# with-contenv restores it (SUPERVISOR_TOKEN, TZ...)
CMD ["/usr/bin/with-contenv", "python3", "/app/main.py"]

LABEL \
    io.hass.type="app" \
    io.hass.name="Energy Mapper" \
    io.hass.description="Identify the appliances of the home from the whole-house power meter" \
    io.hass.url="https://github.com/sylvaing/ha-energy-mapper" \
    org.opencontainers.image.title="Energy Mapper" \
    org.opencontainers.image.description="Identify the appliances of the home from the whole-house power meter" \
    org.opencontainers.image.source="https://github.com/sylvaing/ha-energy-mapper" \
    org.opencontainers.image.licenses="MIT"
