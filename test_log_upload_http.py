#!/usr/bin/env python3
"""Robot log uploads: python3 test_log_upload_http.py

Jibo's log client (logrotate's firstaction, every 10 min) asks Log_*.PutEventsAsync for an uploadUrl and crashed when it
got none; a failing firstaction makes logrotate skip the rotation, so /tmp/messages grew until the 64 MB /tmp was full
and Jibo stopped. BEaker now hands out an https URL on its public host (never :80) that accepts and discards the upload.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

here = Path(__file__).resolve().parent
port = 18751
server = subprocess.Popen([sys.executable, str(here / "server.py"), "--port", str(port)], cwd=here, env=dict(os.environ),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def call(target: str) -> dict:
    headers = {"X-Amz-Target": target, "Content-Type": "application/x-amz-json-1.1",
               "Authorization": "AWS3 AWSAccessKeyId=log-test-key,Algorithm=HmacSHA256,SignedHeaders=host,Signature=s"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps({"kind": "LOG", "serial": "X"}).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 200
        return json.loads(res.read() or b"{}")


def put(path: str, body: bytes, length: str | None = None) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="PUT")
    if length is not None:
        req.add_unredirected_header("Content-Length", length)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status
    except urllib.error.HTTPError as err:
        return err.code


try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
        except Exception:
            time.sleep(0.2)
    public = json.loads((here / "config.json").read_text())["public_base_url"]
    for op in ("PutEventsAsync", "PutBinaryAsync", "PutAsrBinary"):
        url = call(f"Log_20160715.{op}").get("uploadUrl")
        assert url, f"{op} must give the log client an uploadUrl"
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.port is None and parsed.hostname == urlparse(public).hostname, url
        assert parsed.path == "/logs/discard", url
    assert "uploadUrl" not in call("Log_20160715.PutEvents"), "the synchronous call carries its data itself"
    before = sorted(p.name for p in (here / "updates").rglob("*")) if (here / "updates").exists() else []
    assert put("/logs/discard", b"x" * 300_000) == 200, "a log upload is accepted"
    after = sorted(p.name for p in (here / "updates").rglob("*")) if (here / "updates").exists() else []
    assert before == after, "and nothing is stored"
    assert put("/logs/discard", b"x", str(400 * 1024 * 1024)) == 413
    print("PASS log uploads get an https uploadUrl that accepts and discards them")
finally:
    server.terminate(); server.wait(10)
    for p in (here / "updates" / "robots").glob("log-test-key*.json"):
        p.unlink()
