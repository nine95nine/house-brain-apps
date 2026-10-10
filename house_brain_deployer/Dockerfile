FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.08.0@sha256:fb41585796deece71afd5a965e9a8aa1b5c2ceb4f020389d998fbe578dbbde6a

ARG BUILD_VERSION=0.3.11
ARG BUILD_ARCH=aarch64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/house-brain-deployer

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --requirement /tmp/requirements.txt \
  && rm /tmp/requirements.txt

COPY hbd/ /opt/house-brain-deployer/hbd/
RUN chmod -R a-w /opt/house-brain-deployer

# Override the base image's s6-overlay /init; Python is PID 1 and handles SIGTERM.
ENTRYPOINT ["/usr/local/bin/python3", "-m", "hbd.main"]
