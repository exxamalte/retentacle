#!/bin/sh
# Start Retentacle with values from the environment.
#
# When the container is started as root (the default), the data folder is
# made writable by PUID/PGID — this fixes fresh bind mounts, which Docker
# creates root-owned — and the server is then dropped to that unprivileged
# user. Extra command line arguments keep working and are appended last,
# so they override the generated flags.
set -e

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$RETENTACLE_DATA_DIR"
    chown -R "$PUID:$PGID" "$RETENTACLE_DATA_DIR" 2>/dev/null \
        || echo "warning: unable to adjust ownership of $RETENTACLE_DATA_DIR (read-only or NFS mount?)"
    exec python3 -c 'import os, sys; os.setgroups([]); os.setgid(int(os.environ["PGID"])); os.setuid(int(os.environ["PUID"])); os.execvp("retentacle", ["retentacle"] + sys.argv[1:])' \
        --host "${RETENTACLE_HOST:-0.0.0.0}" \
        --port "${RETENTACLE_PORT:-8080}" \
        --target-folder "$RETENTACLE_DATA_DIR" \
        "$@"
fi

# Already running as a non-root user (e.g. an explicit `user:` setting):
exec retentacle \
    --host "${RETENTACLE_HOST:-0.0.0.0}" \
    --port "${RETENTACLE_PORT:-8080}" \
    --target-folder "$RETENTACLE_DATA_DIR" \
    "$@"
