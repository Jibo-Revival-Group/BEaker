#!/usr/bin/env python3
"""End-to-end check of the OTA/backup stubs against stock robot behavior.

Runs server.py in an isolated data directory and replays exactly what the robot
does, in order:

  jibo-get-update       Update_20160301.GetUpdateFrom  (hit + UPDATE_NOT_FOUND)
  jibo-system-backup    Loop_20160324.ListLoops -> Backup_20170222.New
                        -> PUT uploadUrl -> Backup_20170222.List (verify ETag)
  jibo-system-restore   Backup_20170222.List -> GET location.url
  mms / sts             Media_20160725.*, Key_20160201.*, Notification_*

Field names and shapes come from the robot's own
/usr/lib/node_modules/@jibo/jibo-server-client/apis/*.min.json.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from server import reload_allowed

ROOT = Path(__file__).resolve().parent
ROBOT = "selftest-robot"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base: str, target: str, payload: dict, headers: dict | None = None):
    """POST like jibo-server-client does (auth off), returning (status, parsed)."""
    body = json.dumps(payload).encode()
    hdrs = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": target,
        # jibo-server-client always signs; the access key is how the server
        # identifies the robot even when require_auth is false.
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={ROBOT}/20260730/api/x/aws4_request, "
            "SignedHeaders=host;x-amz-date, Signature=deadbeef"
        ),
    }
    hdrs.update(headers or {})
    req = urllib.request.Request(base + "/", data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def short_put(url: str, *, body: bytes, claimed_length: int) -> str:
    """PUT fewer bytes than Content-Length promises, then read the status line."""
    parsed = urllib.parse.urlparse(url)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=10) as sock:
        sock.sendall(
            f"PUT {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
            f"Content-Length: {claimed_length}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + body
        )
        sock.shutdown(socket.SHUT_WR)
        return sock.recv(200).decode("latin-1").splitlines()[0]


def stock_newest(items: list[dict]) -> dict:
    """What jibo-system-backup/restore get out of their reduce().

    They do response.reduce((a, b) => Math.max(new Date(a.modified), ...)), which
    returns a Number the moment there are two entries and then throws on
    entry.etag ("Failed to upload backup file").
    """
    if not items:
        raise AssertionError("empty list: 'Couldn't find any uploaded files'")
    if len(items) > 1:
        raise AssertionError(f"{len(items)} entries: stock reduce() returns a Number")
    return items[0]


def main() -> int:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="ota-selftest-") as tmp:
        work = Path(tmp)
        shutil.copy(ROOT / "server.py", work / "server.py")
        updates = work / "updates"
        (updates / "packages").mkdir(parents=True)
        pkg = updates / "packages" / "selftest.tar"
        with tarfile.open(pkg, "w"):
            pass
        (updates / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "_id": "selftest-be",
                        "fromVersion": "10.0.18",
                        "toVersion": "13.0.0",
                        "subsystem": "@be/be",
                        "filter": "eau,fcs",
                        "changes": "selftest",
                        "package": "selftest.tar",
                    }
                ]
            )
        )
        base = f"http://127.0.0.1:{port}"
        config = work / "config.json"
        config.write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "public_base_url": base,
                    "require_auth": False,
                    "region": "api",
                    "accounts": {},
                }
            )
        )
        proc = subprocess.Popen(
            [sys.executable, "server.py", "--config", str(config)],
            cwd=work,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + "/health", timeout=0.5).read()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                print("server did not start")
                return 1
            run_checks(base)
            check(
                "backup payload is not persisted",
                not (updates / "backups" / ROBOT).exists(),
            )
            check(
                "media payload is not persisted",
                not (updates / "media" / ROBOT).exists(),
            )
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
        if FAILURES:
            print("\n--- server log ---")
            print(out)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


def run_checks(base: str) -> None:
    # --- jibo-get-update -----------------------------------------------------
    status, data = call(
        base,
        "Update_20160301.GetUpdateFrom",
        {"fromVersion": "10.0.18", "subsystem": "@be/be", "filter": "fcs"},
    )
    required = ["_id", "created", "accountId", "fromVersion", "toVersion", "changes",
                "url", "shaHash", "subsystem"]
    missing = [k for k in required if not (data or {}).get(k) is not None]
    check("GetUpdateFrom hit", status == 200 and not missing, f"missing={missing}")
    check(
        "GetUpdateFrom echoes requested filter",
        (data or {}).get("filter") == "fcs",
        str((data or {}).get("filter")),
    )
    status, data = call(
        base,
        "Update_20160301.GetUpdateFrom",
        {"fromVersion": "11.2.0", "subsystem": "@be/be", "filter": "eau"},
    )
    check(
        "GetUpdateFrom any fromVersion -> latest",
        status == 200
        and (data or {}).get("toVersion") == "13.0.0"
        and (data or {}).get("fromVersion") == "11.2.0",
        str(data),
    )
    status, data = call(
        base,
        "Update_20160301.GetUpdateFrom",
        {"fromVersion": "13.0.0", "subsystem": "@be/be", "filter": "fcs"},
    )
    check(
        "GetUpdateFrom already-latest is UPDATE_NOT_FOUND",
        status == 404 and (data or {}).get("__type") == "UPDATE_NOT_FOUND",
        f"{status} {data}",
    )
    status, data = call(
        base,
        "Update_20160301.GetUpdateFrom",
        {"fromVersion": "0.0.0", "subsystem": "os", "filter": "fcs"},
    )
    # UpdateManager treats only this code as "no updates for <subsystem>";
    # anything else aborts the whole multi-subsystem check.
    check(
        "GetUpdateFrom miss is UPDATE_NOT_FOUND",
        status == 404 and (data or {}).get("__type") == "UPDATE_NOT_FOUND",
        f"{status} {data}",
    )

    # --- jibo-system-backup --------------------------------------------------
    status, loops = call(base, "Loop_20160324.ListLoops", {})
    ok = status == 200 and isinstance(loops, list) and len(loops) == 1
    check("ListLoops returns exactly one loop", ok, str(loops))
    if not ok:
        return
    loop = loops[0]
    check("loop has id + owner", bool(loop.get("id") and loop.get("owner")), str(loop))
    loop_id = loop["id"]

    status, new = call(base, "Backup_20170222.New", {"loopId": loop_id})
    check("Backup.New returns uploadUrl", status == 200 and bool((new or {}).get("uploadUrl")))
    upload_url = (new or {}).get("uploadUrl")
    if not upload_url:
        return
    check("uploadUrl contains loopId=", "loopId=" in upload_url, upload_url)

    blob = b"selftest-backup" * 1000
    req = urllib.request.Request(
        upload_url,
        data=blob,
        method="PUT",
        headers={"Content-Type": "application/octet-stream", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req) as resp:
        etag = resp.headers.get("ETag")
        check("PUT upload returns 200 + ETag", resp.status == 200 and bool(etag), str(etag))

    status, items = call(base, "Backup_20170222.List", {"loopId": loop_id})
    try:
        entry = stock_newest(items or [])
        check("Backup.List survives stock reduce()", True)
    except AssertionError as exc:
        check("Backup.List survives stock reduce()", False, str(exc))
        return
    check("listed ETag matches the upload", entry.get("etag") == etag,
          f"{entry.get('etag')} vs {etag}")
    check("entry has location.url (restore needs it)",
          bool((entry.get("location") or {}).get("url")))
    check("size is a string per the model", isinstance(entry.get("size"), str),
          type(entry.get("size")).__name__)

    # Second upload: the list must still hold exactly one (newest) entry.
    time.sleep(1.1)  # modified has one-second resolution
    _, new2 = call(base, "Backup_20170222.New", {"loopId": loop_id})
    req = urllib.request.Request(new2["uploadUrl"], data=b"newer" * 1000, method="PUT")
    with urllib.request.urlopen(req) as resp:
        etag2 = resp.headers.get("ETag")
    status, items = call(base, "Backup_20170222.List", {"loopId": loop_id})
    ok = isinstance(items, list) and len(items) == 1 and items[0].get("etag") == etag2
    check("second upload replaces the listed backup", ok, str(items))

    # A short PUT must not be committed: the ETag would otherwise vouch for a
    # truncated (undecryptable) archive.
    _, new3 = call(base, "Backup_20170222.New", {"loopId": loop_id})
    status_line = short_put(new3["uploadUrl"], body=b"short", claimed_length=999999)
    check("short PUT is rejected", " 400 " in status_line, status_line.strip())
    status, items = call(base, "Backup_20170222.List", {"loopId": loop_id})
    check("rejected upload is not listed", len(items or []) == 1 and items[0]["etag"] == etag2)

    # The no-op server advertises the upload for protocol compatibility, but
    # deliberately has no payload for restore to download.
    url = items[0]["location"]["url"]
    try:
        urllib.request.urlopen(url)
    except urllib.error.HTTPError as exc:
        check("discarded backup is not downloadable", exc.code == 404, str(exc))
    else:
        check("discarded backup is not downloadable", False, "unexpected 200 response")
    check("location.url is http for a plain OTA host", url.startswith("http://"), url)

    # Backups from another loop are never offered: they were encrypted with a
    # different symmetric key (keys/symmetric-<loopId>).
    status, other = call(base, "Backup_20170222.List", {"loopId": "ffffffffffffffffffffffff"})
    check("other loopId sees no backups", other == [], str(other))

    # --- loopId stability ----------------------------------------------------
    status, loops = call(base, "Loop_20160324.ListLoops", {})
    check("loopId is stable after Backup calls", loops[0]["id"] == loop_id, loops[0]["id"])

    # --- media (mms photo sync) ---------------------------------------------
    req = urllib.request.Request(
        base + "/",
        data=b"\x89PNG-not-really",
        method="POST",
        headers={
            "X-Amz-Target": "Media_20160725.Create",
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={ROBOT}/20260730/api/media/aws4_request, "
                "SignedHeaders=host, Signature=deadbeef"
            ),
            "x-loop-id": loop_id,
            "x-path": "selftest-photo",
            "x-type": "thumb",
            "x-meta-width": "640",
        },
    )
    with urllib.request.urlopen(req) as resp:
        rec = json.loads(resp.read())
    check("Media.Create returns path + created",
          bool(rec.get("path")) and rec.get("created") is not None, str(rec))
    check("Media.Create keeps x-meta-* headers", (rec.get("meta") or {}).get("width") == "640",
          str(rec.get("meta")))
    status, got = call(base, "Media_20160725.Get", {"paths": ["selftest-photo"]})
    check("Media.Get is a no-op", status == 200 and got == [], str(got))
    status, listed = call(base, "Media_20160725.List", {"loopIds": [loop_id]})
    check("Media.List is a no-op", status == 200 and listed == [], str(listed))
    status, removed = call(base, "Media_20160725.Remove", {"paths": ["selftest-photo"]})
    check("Media.Remove is a no-op", status == 200 and removed == [], str(removed))

    # --- reload allowlist ----------------------------------------------------
    check("reload allows other 192.168 clients", reload_allowed("192.168.1.1"))
    check("reload denies the tunnel peer", not reload_allowed("192.168.7.55"))
    check("reload denies non-LAN clients", not reload_allowed("203.0.113.10"))
    try:
        urllib.request.urlopen(base + "/reload")
    except urllib.error.HTTPError as exc:
        check("reload rejects the local non-LAN test peer", exc.code == 403, str(exc))
    else:
        check("reload rejects the local non-LAN test peer", False, "unexpected 200 response")

    # --- key (sts) -----------------------------------------------------------
    status, should = call(base, "Key_20160201.ShouldCreate", {"loopId": loop_id})
    check("Key.ShouldCreate is true (robot creates its own UGC key)",
          status == 200 and (should or {}).get("shouldCreate") is True, str(should))
    status, backed = call(
        base, "Key_20160201.Backup", {"loopId": loop_id, "encryptedKey": "AAA="}
    )
    check("Key.Backup echoes loopId + encryptedKey",
          status == 200 and (backed or {}).get("encryptedKey") == "AAA=", str(backed))
    status, restored = call(base, "Key_20160201.Restore", {"loopId": loop_id})
    check("Key.Restore returns the escrowed key",
          status == 200 and (restored or {}).get("encryptedKey") == "AAA=", str(restored))
    status, restored = call(
        base, "Key_20160201.Restore", {"loopId": "ffffffffffffffffffffffff"}
    )
    check("Key.Restore without a backup is BACKUP_NOT_FOUND",
          status == 404 and (restored or {}).get("__type") == "BACKUP_NOT_FOUND",
          f"{status} {restored}")

    # --- notification --------------------------------------------------------
    status, tok = call(base, "Notification_20150505.NewRobotToken", {"deviceId": "x"})
    check("NewRobotToken returns a token", status == 200 and bool((tok or {}).get("token")))
    status, st = call(base, "Notification_20150505.GetStatus", {"accountId": ROBOT})
    check("GetStatus returns connected", status == 200 and (st or {}).get("connected") is True)


if __name__ == "__main__":
    sys.exit(main())
