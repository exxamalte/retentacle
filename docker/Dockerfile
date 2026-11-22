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
# Startup options come from the environment (see the ENV defaults below) and
# additional command line flags can be appended, e.g.:
#   docker run -d -e RETENTACLE_PORT=9000 -p 9000:9000 ... retentacle --discover
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

# Bind address, port and data folder of the server; override with -e.
ENV RETENTACLE_HOST=0.0.0.0 \
    RETENTACLE_PORT=8080 \
    RETENTACLE_DATA_DIR=/data

# Pre-create the data folder from the default in RETENTACLE_DATA_DIR, so an
# empty named volume mounted there inherits ownership for the service user.
# Note: the VOLUME instruction below cannot use environment variables — it
# always refers to the default path; a custom RETENTACLE_DATA_DIR must be
# mounted or otherwise writable by UID 10001 at runtime.
RUN mkdir -p "$RETENTACLE_DATA_DIR" \
    && chown -R retentacle:retentacle "$RETENTACLE_DATA_DIR" /app

COPY docker/docker-entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

USER retentacle

VOLUME /data
EXPOSE 8080

# Uses the public /health endpoint on the configured port.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('RETENTACLE_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5).getcode() == 200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
