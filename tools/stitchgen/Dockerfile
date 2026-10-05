# Inkscape + Ink/Stitch pinned in one image so local runs and servers convert identically.
FROM ubuntu:24.04

ARG TARGETARCH
ARG INKSTITCH_VERSION=3.3.0
ENV DEBIAN_FRONTEND=noninteractive

# Ink/Stitch bundles wxPython and needs GTK/GL libraries plus an X server (xvfb) even when run headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
      inkscape xz-utils curl ca-certificates python3 python3-venv \
      libdrm2 libgbm1 libegl1 libgl1 libgtk-3-0 libnotify4 libsm6 libxtst6 libglu1-mesa libwayland-server0 \
      xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo aarch64 || echo x86_64) \
    && mkdir -p /root/.config/inkscape/extensions \
    && curl -fsSL "https://github.com/inkstitch/inkstitch/releases/download/v${INKSTITCH_VERSION}/inkstitch-${INKSTITCH_VERSION}-linux-${ARCH}.tar.xz" \
       | tar -xJ -C /root/.config/inkscape/extensions

WORKDIR /app
COPY pyproject.toml ./
COPY stitchgen ./stitchgen
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .
COPY config.toml palette.json ./

ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /work
ENTRYPOINT ["stitchgen"]
