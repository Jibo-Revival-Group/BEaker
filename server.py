#!/usr/bin/env python3
"""
Minimal Jibo Update API server for local OTA.

Speaks enough of the classic Update JSON protocol for jibo-get-update /
jibo-download-update, plus the Backup/Loop bits jibo-system-backup needs
when credentials.endpoint points here:

  POST /  X-Amz-Target: Update_20160301.GetUpdateFrom
  POST /  X-Amz-Target: Loop_20160324.ListLoops
  POST /  X-Amz-Target: Backup_20170222.New | Backup_20170222.List
  PUT  /backups/<id>   (upload body from Backup.New uploadUrl)
  GET  /packages/<file>

Point the robot at this host via /var/jibo/credentials.json:

  {
    "accessKeyId": "...",
    "secretAccessKey": "...",
    "region": "api",
    "endpoint": "http://<this-host>:8042"
  }
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
MANIFEST_PATH = ROOT / "updates" / "manifest.json"
PACKAGES_DIR = ROOT / "updates" / "packages"
BACKUPS_DIR = ROOT / "updates" / "backups"
ROBOTS_DIR = ROOT / "updates" / "robots"
MEDIA_DIR = ROOT / "updates" / "media"
KEYS_DIR = ROOT / "updates" / "keys"
# Same trick as srv-backup-ws: lexicographic latest-first object names.
_BACKUP_MAGIC = 9999999999999
STREAM_CHUNK = 1024 * 1024
RELOAD_NETWORK = ipaddress.ip_network("192.168.0.0/16")
RELOAD_DENY = ipaddress.ip_address("192.168.7.55")

# SigV4 credential scope service names, taken from endpointPrefix in the
# robot's own jibo-server-client/apis/*.min.json. Loop really is capitalized.
SERVICE_BY_TARGET = {
    "Account_": "account",
    "Backup_": "backup",
    "Collision_": "collision",
    "GQA_": "gqa",
    "IFTTT_": "ifttt",
    "Key_": "key",
    "Log_": "log",
    "Loop_": "Loop",
    "Lps_": "lps",
    "Media_": "media",
    "NLP_": "nlp",
    "Notification_": "notification",
    "OOBE_": "oobe",
    "Person_": "person",
    "Push_": "push",
    "ROM_": "rom",
    "Robot_": "robot",
    "Settings_": "settings",
    "Update_": "update",
}


def target_service(target: str) -> str | None:
    """Map X-Amz-Target (Backup_20170222.New) to its SigV4 service name."""
    if not target:
        return None
    return SERVICE_BY_TARGET.get(target.split("_", 1)[0] + "_")


def log_line(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "unknown"


def reload_allowed(peer_ip: str) -> bool:
    """Allow LAN reloads except the tunnel peer."""
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return address in RELOAD_NETWORK and address != RELOAD_DENY


def extract_access_key(headers: dict[str, str]) -> str | None:
    """Pull the robot account id from SigV4 Authorization Credential=..."""
    auth = headers.get("authorization", "")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        return None
    for piece in auth[len("AWS4-HMAC-SHA256 ") :].split(","):
        piece = piece.strip()
        if piece.startswith("Credential="):
            cred = piece.split("=", 1)[1].strip()
            return cred.split("/", 1)[0] or None
    return None


def normalize_filters(update_filter: Any) -> list[str]:
    """Accept a string, comma-separated string, or list of OTA filter flags."""
    if update_filter is None:
        return [""]
    if isinstance(update_filter, (list, tuple)):
        parts = [str(x).strip() for x in update_filter]
        return [p for p in parts if p] or [""]
    text = str(update_filter).strip()
    if not text:
        return [""]
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        return [p for p in parts if p] or [""]
    return [text]


def filter_matches(update_filter: Any, request_filter: str) -> bool:
    """Prefix match like update-ws; empty request filter matches all.

    Manifest filters may be a single flag (eau), several flags (eau,fcs),
    or a JSON list. A request for fcs matches an update that lists fcs.
    """
    if not request_filter:
        return True
    return any(f.startswith(request_filter) for f in normalize_filters(update_filter))


def public_update(record: dict[str, Any]) -> dict[str, Any]:
    """Drop internal keys; keep Mongo-style `_id`."""
    return {k: v for k, v in record.items() if k == "_id" or not k.startswith("_")}


class RobotRegistry:
    """Per-robot identity learned from the robot's own signed requests."""

    def __init__(self) -> None:
        ROBOTS_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, robot_id: str) -> Path:
        return ROBOTS_DIR / f"{safe_id(robot_id)}.json"

    def get(self, robot_id: str) -> dict[str, Any] | None:
        path = self._path(robot_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _synthetic_loop_id(robot_id: str) -> str:
        # Same shape as a real Jibo loopId (24 hex chars) and stable per account,
        # so it survives wiping updates/robots/.
        return hashlib.sha1(robot_id.encode("utf-8")).hexdigest()[:24]

    def touch(self, robot_id: str, **fields: Any) -> dict[str, Any]:
        now = int(time.time() * 1000)
        data = self.get(robot_id) or {
            "robotId": robot_id,
            "loopId": self._synthetic_loop_id(robot_id),
            "loopIdPinned": False,
            "firstSeen": now,
        }
        # loopId names the robot's local symmetric key (keys/symmetric-<loopId>),
        # so it must never move once anything on the robot has used it: STS and
        # jibo-system-backup both take it from our Loop.ListLoops answer. Adopt a
        # robot-supplied id once (it may have one cached in KB), then freeze.
        loop_id = fields.pop("loopId", None)
        if loop_id and str(loop_id) != data.get("loopId"):
            if data.get("loopIdPinned"):
                log_line(
                    f"keeping pinned loopId={data['loopId']!r} for robot={robot_id!r} "
                    f"(request said {loop_id!r})"
                )
            else:
                log_line(f"robot={robot_id!r} loopId {data['loopId']!r} -> {loop_id!r}")
                data["loopId"] = str(loop_id)
        if loop_id:
            data["loopIdPinned"] = True
        for key, value in fields.items():
            if value is None or value == "":
                continue
            data[key] = value
        data["robotId"] = robot_id
        data["lastSeen"] = now
        if not data.get("loopId"):
            data["loopId"] = self._synthetic_loop_id(robot_id)
        save_json(self._path(robot_id), data)
        return data

    def by_loop(self, loop_id: str) -> dict[str, Any] | None:
        for path in ROBOTS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("loopId") == loop_id:
                return data
        return None


class IncompleteUpload(Exception):
    """Fewer bytes arrived than Content-Length promised."""


class BackupStore:
    """Protocol-compatible backup stand-in that discards uploaded blobs."""

    def __init__(self, config: dict[str, Any], robots: RobotRegistry) -> None:
        self.config = config
        self.robots = robots
        # Keep only metadata during this process so the stock backup client sees
        # a successful upload without creating persistent backup files.
        self._uploads: dict[tuple[str, str], dict[str, Any]] = {}

    def _public(self) -> str:
        return self.config["public_base_url"].rstrip("/")

    def new_upload(self, *, robot_id: str, loop_id: str) -> dict[str, str]:
        rid = safe_id(robot_id)
        name = f"{_BACKUP_MAGIC - int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        # loopId query param survives into PUT meta even if the robot registry
        # was wiped between Backup.New and the upload (jibo-system-backup
        # never sends loopId on the PUT itself).
        q = urlencode({"loopId": str(loop_id)})
        url = f"{self._public()}/backups/{rid}/{name}?{q}"
        log_line(f"Backup.New robot={rid!r} loopId={loop_id!r} -> {url}")
        return {"uploadUrl": url}

    def save_upload(
        self,
        robot_id: str,
        name: str,
        body: bytes | None = None,
        *,
        stream: Any | None = None,
        length: int | None = None,
        loop_id: str | None = None,
    ) -> str:
        """Consume and checksum a backup blob without storing its contents."""
        if "/" in name or name.startswith(".") or not name:
            raise ValueError("bad backup name")
        if "/" in robot_id or robot_id.startswith(".") or not robot_id:
            raise ValueError("bad robot id")
        md5 = hashlib.md5()
        size = 0
        if stream is not None and length is not None:
            remaining = max(0, int(length))
            while remaining > 0:
                chunk = stream.read(min(STREAM_CHUNK, remaining))
                if not chunk:
                    break
                md5.update(chunk)
                size += len(chunk)
                remaining -= len(chunk)
        else:
            data = body or b""
            md5.update(data)
            size = len(data)
        if length is not None and size != int(length):
            raise IncompleteUpload(
                f"got {size} of {length} bytes for {robot_id}/{name}"
            )
        quoted = f'"{md5.hexdigest()}"'
        profile = self.robots.get(robot_id) or {}
        meta = {
            "name": name,
            "robotId": robot_id,
            "loopId": loop_id or profile.get("loopId"),
            "serial": profile.get("serial"),
            "friendlyId": profile.get("friendlyId"),
            "etag": quoted,
            "size": size,
            "modified": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        self._uploads[(robot_id, name)] = meta
        log_line(
            f"Backup PUT robot={robot_id!r} name={name} bytes={size} "
            f"etag={quoted} (discarded)"
        )
        return quoted

    def list_for_robot(self, *, robot_id: str, loop_id: str) -> list[dict[str, Any]]:
        """Newest backup for this loop, as a one-element list.

        jibo-system-backup and jibo-system-restore both do
        `response.reduce((a, b) => Math.max(new Date(a.modified), ...))`, which
        returns a number as soon as there are two entries and then blows up on
        `entry.etag`. Stock keeps one backup per loop, so return only the newest;
        entries from another loopId are skipped because they were encrypted with
        a different symmetric key.
        """
        all_for_robot = [
            meta for (stored_robot, _), meta in self._uploads.items()
            if stored_robot == robot_id
        ]
        candidates = [
            meta
            for meta in all_for_robot
            if not meta.get("loopId") or str(meta["loopId"]) == str(loop_id)
        ]
        skipped = len(all_for_robot) - len(candidates)
        items: list[dict[str, Any]] = []
        if candidates:
            # ISO-8601 UTC strings sort chronologically; newest wins.
            meta = max(candidates, key=lambda item: str(item.get("modified") or ""))
            name = meta.get("name")
            items.append(
                {
                    "modified": meta.get("modified"),
                    "etag": meta.get("etag"),
                    "size": str(meta.get("size", 0)),
                    "location": {
                        "expires": str(int(time.time() * 1000) + 24 * 60 * 60 * 1000),
                        "url": f"{self._public()}/backups/{safe_id(robot_id)}/{name}",
                    },
                }
            )
        log_line(
            f"Backup.List robot={safe_id(robot_id)!r} loopId={loop_id!r} "
            f"count={len(items)} otherLoop={skipped}"
        )
        return items

    def locate(self, robot_id: str, name: str) -> tuple[Path, dict[str, Any]] | None:
        # No-op uploads have no downloadable backup payload.
        return None


class KeyStore:
    """Minimal Key_20160201 stubs for STS / hasKeyBackup / backup encrypt path."""

    def __init__(self) -> None:
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        (KEYS_DIR / "requests").mkdir(parents=True, exist_ok=True)
        (KEYS_DIR / "backups").mkdir(parents=True, exist_ok=True)

    def create_request(
        self, *, robot_id: str, loop_id: str, public_key: str
    ) -> dict[str, Any]:
        req_id = uuid.uuid4().hex
        rec = {
            "id": req_id,
            "accountId": robot_id,
            "loopId": loop_id,
            "publicKey": public_key,
        }
        save_json(KEYS_DIR / "requests" / f"{req_id}.json", rec)
        return rec

    def get_request(self, req_id: str) -> dict[str, Any] | None:
        path = KEYS_DIR / "requests" / f"{safe_id(req_id)}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def share(self, req_id: str, encrypted_key: str) -> dict[str, Any] | None:
        rec = self.get_request(req_id)
        if not rec:
            return None
        rec["encryptedKey"] = encrypted_key
        save_json(KEYS_DIR / "requests" / f"{safe_id(req_id)}.json", rec)
        return rec

    def list_incoming(self, loop_id: str) -> list[dict[str, Any]]:
        out = []
        for path in (KEYS_DIR / "requests").glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if rec.get("loopId") == loop_id and not rec.get("encryptedKey"):
                out.append(rec)
        return out

    def backup_key(
        self,
        *,
        robot_id: str,
        loop_id: str,
        encrypted_key: str,
        password_hash: str | None,
    ) -> dict[str, Any]:
        rec = {
            "loopId": loop_id,
            "accountId": robot_id,
            "encryptedKey": encrypted_key,
            "passwordHash": password_hash,
        }
        save_json(KEYS_DIR / "backups" / f"{safe_id(loop_id)}.json", rec)
        return {
            "loopId": loop_id,
            "accountId": robot_id,
            "encryptedKey": encrypted_key,
        }

    def restore_key(self, loop_id: str) -> dict[str, Any] | None:
        path = KEYS_DIR / "backups" / f"{safe_id(loop_id)}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


class MediaStore:
    """Media_20160725 compatibility responses without file storage."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def _public(self) -> str:
        return self.config["public_base_url"].rstrip("/")

    def create(
        self,
        *,
        robot_id: str,
        loop_id: str,
        path: str,
        body: bytes,
        media_type: str | None,
        reference: str | None,
        encrypted: bool,
        meta: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        name = safe_id(path) or uuid.uuid4().hex
        rec = {
            "path": path or name,
            "type": media_type or "application/octet-stream",
            "reference": reference or "",
            "accountId": robot_id,
            "loopId": loop_id,
            "url": f"{self._public()}/media/{safe_id(robot_id)}/{name}",
            "isEncrypted": encrypted,
            "isDeleted": False,
            "created": int(time.time() * 1000),
        }
        if meta:
            rec["meta"] = meta
        return rec

    def list(
        self,
        *,
        robot_id: str,
        loop_ids: list[str],
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def get(self, *, robot_id: str, paths: list[str]) -> list[dict[str, Any]]:
        return []

    def remove(self, *, robot_id: str, paths: list[str]) -> list[dict[str, Any]]:
        return []


def version_key(v: str) -> tuple:
    parts = re.split(r"[^0-9A-Za-z]+", v)
    out = []
    for p in parts:
        if not p:
            continue
        out.append((0, int(p)) if p.isdigit() else (1, p.lower()))
    return tuple(out)


class Catalog:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.reload()

    def reload(self) -> None:
        raw = load_json(MANIFEST_PATH)
        self.updates: list[dict[str, Any]] = []
        for entry in raw:
            pkg_name = entry["package"]
            pkg_path = PACKAGES_DIR / pkg_name
            if not pkg_path.is_file():
                print(f"warning: missing package {pkg_path}", file=sys.stderr)
                continue
            uid = entry.get("_id") or pkg_path.stem
            public = self.config["public_base_url"].rstrip("/")
            record = {
                "_id": uid,
                "created": entry.get("created", int(time.time() * 1000)),
                "accountId": entry.get("accountId", self.config.get("account_id", "local")),
                "fromVersion": entry["fromVersion"],
                "toVersion": entry["toVersion"],
                "changes": entry.get("changes", ""),
                "url": f"{public}/packages/{pkg_name}",
                "shaHash": sha1_file(pkg_path),
                "length": pkg_path.stat().st_size,
                "subsystem": entry["subsystem"],
                # Keep multi-flag filters as a comma string for matching + responses.
                "filter": ",".join(normalize_filters(entry.get("filter", ""))),
                "dependencies": entry.get("dependencies") or {},
                "_package_path": str(pkg_path),
            }
            self.updates.append(record)
        print(f"loaded {len(self.updates)} update(s) from {MANIFEST_PATH}")

    def list_from(
        self, *, subsystem: str, from_version: str, filt: str
    ) -> list[dict[str, Any]]:
        """Offer the latest package for subsystem/filter from any installed version.

        Exact fromVersion rows are preferred when present, but any other
        installed version is also eligible for the highest toVersion. Skip only
        when the robot already reports that latest toVersion.
        """
        matches = [
            u
            for u in self.updates
            if u["subsystem"] == subsystem
            and filter_matches(u.get("filter", ""), filt)
        ]
        if not matches:
            return []
        latest = max(matches, key=lambda u: version_key(u["toVersion"]))["toVersion"]
        if from_version == latest:
            return []
        candidates = [u for u in matches if u["toVersion"] == latest]
        exact = [u for u in candidates if u["fromVersion"] == from_version]
        chosen = exact or candidates
        # System Manager expects fromVersion to be the version currently on disk.
        out: list[dict[str, Any]] = []
        for u in chosen:
            rewritten = dict(u)
            rewritten["fromVersion"] = from_version
            out.append(rewritten)
        out.sort(key=lambda u: version_key(u["toVersion"]), reverse=True)
        return out

    def get_from(
        self, *, subsystem: str, from_version: str, filt: str
    ) -> dict[str, Any] | None:
        updates = self.list_from(
            subsystem=subsystem, from_version=from_version, filt=filt
        )
        if not updates:
            return None
        best = updates[0]["toVersion"]
        candidates = [u for u in updates if u["toVersion"] == best]
        chosen = random.choice(candidates)
        out = public_update(chosen)
        # Echo the robot's request filter when it matched a multi-flag package.
        if filt:
            for flag in normalize_filters(chosen.get("filter", "")):
                if flag.startswith(filt):
                    out["filter"] = flag
                    break
        return out


def hmac_sha256(key: bytes, msg: str | bytes) -> bytes:
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


def sign_string(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_sig_v4(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    accounts: dict[str, str],
    skew_seconds: int = 900,
) -> tuple[bool, str, tuple[str, str] | None]:
    """Check a JSC SigV4 signature.

    The credential scope (region/service) is taken from the request itself: the
    signing key is derived from it, so a wrong scope cannot produce a valid
    signature. Returns (ok, detail, (region, service)).
    """
    auth = headers.get("authorization", "")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        return False, "missing AWS4 authorization", None

    # Credential=AKID/date/region/service/aws4_request, SignedHeaders=..., Signature=...
    parts = {}
    for piece in auth[len("AWS4-HMAC-SHA256 ") :].split(","):
        piece = piece.strip()
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()

    try:
        cred = parts["Credential"]
        signed_headers = parts["SignedHeaders"]
        signature = parts["Signature"]
        access_key, date_stamp, region, service, _term = cred.split("/")
    except (KeyError, ValueError):
        return False, "malformed authorization", None

    scope = (region, service)

    secret = accounts.get(access_key)
    if secret is None:
        return False, "unknown access key", scope

    amz_date = headers.get("x-amz-date", "")
    if not amz_date:
        return False, "missing x-amz-date", scope
    try:
        req_time = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False, "bad x-amz-date", scope
    if abs(time.time() - req_time.timestamp()) > skew_seconds:
        return False, "clock skew too large", scope

    # Canonical request
    hdr_names = [h.strip().lower() for h in signed_headers.split(";")]
    canonical_headers = ""
    for name in hdr_names:
        # Host may include port; header keys are lowercased by our handler.
        value = headers.get(name, "")
        canonical_headers += f"{name}:{value.strip()}\n"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = "\n".join(
        [
            method.upper(),
            path if path.startswith("/") else "/" + path,
            "",  # query string
            canonical_headers,
            ";".join(hdr_names),
            payload_hash,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            f"{date_stamp}/{region}/{service}/aws4_request",
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    k_date = hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    k_signing = hmac_sha256(k_service, "aws4_request")
    expected = sign_string(k_signing, string_to_sign)
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch", scope
    return True, access_key, scope


class Handler(BaseHTTPRequestHandler):
    server_version = "jibo-ota-server/1.0"
    catalog: Catalog
    config: dict[str, Any]
    backups: BackupStore
    robots: RobotRegistry
    keys: KeyStore
    media: MediaStore

    def log_message(self, fmt: str, *args: Any) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write("[%s] %s - %s\n" % (ts, self.address_string(), fmt % args))

    def _headers_lower(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 0:
            return self.rfile.read(length)
        # Chunked / unknown length: drain until EOF (rare for classic JSC).
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    size = int(line.strip().split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    # trailer
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(chunks)
        return b""

    def _caller_robot_id(self) -> str | None:
        return extract_access_key(self._headers_lower())

    def _require_robot(self, *, loop_id: str | None = None) -> dict[str, Any] | None:
        robot_id = self._caller_robot_id()
        if not robot_id:
            self._send_error_json(
                401,
                "AuthFailure",
                "missing robot access key in Authorization Credential=",
            )
            return None
        # Prefer the loopId the robot already knows (KB / STS cache) over our
        # synthetic sha1, so Backup.New/List and Media stay consistent.
        extras: dict[str, Any] = {}
        if loop_id:
            extras["loopId"] = str(loop_id)
        return self.robots.touch(robot_id, **extras)

    def _send_file(
        self,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        extra_headers: dict | None = None,
    ) -> None:
        """Stream a file out; OTA packages and backups are hundreds of MB."""
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(STREAM_CHUNK), b""):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # jibo-download-update aborts on its own 120 s socket timeout.
            log_line(f"client hung up during {path.name}")

    def _send_json(self, status: int, payload: Any, extra_headers: dict | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(
            status,
            {"__type": code, "code": code, "message": message},
            {"x-amzn-errortype": code},
        )

    def _check_auth(self, body: bytes) -> bool:
        if not self.config.get("require_auth", False):
            return True
        headers = self._headers_lower()
        ok, detail, scope = verify_sig_v4(
            method=self.command,
            path=urlparse(self.path).path or "/",
            headers=headers,
            body=body,
            accounts=self.config.get("accounts") or {},
        )
        if not ok:
            self._send_error_json(401, "AuthFailure", detail)
            return False
        # Signed fine; just note when the scope is not the one this target uses,
        # so a new service showing up here is visible in the log.
        target = headers.get("x-amz-target", "")
        want = target_service(target)
        if scope and want and scope[1] != want:
            log_line(f"note: {target} signed as service={scope[1]!r} (models say {want!r})")
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/health"):
            robot_count = len(list(ROBOTS_DIR.glob("*.json"))) if ROBOTS_DIR.is_dir() else 0
            self._send_json(
                200,
                {
                    "ok": True,
                    "updates": len(self.catalog.updates),
                    "robots": robot_count,
                    "public_base_url": self.config["public_base_url"],
                },
            )
            return

        if path == "/reload":
            peer_ip = self.client_address[0]
            if not reload_allowed(peer_ip):
                log_line(f"reload denied for peer={peer_ip!r}")
                self._send_error_json(
                    403,
                    "AccessDenied",
                    "reload is limited to 192.168.0.0/16 except 192.168.7.55",
                )
                return
            # Re-read config so public_base_url (and similar) take effect without
            # a full process restart.
            try:
                fresh = load_json(DEFAULT_CONFIG)
                for key in (
                    "public_base_url",
                    "require_auth",
                    "region",
                    "service",
                    "account_id",
                    "accounts",
                ):
                    if key in fresh:
                        self.config[key] = fresh[key]
            except Exception as exc:
                self._send_json(500, {"error": f"config reload failed: {exc}"})
                return
            self.catalog.reload()
            self._send_json(
                200,
                {
                    "reloaded": True,
                    "updates": len(self.catalog.updates),
                    "public_base_url": self.config["public_base_url"],
                },
            )
            return

        if path.startswith("/packages/"):
            name = path[len("/packages/") :]
            if "/" in name or name.startswith(".") or not name:
                self.send_error(400, "bad package name")
                return
            pkg = PACKAGES_DIR / name
            if not pkg.is_file():
                self.send_error(404, "package not found")
                return
            self._send_file(pkg)
            return

        if path.startswith("/backups/"):
            rest = path[len("/backups/") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) != 2 or any(p.startswith(".") for p in parts):
                self.send_error(400, "bad backup path")
                return
            robot_id, name = parts
            got = self.backups.locate(robot_id, name)
            if not got:
                self.send_error(404, "backup not found")
                return
            data_path, meta = got
            extra = {"ETag": str(meta["etag"])} if meta.get("etag") else None
            self._send_file(data_path, extra_headers=extra)
            return

        if path.startswith("/media/"):
            rest = path[len("/media/") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) != 2 or any(p.startswith(".") for p in parts):
                self.send_error(400, "bad media path")
                return
            robot_id, name = parts
            data_path = MEDIA_DIR / safe_id(robot_id) / safe_id(name)
            meta_path = MEDIA_DIR / safe_id(robot_id) / f"{safe_id(name)}.json"
            if not data_path.is_file():
                self.send_error(404, "media not found")
                return
            ctype = "application/octet-stream"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    # x-type carries Jibo's own labels (thumb/photo), not MIME.
                    if "/" in str(meta.get("type") or ""):
                        ctype = meta["type"]
                except (OSError, json.JSONDecodeError):
                    pass
            self._send_file(data_path, content_type=ctype)
            return

        self.send_error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        # Backup.New returns an uploadUrl; robot PUTs the blob here (unsigned).
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/backups/"):
            self.send_error(404, "not found")
            return
        rest = path[len("/backups/") :]
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 2 or any(p.startswith(".") for p in parts):
            self.send_error(400, "bad backup path")
            return
        robot_id, name = parts
        qs = parse_qs(parsed.query)
        loop_id = (qs.get("loopId") or [None])[0]
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # S3 requires it, and jibo-system-backup always sets it; without it
            # we would happily store an empty "backup".
            self.send_error(411, "length required")
            return
        length = int(raw_length or 0)
        try:
            # Stream to disk — stock encrypted tarballs can be hundreds of MB.
            etag = self.backups.save_upload(
                robot_id,
                name,
                stream=self.rfile,
                length=length,
                loop_id=str(loop_id) if loop_id else None,
            )
        except ValueError:
            self.send_error(400, "bad backup path")
            return
        except IncompleteUpload as exc:
            log_line(f"Backup PUT incomplete robot={robot_id!r} name={name}: {exc}")
            try:
                self.send_error(400, "incomplete body")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # S3 answers a completed PUT with 200 + ETag; jibo-system-backup reads
        # only the ETag header, then verifies it through Backup.List.
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        if not self._check_auth(body):
            return

        headers = self._headers_lower()
        target = headers.get("x-amz-target", "")
        op = target.split(".")[-1] if target else ""
        prefix = target.split(".")[0] if "." in target else ""
        robot_id = self._caller_robot_id()
        clen = headers.get("content-length", "0")
        log_line(
            f"POST target={target or '(none)'} robot={robot_id!r} "
            f"contentLength={clen} bytes_read={len(body)}"
        )

        # Media.Create sends a raw binary payload (not JSON). Handle before json.loads.
        if prefix.startswith("Media_"):
            payload: dict[str, Any] = {}
            if op != "Create":
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
            self._media_op(op, payload, body)
            return

        # Key.ShareBinary is the other streaming-payload op (id in x-id).
        if prefix.startswith("Key_") and op == "ShareBinary":
            self._key_op(op, {"id": headers.get("x-id")}, body)
            return

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_error_json(400, "SerializationException", "invalid JSON")
            return

        # Learn serial / friendly id from whatever the robot sends. loopId is
        # adopted by the individual handlers, which also see the x-loop-id header.
        if robot_id:
            extras: dict[str, Any] = {}
            if payload.get("serial"):
                extras["serial"] = payload["serial"]
            if payload.get("serialNumber"):
                extras["serial"] = payload["serialNumber"]
            if payload.get("friendlyId"):
                extras["friendlyId"] = payload["friendlyId"]
            if payload.get("robotFriendlyId"):
                extras["friendlyId"] = payload["robotFriendlyId"]
            if extras:
                self.robots.touch(robot_id, **extras)

        if prefix.startswith("Backup_"):
            self._backup_op(op, payload)
            return
        if prefix.startswith("Loop_"):
            if op in ("List", "ListLoops"):
                self._loop_list(payload)
                return
            if op == "GetRobot":
                self._loop_get_robot(payload)
                return
        if prefix.startswith("Robot_") and op == "GetRobot":
            self._robot_get(payload)
            return
        if prefix.startswith("Key_"):
            self._key_op(op, payload)
            return
        if prefix.startswith("Notification_"):
            self._notification_op(op, payload)
            return
        if prefix.startswith("Log_") and op in (
            "PutEvents",
            "PutEventsAsync",
            "PutBinary",
            "PutBinaryAsync",
            "PutAsrBinary",
        ):
            log_line(f"Log.{op} robot={robot_id!r} keys={list(payload.keys())}")
            self._send_json(200, {})
            return
        if op == "GetUpdateFrom":
            self._get_update_from(payload)
        elif op == "ListUpdatesFrom":
            self._list_updates_from(payload)
        elif op == "ListUpdates":
            self._list_updates(payload)
        else:
            log_line(f"unsupported target={target!r} payload_keys={list(payload.keys())}")
            self._send_error_json(
                400,
                "UnknownOperationException",
                f"unsupported target: {target or '(missing X-Amz-Target)'}",
            )

    def _key_op(self, op: str, payload: dict[str, Any], body: bytes = b"") -> None:
        req_loop = payload.get("loopId")
        profile = self._require_robot(loop_id=str(req_loop) if req_loop else None)
        if not profile:
            return
        robot_id = profile["robotId"]
        loop_id = req_loop or profile.get("loopId")

        if op == "ShouldCreate":
            # True => robot may create a local UGC key (STS keyRequired=false).
            log_line(f"Key.ShouldCreate robot={robot_id!r} loopId={loop_id!r} -> true")
            self._send_json(200, {"shouldCreate": True})
            return
        if op == "CreateRequest":
            public_key = payload.get("publicKey") or ""
            if not loop_id:
                self._send_error_json(400, "ValidationException", "loopId required")
                return
            rec = self.keys.create_request(
                robot_id=robot_id, loop_id=str(loop_id), public_key=str(public_key)
            )
            log_line(f"Key.CreateRequest id={rec['id']!r} loopId={loop_id!r}")
            self._send_json(200, rec)
            return
        if op == "GetRequest":
            req_id = payload.get("id")
            if not req_id:
                self._send_error_json(400, "ValidationException", "id required")
                return
            rec = self.keys.get_request(str(req_id))
            if not rec:
                self._send_error_json(404, "NotFound", "request not found")
                return
            self._send_json(200, rec)
            return
        if op == "Share":
            req_id = payload.get("id")
            encrypted = payload.get("encryptedKey")
            if not req_id or encrypted is None:
                self._send_error_json(
                    400, "ValidationException", "id and encryptedKey required"
                )
                return
            rec = self.keys.share(str(req_id), str(encrypted))
            if not rec:
                self._send_error_json(404, "NotFound", "request not found")
                return
            log_line(f"Key.Share id={req_id!r}")
            self._send_json(200, rec)
            return
        if op == "ListIncomingRequests":
            self._send_json(200, self.keys.list_incoming(str(loop_id or "")))
            return
        if op == "ListBinaryRequests":
            self._send_json(200, [])
            return
        if op == "ShareBinary":
            # No binary escrow here; answer in the model's shape so STS logs a
            # record instead of a parse error.
            req_id = str(payload.get("id") or uuid.uuid4().hex)
            log_line(f"Key.ShareBinary id={req_id!r} bytes={len(body)} (not stored)")
            self._send_json(
                200,
                {
                    "id": req_id,
                    "accountId": robot_id,
                    "loopId": str(loop_id or ""),
                    "encryptedUrl": "",
                },
            )
            return
        if op == "Backup":
            if not loop_id or payload.get("encryptedKey") is None:
                self._send_error_json(
                    400, "ValidationException", "loopId and encryptedKey required"
                )
                return
            rec = self.keys.backup_key(
                robot_id=robot_id,
                loop_id=str(loop_id),
                encrypted_key=str(payload["encryptedKey"]),
                password_hash=payload.get("passwordHash"),
            )
            log_line(f"Key.Backup loopId={loop_id!r}")
            self._send_json(200, rec)
            return
        if op == "Restore":
            if not loop_id:
                self._send_error_json(400, "ValidationException", "loopId required")
                return
            rec = self.keys.restore_key(str(loop_id))
            if not rec:
                # SSM hasKeyBackup treats this as "no key backup".
                self._send_error_json(404, "BACKUP_NOT_FOUND", "no key backup")
                return
            # Probe with passwordHash "X" from SSM — wrong password means backup exists.
            if payload.get("passwordHash") and payload.get("passwordHash") != rec.get(
                "passwordHash"
            ):
                self._send_error_json(403, "BACKUP_PASSWORD_WRONG", "wrong password")
                return
            self._send_json(
                200,
                {
                    "loopId": rec["loopId"],
                    "accountId": rec.get("accountId", robot_id),
                    "encryptedKey": rec["encryptedKey"],
                },
            )
            return

        log_line(f"unsupported Key op={op!r}")
        self._send_error_json(
            400, "UnknownOperationException", f"unsupported key op: {op}"
        )

    def _media_op(self, op: str, payload: dict[str, Any], body: bytes) -> None:
        headers = self._headers_lower()
        hdr_loop = headers.get("x-loop-id") or payload.get("loopId")
        profile = self._require_robot(loop_id=str(hdr_loop) if hdr_loop else None)
        if not profile:
            return
        robot_id = profile["robotId"]

        if op == "Create":
            # Media API: metadata in headers; body is the raw blob (may be empty
            # when MMS lacks a UGC key — still accept so photo sync does not block).
            loop_id = hdr_loop or profile["loopId"]
            path = headers.get("x-path") or payload.get("path") or f"media-{uuid.uuid4().hex[:8]}"
            media_type = headers.get("x-type") or payload.get("type")
            reference = headers.get("x-reference") or payload.get("reference")
            enc_hdr = headers.get("x-encrypted", "").lower()
            encrypted = enc_hdr in ("1", "true", "yes") or bool(payload.get("isEncrypted"))
            meta = {
                k[len("x-meta-") :]: v
                for k, v in headers.items()
                if k.startswith("x-meta-")
            }
            blob = body
            if blob.strip() in (b"", b"{}", b"null"):
                blob = b""
            rec = self.media.create(
                robot_id=robot_id,
                loop_id=str(loop_id),
                path=str(path),
                body=blob,
                media_type=media_type,
                reference=reference,
                encrypted=encrypted,
                meta=meta or None,
            )
            log_line(
                f"Media.Create robot={robot_id!r} loopId={loop_id!r} "
                f"path={rec['path']!r} bytes={len(blob)} type={media_type!r}"
            )
            self._send_json(200, rec)
            return
        # List/Get/Remove all answer with a list of media records.
        if op == "List":
            loop_ids = [str(x) for x in (payload.get("loopIds") or []) if x]
            self._send_json(
                200,
                self.media.list(
                    robot_id=robot_id,
                    loop_ids=loop_ids,
                    after=payload.get("after"),
                    before=payload.get("before"),
                ),
            )
            return
        if op == "Get":
            paths = [str(x) for x in (payload.get("paths") or []) if x]
            self._send_json(200, self.media.get(robot_id=robot_id, paths=paths))
            return
        if op == "Remove":
            paths = [str(x) for x in (payload.get("paths") or []) if x]
            recs = self.media.remove(robot_id=robot_id, paths=paths)
            log_line(f"Media.Remove robot={robot_id!r} removed={len(recs)}")
            self._send_json(200, recs)
            return

        log_line(f"unsupported Media op={op!r}")
        self._send_error_json(
            400, "UnknownOperationException", f"unsupported media op: {op}"
        )

    def _notification_op(self, op: str, payload: dict[str, Any]) -> None:
        robot_id = self._caller_robot_id()
        if op in ("NewRobotToken", "NewToken", "CreateToken"):
            token = hashlib.sha1(
                f"notify-{robot_id or 'anon'}".encode("utf-8")
            ).hexdigest()
            log_line(f"Notification.{op} robot={robot_id!r}")
            self._send_json(200, {"token": token})
            return
        if op in ("GetStatus", "Status"):
            self._send_json(200, {"connected": True})
            return
        if op in ("Subscribe", "Unsubscribe", "Publish"):
            self._send_json(200, {})
            return
        log_line(f"Notification.{op} robot={robot_id!r} (noop)")
        self._send_json(200, {})

    def _backup_op(self, op: str, payload: dict[str, Any]) -> None:
        loop_id = payload.get("loopId")
        if not loop_id:
            self._send_error_json(400, "ValidationException", "loopId required")
            return
        # Adopt the robot's loopId (from STS/KB) instead of rejecting a mismatch
        # against our synthetic sha1 — stock Backup.New requires loop.robot match
        # only, which we enforce by binding loopId to this access key.
        profile = self._require_robot(loop_id=str(loop_id))
        if not profile:
            return
        robot_id = profile["robotId"]
        if op == "New":
            self._send_json(
                200, self.backups.new_upload(robot_id=robot_id, loop_id=str(loop_id))
            )
            return
        if op == "List":
            self._send_json(
                200,
                self.backups.list_for_robot(robot_id=robot_id, loop_id=str(loop_id)),
            )
            return
        self._send_error_json(
            400,
            "UnknownOperationException",
            f"unsupported backup op: {op}",
        )

    def _loop_list(self, _payload: dict[str, Any]) -> None:
        # jibo-system-backup requires exactly one loop owned by this robot.
        profile = self._require_robot()
        if not profile:
            return
        now = int(time.time() * 1000)
        robot_id = profile["robotId"]
        loop_id = profile["loopId"]
        friendly = profile.get("friendlyId") or profile.get("serial") or robot_id[:12]
        log_line(
            f"Loop.ListLoops robot={robot_id!r} loopId={loop_id!r} "
            f"serial={profile.get('serial')!r} friendly={friendly!r}"
        )
        self._send_json(
            200,
            [
                {
                    "id": loop_id,
                    "name": friendly,
                    "owner": robot_id,
                    "robot": robot_id,
                    "robotFriendlyId": friendly,
                    "members": [],
                    "isSuspended": False,
                    "created": profile.get("firstSeen", now),
                    "updated": now,
                }
            ],
        )

    def _loop_get_robot(self, payload: dict[str, Any]) -> None:
        # Loop.GetRobot hands out the robot account key pair (pairing/OOBE uses
        # it, and System Manager writes the result back to credentials.json —
        # dropping our endpoint override). Only answer when the secret is really
        # configured; inventing one would lock the robot out of its own account.
        profile = self._require_robot()
        if not profile:
            return
        robot_id = profile["robotId"]
        secret = (self.config.get("accounts") or {}).get(robot_id)
        if not secret:
            log_line(
                f"Loop.GetRobot refused for robot={robot_id!r}: no secret in "
                "config.accounts (would overwrite /var/jibo/credentials.json)"
            )
            self._send_error_json(
                403, "AccessDenied", "robot credentials are not managed by this server"
            )
            return
        self._send_json(
            200,
            {
                "accessKeyId": robot_id,
                "secretAccessKey": secret,
                "friendlyId": profile.get("friendlyId") or profile.get("serial") or "",
            },
        )

    def _robot_get(self, payload: dict[str, Any]) -> None:
        profile = self._require_robot()
        if not profile:
            return
        req_id = payload.get("id") or profile["robotId"]
        if str(req_id) != str(profile["robotId"]):
            # Only serve this robot's own record when pointed at the local OTA host.
            self._send_error_json(404, "NotFound", "robot not found")
            return
        if payload.get("serialNumber"):
            profile = self.robots.touch(
                profile["robotId"], serial=payload.get("serialNumber")
            )
        now = int(time.time() * 1000)
        self._send_json(
            200,
            {
                "id": profile["robotId"],
                "payload": {
                    "friendlyId": profile.get("friendlyId"),
                    "serial": profile.get("serial"),
                    "loopId": profile.get("loopId"),
                },
                "created": profile.get("firstSeen", now),
                "updated": profile.get("lastSeen", now),
            },
        )

    def _get_update_from(self, payload: dict[str, Any]) -> None:
        from_version = payload.get("fromVersion")
        if not from_version:
            self._send_error_json(400, "ValidationException", "fromVersion required")
            return
        subsystem = payload.get("subsystem") or "main"
        filt = payload.get("filter") or ""
        update = self.catalog.get_from(
            subsystem=subsystem, from_version=from_version, filt=filt
        )
        if not update:
            log_line(
                f"GetUpdateFrom miss subsystem={subsystem!r} "
                f"from={from_version!r} filter={filt!r}"
            )
            # System Manager only treats this exact code as "no update";
            # any other code aborts the entire multi-subsystem check.
            self._send_error_json(404, "UPDATE_NOT_FOUND", "Update not found")
            return
        log_line(
            f"GetUpdateFrom hit subsystem={subsystem!r} "
            f"from={from_version!r} -> {update.get('toVersion')!r}"
        )
        self._send_json(200, update)

    def _list_updates_from(self, payload: dict[str, Any]) -> None:
        from_version = payload.get("fromVersion")
        if not from_version:
            self._send_error_json(400, "ValidationException", "fromVersion required")
            return
        subsystem = payload.get("subsystem") or "main"
        filt = payload.get("filter") or ""
        updates = self.catalog.list_from(
            subsystem=subsystem, from_version=from_version, filt=filt
        )
        self._send_json(200, [public_update(u) for u in updates])

    def _list_updates(self, payload: dict[str, Any]) -> None:
        subsystem = payload.get("subsystem") or "main"
        filt = payload.get("filter") or ""
        updates = [
            public_update(u)
            for u in self.catalog.updates
            if u["subsystem"] == subsystem and filter_matches(u.get("filter", ""), filt)
        ]
        self._send_json(200, updates)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--host", help="override config host")
    ap.add_argument("--port", type=int, help="override config port")
    ap.add_argument(
        "--no-auth",
        action="store_true",
        help="accept unsigned requests (local debugging only)",
    )
    args = ap.parse_args()

    config = load_json(args.config)
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.no_auth:
        config["require_auth"] = False

    catalog = Catalog(config)
    robots = RobotRegistry()
    backups = BackupStore(config, robots)
    keys = KeyStore()
    media = MediaStore(config)
    Handler.catalog = catalog
    Handler.config = config
    Handler.backups = backups
    Handler.robots = robots
    Handler.keys = keys
    Handler.media = media

    host = config.get("host", "0.0.0.0")
    port = int(config.get("port", 8042))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"listening on http://{host}:{port}", flush=True)
    print(f"public package URLs use {config['public_base_url']}", flush=True)
    print(f"auth {'ON' if config.get('require_auth', False) else 'OFF'}", flush=True)
    print("robot credentials endpoint should be:", config["public_base_url"], flush=True)
    print(f"per-robot backups under {BACKUPS_DIR}/<accessKeyId>/", flush=True)
    print(f"robot profiles under {ROBOTS_DIR}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
