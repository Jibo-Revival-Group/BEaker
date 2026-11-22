#!/usr/bin/env python3
"""BEacon's Store star over HTTP, and the web store reading /store/ from a browser: python3 test_store_vote_http.py"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

here = Path(__file__).resolve().parent
mirror, votes = here / "updates" / "store-mirror.json", here / "updates" / "store-votes.json"
assert not mirror.exists() and not votes.exists(), "run in a checkout without live Store state"


def post(port: int, body: dict, key: str | None = "robot-key") -> tuple[int, dict]:
    headers = {"X-Amz-Target": "Loop_20160324.StoreVote", "Content-Type": "application/json"}
    if key:
        # BEacon's signature (calendars.js signedHeaders); robots themselves sign with AWS4.
        headers["Authorization"] = f"AWS3 AWSAccessKeyId={key},Algorithm=HmacSHA256,SignedHeaders=host;x-amz-date;x-amz-target,Signature=s"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


store_list = json.dumps({"themes": ["Astra", {"url": "https://github.com/Zane/Astra"}]})
mirror.write_text(json.dumps({"manifest": store_list, "repos": {"zane/astra": {"tag": "v1", "checked": 0, "repo": {"owner": {"login": "Zane"}, "stargazers_count": 5}, "release": {}}}}))


def get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()
port = 18743
server = subprocess.Popen([sys.executable, str(here / "server.py"), "--port", str(port)], cwd=here, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
        except OSError:
            time.sleep(0.2)
    assert post(port, {"repo": "Zane/Astra", "starred": True}) == (200, {"repo": "zane/astra", "starred": True, "stars": 6})
    assert post(port, {"repo": "Zane/Astra", "starred": True})[1]["stars"] == 6, "once per robot"
    assert post(port, {"repo": "Zane/Astra", "starred": True}, "other-robot")[1]["stars"] == 7
    assert post(port, {"repo": "Zane/Astra", "starred": True}, None)[0] == 401, "needs a robot"
    assert post(port, {"repo": "nope/nope", "starred": True})[0] == 404
    assert post(port, {"repo": "../x", "starred": True})[0] == 400 and post(port, {"repo": "Zane/Astra", "starred": "yes"})[0] == 400
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/store/repos/zane/astra", timeout=10) as res:
        assert res.headers["Access-Control-Allow-Origin"] == "*" and json.loads(res.read())["stargazers_count"] == 7
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/store/repos/a/b", timeout=10); raise AssertionError("404 expected")
    except urllib.error.HTTPError as err:
        assert err.code == 404 and err.headers["Access-Control-Allow-Origin"] == "*"
    status, headers, body = get(port, "/store/manifest.json")
    assert status == 200 and body.decode() == store_list and headers["Access-Control-Allow-Origin"] == "*", "the Store list"
    for path in ("/store/raw/someone/else/HEAD/README.md", "/store/raw/Zane/Astra/HEAD/../x", "/store/raw/Zane/Astra/HEAD/%2e%2e/x"):
        assert get(port, path)[0] == 404, path  # outside the Store, or not a file path: never fetched
    print("PASS store vote http")
finally:
    server.terminate(); server.wait(5)
    mirror.unlink(); votes.unlink(missing_ok=True)
