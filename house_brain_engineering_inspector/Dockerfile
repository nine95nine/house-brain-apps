FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.08.0@sha256:fb41585796deece71afd5a965e9a8aa1b5c2ceb4f020389d998fbe578dbbde6a

ARG BUILD_VERSION=0.2.16
ARG BUILD_ARCH=aarch64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/house-brain-engineering-inspector

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --requirement /tmp/requirements.txt

COPY inspector.py /opt/house-brain-engineering-inspector/inspector.py
RUN chmod 0555 /opt/house-brain-engineering-inspector/inspector.py
# v0.2.0 (#212 G0): opt-in entity snapshot sender and the byte-identical embedded builder as flat
# embedded.* files (tools/sync_inspector_embedded_sources.py keeps them equal to the repository sources).
COPY entity_sender.py /opt/house-brain-engineering-inspector/entity_sender.py
COPY registry_probe.py /opt/house-brain-engineering-inspector/registry_probe.py
COPY cooling_r18_site_probe.py /opt/house-brain-engineering-inspector/cooling_r18_site_probe.py
COPY embedded.* /opt/house-brain-engineering-inspector/
RUN chmod 0444 /opt/house-brain-engineering-inspector/entity_sender.py /opt/house-brain-engineering-inspector/registry_probe.py /opt/house-brain-engineering-inspector/embedded.*
RUN chmod 0444 /opt/house-brain-engineering-inspector/cooling_r18_site_probe.py

# Override the Home Assistant base image's inherited s6-overlay /init entrypoint.
# This one-shot app has no child-process supervision requirement; Python is PID 1.
ENTRYPOINT ["/usr/local/bin/python3", "/opt/house-brain-engineering-inspector/inspector.py"]
