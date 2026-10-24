FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.08.0@sha256:fb41585796deece71afd5a965e9a8aa1b5c2ceb4f020389d998fbe578dbbde6a
ARG BUILD_VERSION=0.1.2
ARG BUILD_ARCH=aarch64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
WORKDIR /opt/house-brain-energy-ai
COPY energy_ai energy_ai
COPY tools tools
COPY Dockerfile .dockerignore config.yaml DOCS.md README.md requirements.txt SOURCE_MANIFEST.sha256 ./
ENTRYPOINT ["/usr/local/bin/python3", "-m", "energy_ai"]
