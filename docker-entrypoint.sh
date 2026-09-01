#!/bin/sh
set -eu

mkdir -p /data
PUID="${PUID:-10001}"
PGID="${PGID:-10001}"
chown -R "${PUID}:${PGID}" /data
exec gosu "${PUID}:${PGID}" "$@"
