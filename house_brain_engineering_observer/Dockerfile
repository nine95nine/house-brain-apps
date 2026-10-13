FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.08.0@sha256:fb41585796deece71afd5a965e9a8aa1b5c2ceb4f020389d998fbe578dbbde6a

ARG BUILD_VERSION=0.2.5
ARG BUILD_ARCH=aarch64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

WORKDIR /opt/house-brain-engineering-observer

COPY observer.py observer_source.py observer_supervisor.py app_inventory.py read_worker.py tailscale_identity.py observer_entrypoint.py /opt/house-brain-engineering-observer/

RUN chmod 0555 /opt/house-brain-engineering-observer/observer.py \
  /opt/house-brain-engineering-observer/app_inventory.py \
  /opt/house-brain-engineering-observer/read_worker.py \
  /opt/house-brain-engineering-observer/tailscale_identity.py \
  /opt/house-brain-engineering-observer/observer_entrypoint.py

# Override the Home Assistant base image's inherited s6-overlay /init entrypoint.
# The Observer has no child-process supervision requirement outside its Python coordinator.
ENTRYPOINT ["/usr/local/bin/python3", "/opt/house-brain-engineering-observer/observer_entrypoint.py"]
