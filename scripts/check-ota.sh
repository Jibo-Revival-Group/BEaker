#!/bin/sh
# Run ON the robot. Preflight for the homebrew OTA path, then ask System Manager
# for available updates (the same GET /update/<filter> settings/oobe-config use).
#
# Usage: OTA_FILTER=fcs sh check-ota.sh
set -e
OTA_FILTER="${OTA_FILTER:-fcs}"
CREDS="${CREDS:-/var/jibo/credentials.json}"
BACKUP_BIN="${BACKUP_BIN:-/usr/local/bin/jibo-system-backup}"
RESTORE_BIN="${RESTORE_BIN:-/usr/local/bin/jibo-system-restore}"

# System Manager's CredentialsManager only knows accessKeyId/secretAccessKey/
# region, so anything that POSTs /_M_/credentials rewrites this file and drops
# the endpoint override. Check it every time before blaming the server.
if grep -q '"endpoint"' "$CREDS" 2>/dev/null; then
  echo "endpoint: $(sed -n 's/.*"endpoint":"\([^"]*\)".*/\1/p' "$CREDS")"
else
  echo "WARNING: no endpoint in $CREDS — re-run point-ota.sh" >&2
fi

for bin in "$BACKUP_BIN" "$RESTORE_BIN"; do
  if [ ! -f "$bin" ]; then
    echo "WARNING: missing $bin" >&2
  elif grep -q 'jibo.config.update(credentials)' "$bin" 2>/dev/null \
      && ! grep -q 'new jibo.Backup({ *credentials' "$bin" 2>/dev/null; then
    echo "patched:  $bin"
  else
    echo "WARNING: $bin still targets api.jibo.com — run patch-system-backup.sh" >&2
  fi
done

# OTA also needs real internet: OTAUpdater._isReady verifies the connection
# (google.com) and that Jibo is plugged in before it will back up or download.
echo "GET http://127.0.0.1:8585/update/${OTA_FILTER}"
curl -sS "http://127.0.0.1:8585/update/${OTA_FILTER}"
echo
