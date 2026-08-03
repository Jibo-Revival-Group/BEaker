#!/bin/sh
# Run ON the robot. Diagnose why OTA stalls after GetUpdateFrom with no
# Backup.* / package download on the ota-server.
#
# Stock Be Settings:
#   Check  -> scheduler.otaCheckUpdates  -> GetUpdateFrom only  (what you see)
#   Install -> scheduler.otaDownloadAndInstall
#           -> _isReady (plugged + Wi-Fi + google.com)
#           -> POST /system/backup  (pack /opt/tmp, then jibo-system-backup)
#           -> PUT  /update  (download packages)
#           -> POST /update  (apply + reboot)
#
# If the laptop only ever sees GetUpdateFrom + Log/Key, Install never started
# or died in _isReady before any Backup HTTP.
#
# Usage: OTA_FILTER=fcs sh diagnose-ota.sh
set -e
OTA_FILTER="${OTA_FILTER:-fcs}"
CREDS="${CREDS:-/var/jibo/credentials.json}"
BACKUP_BIN="${BACKUP_BIN:-/usr/local/bin/jibo-system-backup}"
SSM="http://127.0.0.1:8585"

echo "=== credentials ==="
if [ -f "$CREDS" ]; then
  cat "$CREDS"
  echo
  if grep -q '"endpoint"' "$CREDS"; then
    echo "OK: endpoint present"
  else
    echo "FAIL: no endpoint — re-run point-ota.sh / prep-robot-ota.sh"
  fi
else
  echo "FAIL: missing $CREDS"
fi

echo
echo "=== jibo-system-backup patch ==="
if [ ! -f "$BACKUP_BIN" ]; then
  echo "FAIL: missing $BACKUP_BIN"
elif grep -q 'jibo.config.update(credentials)' "$BACKUP_BIN" \
    && ! grep -q 'new jibo.Backup({ *credentials' "$BACKUP_BIN"; then
  echo "OK: patched $BACKUP_BIN"
else
  echo "FAIL: $BACKUP_BIN still targets api.jibo.com — run patch-system-backup.sh"
fi

echo
echo "=== /opt free space (need ~1.2G for BEam download+extract) ==="
df -h /opt 2>/dev/null || df -h /

echo
echo "=== Wi-Fi / internet (_isReady gates Install) ==="
if [ -r /sys/class/net/wlan0/operstate ]; then
  echo "wlan0: $(cat /sys/class/net/wlan0/operstate)"
else
  echo "wlan0: (no sysfs)"
fi
# Stock OTAUpdater.verifyConnection hits google.com; no route => Install aborts
# before backup with no traffic to the ota-server.
if command -v wget >/dev/null 2>&1; then
  if wget -q -T 5 -O /dev/null http://www.google.com/generate_204 2>/dev/null \
      || wget -q -T 5 -O /dev/null https://www.google.com/generate_204 2>/dev/null; then
    echo "OK: google.com reachable"
  else
    echo "FAIL: cannot reach google.com — Settings Install will refuse (_isReady)"
  fi
elif command -v curl >/dev/null 2>&1; then
  if curl -sS -m 5 -o /dev/null -w "google HTTP %{http_code}\n" http://www.google.com/generate_204; then
    :
  else
    echo "FAIL: cannot reach google.com — Settings Install will refuse (_isReady)"
  fi
fi

echo
echo "=== plug / power (Install requires AC) ==="
# Best-effort: body service exposes power over WS; sysfs varies by build.
for p in /sys/class/power_supply/*/online /sys/class/power_supply/*/status; do
  [ -r "$p" ] && echo "$p: $(cat "$p")"
done
if ! ls /sys/class/power_supply/*/online >/dev/null 2>&1; then
  echo "(no power_supply sysfs — confirm Jibo is plugged in visually)"
fi

echo
echo "=== local backup artifacts (packing in progress?) ==="
ls -lh /opt/tmp/backup.tar.bz2 /opt/tmp/backup 2>/dev/null || echo "(none yet)"
ps w 2>/dev/null | grep -E 'jibo-system-backup|jibo-system-manager' | grep -v grep || true

echo
echo "=== System Manager update list (filter=${OTA_FILTER}) ==="
if curl -sS -m 30 "${SSM}/update/${OTA_FILTER}"; then
  echo
else
  echo "FAIL: SM /update unreachable or empty reply"
  echo "*** SM is often wedged inside POST /system/backup after packing."
  if [ -f /opt/tmp/backup.tar.bz2 ] && ! ps w 2>/dev/null | grep -v grep | grep -q '[j]ibo-system-backup'; then
    echo "*** Archive exists, uploader not running → sh recover-stuck-backup.sh"
  fi
fi
echo

echo
echo "=== how to start the REAL install path (includes backup) ==="
echo "  Settings → Install   OR"
echo "  OTA_FILTER=${OTA_FILTER} sh apply-available-ota.sh"
echo "If packing finished but SM is wedged:"
echo "  sh recover-stuck-backup.sh"
echo "During backup expect long silence on the laptop, then:"
echo "  Loop.ListLoops, Backup.New, PUT /backups/..., Backup.List, GET /packages/..."
