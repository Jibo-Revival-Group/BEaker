#!/bin/sh
# Run ON the robot. Bypasses the slow ServiceManager local pack and runs the
# stock cloud uploader (jibo-system-backup) against credentials.endpoint.
#
# This is the same Upload step OTA waits on after /opt/tmp/backup.tar.bz2 exists.
# Use it to prove Backup.New → PUT → Backup.List against your OTA host.
#
# Usage (robot ash, one line):
#   sh force-cloud-backup.sh
#
# Requires patch-system-backup.sh first (stock tool ignores credentials.endpoint).
set -e

CREDS="${CREDS:-/var/jibo/credentials.json}"
KEYDIR="${KEYDIR:-/var/jibo/keys}"
BACKUP_BIN="${BACKUP_BIN:-/usr/local/bin/jibo-system-backup}"
TMPDIR="${TMPDIR:-/opt/tmp}"
FILE="${TMPDIR}/force-backup.tar.bz2"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo .)

if [ ! -f "$CREDS" ]; then
  echo "missing $CREDS — run point-ota.sh first" >&2
  exit 1
fi
if [ ! -x "$BACKUP_BIN" ]; then
  echo "missing $BACKUP_BIN" >&2
  exit 1
fi

# Ensure Backup client uses credentials.endpoint (not api.jibo.com).
if [ -f "$SCRIPT_DIR/patch-system-backup.sh" ]; then
  sh "$SCRIPT_DIR/patch-system-backup.sh"
elif ! grep -q 'jibo.config.update(credentials)' "$BACKUP_BIN" 2>/dev/null; then
  echo "run patch-system-backup.sh first (Backup still points at api.jibo.com)" >&2
  exit 1
fi

mkdir -p "$TMPDIR" "$KEYDIR"

# Prefer the archive ServiceManager already packed: Backup.List only reports the
# newest upload, so a placeholder would shadow the robot's real backup.
if [ -f "${TMPDIR}/backup.tar.bz2" ]; then
  FILE="${TMPDIR}/backup.tar.bz2"
  echo "using existing System Manager archive $FILE"
else
  WORKDIR="${TMPDIR}/force-backup-src"
  rm -rf "$WORKDIR"
  mkdir -p "$WORKDIR"
  echo "beam-ota-force-backup $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$WORKDIR/README"
  # Stock uploader expects a .tar.bz2; keep it tiny so encrypt fits in RAM.
  tar -cjf "$FILE" -C "$WORKDIR" .
  rm -rf "$WORKDIR"
  echo "NOTE: uploading a placeholder archive; it becomes the newest cloud"
  echo "      backup for this robot until the next real System Manager backup."
fi

echo "Uploading $FILE via $BACKUP_BIN ..."
# --encrypt matches ServiceManager::backup argv.
OUT="$("$BACKUP_BIN" --credentials "$CREDS" --keydir "$KEYDIR" --filename "$FILE" --encrypt)"
echo "etag=$OUT"
echo "Cloud backup OK. Re-run Settings → Check for updates / Install."
