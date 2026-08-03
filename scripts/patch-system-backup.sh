#!/bin/sh
# Run ON the robot. Fixes the stock cloud backup/restore tools so they honor
# credentials.endpoint (homebrew OTA host) instead of https://api.jibo.com.
#
# Stock jibo-system-backup (ADLC /usr/local/bin) does TWO broken things:
#
#   1) jibo.config.update({ region: credentials.region });
#      // drops endpoint — every other robot tool updates the WHOLE credentials
#      // object (jibo-get-update, STS CredentialStore.authorizeJSC, …)
#
#   2) backup = new jibo.Backup({ credentials: credentials });
#      // nests the object. JSC Service.initialize copies AWS.config then
#      // config.update(options). Nested `credentials` becomes the signer only;
#      // endpoint must already be on AWS.config (step 1) OR passed flat like
#      // `new jibo.Loop(credentials)`. We flatten Backup to match Loop/Key.
#
# jibo-system-restore also hardcodes https.get(downloadUrl), which fails
# against a plain-http OTA host — switch on the URL scheme.
#
# Usage (robot ash):
#   sh patch-system-backup.sh
set -e

BACKUP_BIN="${BACKUP_BIN:-/usr/local/bin/jibo-system-backup}"
RESTORE_BIN="${RESTORE_BIN:-/usr/local/bin/jibo-system-restore}"

if command -v jibo-mount >/dev/null 2>&1; then
  jibo-mount --rw >/dev/null 2>&1 || true
fi

patch_one() {
  target="$1"
  if [ ! -f "$target" ]; then
    echo "skip (missing): $target"
    return 0
  fi
  if ! grep -q 'jibo.config.update' "$target"; then
    echo "unexpected file (no jibo.config.update): $target" >&2
    return 1
  fi
  bak="${target}.pre-beam-ota"
  if [ ! -f "$bak" ]; then
    cp -a "$target" "$bak"
    echo "backed up original to $bak"
  fi

  python - "$target" <<'PY'
import re, sys

path = sys.argv[1]
src = open(path).read()
orig = src
notes = []

if 'jibo.config.update(credentials)' not in src:
    pat = re.compile(
        r"jibo\.config\.update\(\s*\{\s*region:\s*credentials\.region\s*\}\s*\)")
    src, n = pat.subn("jibo.config.update(credentials)", src, count=1)
    if n != 1:
        # Broader: any update({ ... region: credentials.region ... })
        pat2 = re.compile(
            r"jibo\.config\.update\(\s*\{[^}]*region:\s*credentials\.region[^}]*\}\s*\)")
        src, n = pat2.subn("jibo.config.update(credentials)", src, count=1)
    if n != 1:
        sys.stderr.write("patch failed: no region-only config.update in %s\n" % path)
        sys.exit(1)
    notes.append("config.update({region}) -> config.update(credentials)")

# Flatten Backup ctor so endpoint lives on the service config (same as Loop/Key).
# Accept whitespace / single-line forms from stock and minified builds.
nested = re.compile(
    r"new\s+jibo\.Backup\(\s*\{\s*credentials\s*:\s*credentials\s*\}\s*\)"
)
src2, n = nested.subn("new jibo.Backup(credentials)", src)
if n:
    src = src2
    notes.append("new Backup({credentials}) -> new Backup(credentials)")

# Restore downloads location.url with the https module regardless of scheme.
if 'https.get(downloadUrl' in src:
    src = src.replace(
        "https.get(downloadUrl",
        "require(downloadUrl.indexOf('https:') === 0 ? 'https' : 'http')"
        ".get(downloadUrl",
        1,
    )
    notes.append("https.get(downloadUrl) -> scheme-aware get")

if src == orig:
    print("already patched: %s" % path)
else:
    open(path, "w").write(src)
    print("patched %s" % path)
    for note in notes:
        print("  %s" % note)
PY

  chmod +x "$target" 2>/dev/null || true
}

patch_one "$BACKUP_BIN"
patch_one "$RESTORE_BIN"

echo "Done. System Manager backup/restore now use credentials.endpoint."
echo "Test the upload path with:"
echo "  $BACKUP_BIN --credentials /var/jibo/credentials.json \\"
echo "    --keydir /var/jibo/keys --filename /opt/tmp/backup.tar.bz2 --encrypt"
