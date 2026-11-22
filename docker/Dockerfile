# Retentacle — backup server for Shelly devices.
#
# Builds the Python package into a slim image. All state (backup database,
# settings, users) lives in the data folder (RETENTACLE_DATA_DIR, default
# /data), which should be backed by a volume.
#
# Build from the repository root (the build context must contain the package):
#   docker build -t retentacle -f docker/Dockerfile .
#   docker run -d -p 8080:8080 -v retentacle-data:/data retentacle
#
# The container starts as root, fixes ownership of the data folder (fresh
# host bind mounts are root-owned) and then drops to the unprivileged
# PUID/PGID user before launching the server. Override PUID/PGID to match
# an existing host folder. Extra command line flags are appended last:
#   docker run -d -e RETENTACLE_PORT=9000 -e PUID=568 -e PGID=568 ... retentacle --discover
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only the packaging inputs and the package itself are copied, so dependency
# installation stays cached unless those files change.
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY retentacle ./retentacle

RUN useradd --create-home --uid 10001 retentacle && pip install .

# Bind address, port and data folder of the server; PUID/PGID select the
# unprivileged user the server actually runs as. Override all with -e.
ENV RETENTACLE_HOST=0.0.0.0 \
    RETENTACLE_PORT=8080 \
    RETENTACLE_DATA_DIR=/data \
    PUID=10001 \
    PGID=10001 \
    HOME=/home/retentacle

# Pre-create the data folder owned by the default user, so an empty named
# volume inherits writable ownership without the startup chown. The VOLUME
# instruction cannot use environment variables and always refers to /data;
# a custom RETENTACLE_DATA_DIR must be mounted or otherwise writable.
RUN mkdir -p "$RETENTACLE_DATA_DIR" \
    && chown -R 10001:10001 "$RETENTACLE_DATA_DIR" /app

COPY docker/docker-entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

# Intentionally no USER instruction: the entrypoint runs the server as
# PUID/PGID after preparing the data folder (see docker-entrypoint.sh).

VOLUME /data
EXPOSE 8080

# Uses the public /health endpoint on the configured port.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('RETENTACLE_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5).getcode() == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
