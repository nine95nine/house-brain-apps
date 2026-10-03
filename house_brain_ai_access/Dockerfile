# Immutable multi-arch HA base; 2026.08.0 replaces 2026.06.1 (2026-09-29 scan: removes the CRITICAL
# and 31 of 61 HIGH OS findings). The accepted Observer/Scout/Inspector still pin 2026.06.1.
# Python 3.14 / Alpine 3.24 / s6-overlay 3.2.2.0: final image parity still required.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.08.0@sha256:fb41585796deece71afd5a965e9a8aa1b5c2ceb4f020389d998fbe578dbbde6a
ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.1.0-dev
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    S6_SERVICES_GRACETIME=15000 S6_KILL_GRACETIME=3000
WORKDIR /opt/house-brain-ai-access
COPY runtime ./runtime
COPY app.py child.py vendor.lock.json ./
COPY install_vendor.py /tmp/install_vendor.py
RUN addgroup -g 10001 hbaccess && adduser -D -H -u 10001 -G hbaccess hbaccess \
    && python3 /tmp/install_vendor.py --arch "${BUILD_ARCH}" \
       --lock /opt/house-brain-ai-access/vendor.lock.json \
       --destination /opt/house-brain-ai-access/vendor \
    && python3 -I /opt/house-brain-ai-access/app.py verify \
    && find /opt/house-brain-ai-access -type d -exec chmod 0555 {} \; \
    && find /opt/house-brain-ai-access -type f -exec chmod 0444 {} \; \
    && chmod 0555 /opt/house-brain-ai-access/vendor/tunnel-client \
    && rm /tmp/install_vendor.py
COPY rootfs /
RUN chmod 0555 /etc/services.d/house-brain-ai-access/run \
    /etc/services.d/house-brain-ai-access/finish
# Keep the base image's native init/supervisor; no shell restart loop.
ENTRYPOINT ["/init"]
CMD []
