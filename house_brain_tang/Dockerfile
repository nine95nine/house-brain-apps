FROM ghcr.io/home-assistant/base-debian:trixie-2026.11.0@sha256:d996c8f8ef0096a1ac3756766149b31b8171afc663068cc192f2bcab20f1b8c9
ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=aarch64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
# Debian trixie packages at exact versions: Tang's own server and key tool, socat, and Python for /health only.
# No key is made at build time: the image must carry an empty /var/lib/tang and no *.jwk anywhere.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tang=15-2 socat=1.8.0.3-1+deb13u1 python3=3.13.5-1 \
 && rm -rf /var/lib/apt/lists/* \
 && test -x /usr/libexec/tangd && test -x /usr/libexec/tangd-keygen && test -x /usr/bin/tang-show-keys \
 && test -x /usr/bin/curl && getent passwd _tang \
 && test -z "$(find / -xdev -name '*.jwk' -print -quit)"
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/house-brain-tang
COPY tang_app tang_app
COPY Dockerfile .dockerignore config.yaml DOCS.md README.md SOURCE_MANIFEST.sha256 ./
ENTRYPOINT ["/usr/bin/python3", "-m", "tang_app"]
