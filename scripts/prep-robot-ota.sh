#!/bin/sh
# Run ON the robot (busybox ash). One-shot prep so stock OTA can backup then
# download from your homebrew ota-server.
#
# What stock ADLC needs (verified against PlatformTeam/system-manager +
# /usr/local/bin/jibo-system-backup + Be Settings → Install):
#   1. credentials.endpoint -> http://<host>:8042  (point-ota.sh)
#   2. jibo-system-backup/restore patched to honor that endpoint
#   3. otaFilter matching the server manifest (fcs/eau)
#   4. Robot plugged in + Wi-Fi with real internet (SSM verifies google.com)
#   5. /opt free ~>= 1.2GB for the BEam packages
#
# Usage:
#   OTA_HOST=192.168.7.105 OTA_PORT=8042 OTA_FILTER=fcs sh prep-robot-ota.sh
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo .)
OTA_HOST="${OTA_HOST:-192.168.7.105}"
OTA_PORT="${OTA_PORT:-8042}"
OTA_FILTER="${OTA_FILTER:-fcs}"

export OTA_HOST OTA_PORT OTA_FILTER

echo "=== 1/3 point credentials + otaFilter at ${OTA_HOST}:${OTA_PORT} ==="
sh "$SCRIPT_DIR/point-ota.sh"

echo
echo "=== 2/3 patch jibo-system-backup / jibo-system-restore ==="
sh "$SCRIPT_DIR/patch-system-backup.sh"

echo
echo "=== 3/3 preflight ==="
sh "$SCRIPT_DIR/check-ota.sh"

echo
echo "Next:"
echo "  1. Laptop:  cd ~/jsih/ota-server && PYTHONUNBUFFERED=1 python3 server.py"
echo "  2. Robot:   OTA_FILTER=${OTA_FILTER} sh $SCRIPT_DIR/diagnose-ota.sh"
echo "  3. Start the REAL install path (includes System Manager backup):"
echo "       OTA_FILTER=${OTA_FILTER} sh $SCRIPT_DIR/apply-available-ota.sh"
echo "     Or: Settings → Install (not just Check)."
echo "     Backup packing is silent on the laptop for 10–20+ min, then:"
echo "       Loop.ListLoops, Backup.New, PUT /backups/..., Backup.List, GET /packages/"
