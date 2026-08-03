#!/bin/sh
# Run ON the robot. Full stock OTA path including pre-update cloud backup.
#
# This is what Be Settings → Install does via scheduler.otaDownloadAndInstall:
#   check updates → POST /system/backup → download → apply (reboot)
#
# Previous versions of this script skipped backup (PUT/POST /update only), which
# is NOT what an unmodded robot runs and is useless for validating that path.
#
# Prerequisites (see diagnose-ota.sh / prep-robot-ota.sh):
#   - credentials.endpoint + patched jibo-system-backup
#   - plugged in, Wi-Fi, google.com reachable
#   - ~1.2G free on /opt
#
# Usage: OTA_FILTER=fcs sh apply-available-ota.sh
#
# Backup packing can take 10–20+ minutes with NO ota-server traffic. Only after
# /opt/tmp/backup.tar.bz2 exists does jibo-system-backup hit the stub.
set -e
OTA_FILTER="${OTA_FILTER:-fcs}"
SSM="http://127.0.0.1:8585"
TMP="/tmp/ota-updates.json"
# SM backup has no short timeout; packing + encrypt + upload of a real archive
# can exceed half an hour on a full knowledge base.
BACKUP_TIMEOUT="${BACKUP_TIMEOUT:-7200}"
DOWNLOAD_TIMEOUT="${DOWNLOAD_TIMEOUT:-7200}"

echo "=== 1/4 check updates (filter=${OTA_FILTER}) ==="
curl -sS -m 120 "${SSM}/update/${OTA_FILTER}" > "$TMP"
cat "$TMP"
echo

IDS_JSON=$(python - <<PY
import json
data = json.load(open("$TMP"))
if data.get("error"):
    raise SystemExit("System Manager error: %s" % data["error"])
updates = data.get("updates") or []
ids = [u["id"] for u in updates]
print(json.dumps({"ids": ids}))
PY
)

COUNT=$(python - <<PY
import json
print(len(json.load(open("$TMP")).get("updates") or []))
PY
)

if [ "$COUNT" = "0" ]; then
  echo "No updates available — nothing to back up or install."
  exit 0
fi

echo
echo "=== 2/4 POST /system/backup (stock pre-OTA backup; can take a long time) ==="
echo "Watch the laptop for Loop.ListLoops / Backup.New / PUT /backups / Backup.List"
echo "until packing finishes there will be silence on the ota-server."
# -f so a 500 ("Failed to do system backup") aborts instead of downloading.
# --max-time 0 means unlimited on some curl builds; use BACKUP_TIMEOUT.
if ! curl -sS -f -m "$BACKUP_TIMEOUT" -X POST "${SSM}/system/backup"; then
  echo >&2
  echo "FAIL: System Manager backup failed." >&2
  echo "  - confirm patch-system-backup.sh + credentials.endpoint" >&2
  echo "  - check laptop for Backup.* errors / api.jibo.com ENOTFOUND" >&2
  echo "  - ls -lh /opt/tmp/backup.tar.bz2 ; logread | grep -i backup" >&2
  exit 1
fi
echo "backup OK (HTTP 204)"

echo
echo "=== 3/4 download $IDS_JSON ==="
# Streaming JSON progress lines; -N disables buffering so you see percent updates.
curl -sS -N -m "$DOWNLOAD_TIMEOUT" -X PUT -H 'Content-Type: application/json' \
  -d "$IDS_JSON" "${SSM}/update"
echo

echo
echo "=== 4/4 apply (robot will reboot into jibo-apply-update) ==="
curl -sS -m 120 -X POST -H 'Content-Type: application/json' \
  -d "$IDS_JSON" "${SSM}/update"
echo
echo "Done. If apply succeeded the robot should reboot shortly."
