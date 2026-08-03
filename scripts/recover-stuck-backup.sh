#!/bin/sh
# Run ON the robot when diagnose-ota.sh shows:
#   - /opt/tmp/backup.tar.bz2 exists
#   - jibo-system-backup is NOT running
#   - curl GET /update/<filter> returns "Empty reply from server"
#
# That means System Manager finished (or nearly finished) local packing but is
# wedged inside POST /system/backup before/during/after the cloud uploader.
# Until that returns, SM's HTTP workers can look dead (empty replies).
#
# This script completes the CLOUD half stock would have run:
#   jibo-system-backup --credentials … --keydir … --filename … --encrypt
# using the archive SM already built. Then it re-checks whether /update works.
#
# Usage: sh recover-stuck-backup.sh
set -e

CREDS="${CREDS:-/var/jibo/credentials.json}"
KEYDIR="${KEYDIR:-/var/jibo/keys}"
BACKUP_BIN="${BACKUP_BIN:-/usr/local/bin/jibo-system-backup}"
FILE="${FILE:-/opt/tmp/backup.tar.bz2}"
SSM="http://127.0.0.1:8585"
OTA_FILTER="${OTA_FILTER:-fcs}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo .)

if [ ! -f "$FILE" ]; then
  echo "No $FILE — nothing to recover. Is packing still running?" >&2
  ls -lh /opt/tmp 2>/dev/null || true
  exit 1
fi

if ps w 2>/dev/null | grep -v grep | grep -q '[j]ibo-system-backup'; then
  echo "jibo-system-backup is already running — wait for it (watch the laptop)."
  ps w | grep '[j]ibo-system-backup' || true
  exit 0
fi

if [ ! -f "$CREDS" ] || ! grep -q '"endpoint"' "$CREDS"; then
  echo "missing credentials.endpoint — run point-ota.sh first" >&2
  exit 1
fi

if ! grep -q 'jibo.config.update(credentials)' "$BACKUP_BIN" 2>/dev/null \
    || grep -q 'new jibo.Backup({ *credentials' "$BACKUP_BIN" 2>/dev/null; then
  echo "patching $BACKUP_BIN first..."
  sh "$SCRIPT_DIR/patch-system-backup.sh"
fi

echo "Uploading existing SM archive: $(ls -lh "$FILE" | awk '{print $5}')"
echo "Laptop should show: Loop.ListLoops, Backup.New, PUT /backups/..., Backup.List"
OUT="$("$BACKUP_BIN" --credentials "$CREDS" --keydir "$KEYDIR" --filename "$FILE" --encrypt)"
echo "cloud backup etag=$OUT"

echo
echo "Re-checking System Manager /update/${OTA_FILTER} ..."
# Give SM a moment if it was blocked waiting on the (now finished) uploader.
sleep 2
if curl -sS -m 30 "${SSM}/update/${OTA_FILTER}" > /tmp/ota-updates-recover.json; then
  echo "SM is responding again:"
  cat /tmp/ota-updates-recover.json
  echo
  echo "Next: download + apply (backup already done):"
  echo "  # skip the backup step — SM may still be wedged on /system/backup"
  echo "  python -c \"import json;d=json.load(open('/tmp/ota-updates-recover.json')); print(json.dumps({'ids':[u['id'] for u in d.get('updates',[])]}))\"" 
  echo "  curl -sS -N -X PUT -H 'Content-Type: application/json' -d '<ids json>' http://127.0.0.1:8585/update"
  echo "  curl -sS -X POST -H 'Content-Type: application/json' -d '<ids json>' http://127.0.0.1:8585/update"
  echo
  echo "If /system/backup is still hung from Settings Install, leave it alone —"
  echo "or after downloads finish, a gentle SM restart may be needed:"
  echo "  # LAST RESORT (will disrupt running skills):"
  echo "  # /etc/init.d/S78jibo-system-manager restart"
else
  echo "SM /update STILL broken (empty reply / timeout)."
  echo "Cloud backup is done, but SM is wedged. Try:"
  echo "  logread | grep -iE 'backup|Update|system-manager' | tail -40"
  echo "  # LAST RESORT — restarts SM (robot goes dark briefly):"
  echo "  # /etc/init.d/S78jibo-system-manager restart"
  echo "Then: OTA_FILTER=${OTA_FILTER} sh apply-available-ota.sh"
  exit 1
fi
