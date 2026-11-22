#!/bin/sh
# Start Retentacle with values from the environment, keeping any extra
# command line arguments passed to the container in charge (they are
# appended last and override the defaults above).
set -e

exec retentacle \
    --host "${RETENTACLE_HOST:-0.0.0.0}" \
    --port "${RETENTACLE_PORT:-8080}" \
    --target-folder "${RETENTACLE_DATA_DIR:-/data}" \
    "$@"
