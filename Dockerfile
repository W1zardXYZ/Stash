# syntax=docker/dockerfile:1.7
# Stash - self-hosted 3D asset library (web UI + API + workers + headless Blender)

# ---------------------------------------------------------------- web UI
FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm
ARG BLENDER_VERSION=4.5.14
ARG BLENDER_SERIES=4.5
# Set INSTALL_BLENDER=false for a small image without conversion/extraction (e.g. arm64 NAS)
ARG INSTALL_BLENDER=true
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STASH_DATA_DIR=/data \
    STASH_FRONTEND_DIR=/app/frontend/dist \
    STASH_INTEGRATIONS_DIR=/app/integrations \
    STASH_BROWSE_ROOTS=/library,/data/uploads \
    HOME=/tmp

# Runtime libraries Blender needs in background mode (no display): X11/GL stubs, OpenMP for Cycles
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils tini \
        libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 \
        libgl1 libegl1 libglx-mesa0 libgomp1 libwayland-client0 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$INSTALL_BLENDER" = "true" ] && [ "${TARGETARCH:-amd64}" = "amd64" ]; then \
        curl -fsSL "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" -o /tmp/blender.tar.xz \
        && mkdir -p /opt/blender \
        && tar -xf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
        && rm /tmp/blender.tar.xz \
        && /opt/blender/blender -b --factory-startup -noaudio --python-expr "import bpy; print('blender ok', bpy.app.version_string)"; \
    else echo "Skipping Blender (INSTALL_BLENDER=$INSTALL_BLENDER, arch=$TARGETARCH)"; fi

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY integrations/ integrations/
COPY --from=ui /ui/dist frontend/dist

# Works as root or as an unprivileged user (e.g. TrueNAS "apps", 568:568) that owns /data
RUN mkdir -p /data /library && chmod 777 /data
VOLUME ["/data"]
EXPOSE 8420
WORKDIR /app/backend
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8420/api/status || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "stash", "serve", "--host", "0.0.0.0", "--port", "8420"]
