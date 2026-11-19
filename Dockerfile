# Retentacle — backup server for Shelly devices.
#
# Builds the Python package into a slim image. All state (backup database,
# settings, users) lives in /data, which should be backed by a volume.
#
#   docker build -t retentacle .
#   docker run -d -p 8080:8080 -v retentacle-data:/data retentacle
#
# Additional command line flags can be passed by overriding the CMD, e.g.:
#   docker run ... retentacle --discover
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only the packaging inputs and the package itself are copied, so dependency
# installation stays cached unless those files change.
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY retentacle ./retentacle

RUN useradd --create-home --uid 10001 retentacle \
    && pip install . \
    && mkdir -p /data \
    && chown -R retentacle:retentacle /data /app

USER retentacle

VOLUME /data
EXPOSE 8080

# Uses the public /health endpoint; assumes the default port 8080.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).getcode() == 200 else 1)"

ENTRYPOINT ["retentacle"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--target-folder", "/data"]
