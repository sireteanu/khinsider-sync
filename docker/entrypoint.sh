#!/bin/sh
# Runs Khinsider Sync as PUID:PGID (Unraid: 99:100 = nobody:users) so the files it writes in
# /data and /config belong to the same user as the rest of your shares.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
CONFIG="${KHINSIDER_CONFIG:-/config}"
DATA="${KHINSIDER_DATA:-/data}"
umask "${UMASK:-000}"
mkdir -p "$CONFIG" "$DATA"

if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" "$CONFIG" 2>/dev/null || true
  # a freshly created, empty download folder gets the right owner; existing shares are left alone
  if [ -z "$(ls -A "$DATA" 2>/dev/null)" ]; then chown "$PUID:$PGID" "$DATA" 2>/dev/null || true; fi
  if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w "$DATA"; then
    echo "WARNING: $DATA is not writable for user $PUID:$PGID - check the share's permissions" >&2
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python -m khinsider_sync "$@"
fi
exec python -m khinsider_sync "$@"
