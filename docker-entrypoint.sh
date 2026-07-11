#!/bin/sh
set -e

# Ensure the data directory exists and is writable by appuser.
# The ./app:/app bind mount shadows the image-layer mkdir+chown, so we
# re-apply ownership here while we still have root.
mkdir -p /app/data
chown appuser:appuser /app/data

# Drop privileges and exec the application as appuser
exec gosu appuser "$@"
