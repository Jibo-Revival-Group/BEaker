#!/usr/bin/env python3
"""Regressions for the 2026-10-30 bug review: python3 test_review_fixes.py

Runs server.py in a temp dir with require_auth false (like prod config.json), so
a caller is identified only by the access key in its Authorization header.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{': ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def call(base: str, target: str, payload, robot: str = "robot-a", raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(base + "/", data=body, method="POST", headers={
        "X-Amz-Target": target, "Content-Type": "application/x-amz-json-1.1",
        "Authorization": f"AWS4-HMAC-SHA256 Credential={robot}/20261030/api/x/aws4_request, "
                         "SignedHeaders=host, Signature=00"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def loop_of(base: str, robot: str) -> str:
    return call(base, "Loop_20160324.ListLoops", {}, robot=robot)[1][0]["id"]


def put(url: str, body: bytes = b"x"):
    req = urllib.request.Request(url, data=body, method="PUT", headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        return e.code, None


def backup_checks(base: str, work: Path) -> None:
    loop_b = loop_of(base, "robot-b")
    url = call(base, "Backup_20170222.New", {"loopId": loop_b}, robot="robot-b")[1]["uploadUrl"]
    status, etag = put(url, b"real backup")
    check("robot PUT to its uploadUrl", status == 200 and bool(etag), str(status))
    check("uploadUrl is single-use", put(url, b"again")[0] == 403)
    status, _ = put(f"{base}/backups/robot-b/zzz-forged?loopId={loop_b}", b"junk")
    check("PUT without a Backup.New token is refused", status == 403, str(status))
    url2 = call(base, "Backup_20170222.New", {"loopId": loop_b}, robot="robot-b")[1]["uploadUrl"]
    check("token is bound to its robot/name", put(url2.replace("/robot-b/", "/robot-c/"))[0] == 403)
    check("PUT with the token stripped is refused", put(url2.split("&")[0] if "&" in url2 else url2.split("?")[0])[0] == 403)
    items = call(base, "Backup_20170222.List", {"loopId": loop_b}, robot="robot-b")[1]
    check("List still shows the robot's own upload", [i["etag"] for i in items] == [etag], str(items))

    # Two uploads in the same second: List must verify against the newest.
    etags = []
    for body in (b"first", b"second", b"third"):
        u = call(base, "Backup_20170222.New", {"loopId": loop_b}, robot="robot-b")[1]["uploadUrl"]
        etags.append(put(u, body)[1])
    items = call(base, "Backup_20170222.List", {"loopId": loop_b}, robot="robot-b")[1]
    check("same-second List returns the newest upload", [i["etag"] for i in items] == etags[-1:],
          f"{[i['etag'] for i in items]} vs {etags}")
    metas = list((work / "updates" / "backups" / "robot-b").glob("*.meta.json"))
    check("only the newest backup meta per loop is kept", len(metas) == 1, str(len(metas)))


def key_checks(base: str, work: Path) -> None:
    loop_a = loop_of(base, "robot-a")
    status, _ = call(base, "Key_20160201.Backup",
                     {"loopId": loop_a, "encryptedKey": "SECRET-A", "passwordHash": "pw-A"})
    check("owner Key.Backup", status == 200, str(status))

    status, data = call(base, "Key_20160201.Restore", {"loopId": loop_a}, robot="robot-evil")
    check("other robot Restore without passwordHash gets no key",
          status >= 400 and "SECRET-A" not in json.dumps(data), f"{status} {data}")
    status, data = call(base, "Key_20160201.Restore", {"loopId": loop_a, "passwordHash": "pw-A"},
                        robot="robot-evil")
    check("other robot Restore with right passwordHash gets no key",
          status >= 400 and "SECRET-A" not in json.dumps(data), f"{status} {data}")
    status, data = call(base, "Key_20160201.Restore", {"loopId": loop_a})
    check("owner Restore without passwordHash gets no key",
          status == 403 and "SECRET-A" not in json.dumps(data), f"{status} {data}")
    status, data = call(base, "Key_20160201.Restore", {"loopId": loop_a, "passwordHash": "X"})
    check("owner hasKeyBackup probe is BACKUP_PASSWORD_WRONG",
          status == 403 and (data or {}).get("__type") == "BACKUP_PASSWORD_WRONG", f"{status} {data}")

    status, data = call(base, "Key_20160201.Backup",
                        {"loopId": loop_a, "encryptedKey": "EVIL", "passwordHash": "x"}, robot="robot-evil")
    check("other robot cannot overwrite the key backup", status >= 400, f"{status} {data}")
    status, data = call(base, "Key_20160201.Restore", {"loopId": loop_a, "passwordHash": "pw-A"})
    check("owner Restore still returns its key",
          status == 200 and (data or {}).get("encryptedKey") == "SECRET-A", f"{status} {data}")


def main() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="beaker-review-") as tmp:
        work = Path(tmp)
        shutil.copy(ROOT / "server.py", work / "server.py")
        (work / "updates").mkdir()
        (work / "updates" / "manifest.json").write_text("[]")
        (work / "config.json").write_text(json.dumps(
            {"host": "127.0.0.1", "port": port, "public_base_url": base, "require_auth": False, "accounts": {}}))
        proc = subprocess.Popen([sys.executable, "server.py", "--config", str(work / "config.json")], cwd=work,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + "/health", timeout=0.5).read()
                    break
                except OSError:
                    time.sleep(0.05)
            for checks in (key_checks, backup_checks):
                try:
                    checks(base, work)
                except Exception as exc:  # a dropped connection is a failure, not a crash of the suite
                    check(f"{checks.__name__} ran", False, repr(exc))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
        if FAILURES:
            print("\n--- server log ---\n" + out[-4000:])
    print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}" if FAILURES else "\nall checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
