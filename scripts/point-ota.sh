#!/bin/sh
# Run ON the robot. Points classic OTA (credentials) at your update server
# and sets the oobe-config OTA filter so System Manager can discover packages.
#
# Usage (robot ash):
#   OTA_HOST=192.168.7.105 OTA_PORT=8042 OTA_FILTER=fcs sh point-ota.sh
set -e

OTA_HOST="${OTA_HOST:-192.168.7.105}"
OTA_PORT="${OTA_PORT:-8042}"
OTA_FILTER="${OTA_FILTER:-fcs}"
CREDS="/var/jibo/credentials.json"
OOBE_CFG="/opt/jibo/Jibo/Skills/oobe-config/config.json"

ENDPOINT="http://${OTA_HOST}:${OTA_PORT}"

echo "Pointing OTA at ${ENDPOINT} (filter=${OTA_FILTER})"

# Prefer rw remount if available
if command -v jibo-mount >/dev/null 2>&1; then
  jibo-mount --rw >/dev/null 2>&1 || true
fi

# Preserve existing keys if present; otherwise create placeholder keys.
# Server auth is optional (require_auth:false); keys still required by jibo-get-update.
python - <<PY
import json, os
creds_path = "${CREDS}"
endpoint = "${ENDPOINT}"
ak = "local"
sk = "local"
region = "api"
if os.path.isfile(creds_path):
    try:
        old = json.load(open(creds_path))
        ak = old.get("accessKeyId") or ak
        sk = old.get("secretAccessKey") or sk
        region = old.get("region") or region
    except Exception:
        pass
data = {
    "accessKeyId": ak,
    "secretAccessKey": sk,
    "region": region,
    "endpoint": endpoint,
}
open(creds_path, "w").write(json.dumps(data, separators=(",", ":")))
print("wrote", creds_path)
PY

if [ -f "$OOBE_CFG" ]; then
  python - <<PY
import json
path = "${OOBE_CFG}"
filt = "${OTA_FILTER}"
data = json.load(open(path))
data["otaFilter"] = filt
# keep serverRegion; classic OTA uses credentials.endpoint override
json.dump(data, open(path, "w"), indent=2)
print("set otaFilter=%s in %s" % (filt, path))
PY
else
  echo "warning: missing $OOBE_CFG (filter not updated)"
fi

echo "Done. Trigger a check with:"
echo "  curl -s http://127.0.0.1:8585/update/${OTA_FILTER}"
echo "Or use Settings / oobe-config update UI (native path)."
