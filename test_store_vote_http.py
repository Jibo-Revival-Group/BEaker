#!/usr/bin/env python3
"""BEacon's Store star over HTTP (with BEefy's check), and the web store reading /store/: python3 test_store_vote_http.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

here = Path(__file__).resolve().parent
mirror, votes = here / "updates" / "store-mirror.json", here / "updates" / "store-votes.json"
robots = here / "updates" / "robots"
assert not mirror.exists() and not votes.exists(), "run in a checkout without live Store state"
DAY = 24 * 3600 * 1000
now = int(time.time() * 1000)
# BEaker's own record of each key: old robots with a serial, a week-old-but-new one, one without a serial, an idle one.
profiles = {"old-key": {"serial": "S1", "firstSeen": now - 30 * DAY, "lastSeen": now - 3600_000},
            "old-key-2": {"serial": "S2", "firstSeen": now - 30 * DAY, "lastSeen": now - 3600_000},
            "new-key": {"serial": "S3", "firstSeen": now - 2 * DAY, "lastSeen": now},
            "no-serial-key": {"firstSeen": now - 30 * DAY, "lastSeen": now},
            "idle-key": {"serial": "S4", "firstSeen": now - 30 * DAY, "lastSeen": now - 5 * DAY}}
for key, data in profiles.items():
    assert not (robots / f"{key}.json").exists()
    robots.mkdir(parents=True, exist_ok=True)
    (robots / f"{key}.json").write_text(json.dumps(dict(data, robotId=key, loopId="l")))

beefy_seen: list[dict] = []
REAL_ROBOTS = {"Real-Robot", "Other-Robot"}  # robots BEefy has heard talking for a while


class FakeBeefy(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        beefy_seen.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        robot = self.headers.get("X-Jibo-RobotId", "")
        ok = robot in REAL_ROBOTS
        reply = json.dumps({"verified": True, "robot": robot} if ok else {"error": "Unable to star"}).encode()
        self.send_response(200 if ok else 403)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def post(port: int, body: dict, key: str | None = "old-key", robot: str | None = "Real-Robot") -> tuple[int, dict]:
    headers = {"X-Amz-Target": "Loop_20160324.StoreVote", "Content-Type": "application/json"}
    if key:
        # BEacon's signature (calendars.js signedHeaders); robots themselves sign with AWS4.
        headers["Authorization"] = f"AWS3 AWSAccessKeyId={key},Algorithm=HmacSHA256,SignedHeaders=host;x-amz-date;x-amz-target,Signature=s"
    if robot:
        headers["X-Jibo-RobotId"] = robot
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as res:
            return res.status, res.headers, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


store_list = json.dumps({"themes": ["Astra", {"url": "https://github.com/Zane/Astra"}]})
mirror.write_text(json.dumps({"manifest": store_list, "repos": {"zane/astra": {"tag": "v1", "checked": 0, "repo": {"owner": {"login": "Zane"}, "stargazers_count": 5}, "release": {}}}}))
beefy = ThreadingHTTPServer(("127.0.0.1", 0), FakeBeefy)
threading.Thread(target=beefy.serve_forever, daemon=True).start()
port = 18743
env = dict(os.environ, BEAKER_BEEFY_URL=f"http://127.0.0.1:{beefy.server_address[1]}/")
server = subprocess.Popen([sys.executable, str(here / "server.py"), "--port", str(port)], cwd=here, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
astra = {"repo": "Zane/Astra", "starred": True}
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); break
        except OSError:
            time.sleep(0.2)
    assert post(port, astra) == (200, {"repo": "zane/astra", "starred": True, "stars": 6})
    h = beefy_seen[-1]["headers"]
    assert h["x-amz-target"] == "Loop_20160324.StoreVote" and h["x-jibo-robotid"] == "Real-Robot" and h["authorization"].startswith("AWS3 "), "BEefy gets the robot's own request"
    assert post(port, astra)[1]["stars"] == 6, "once per robot"
    # Refused: BEefy doesn't know the robot; BEaker's record of the key isn't a regular robot's; the name changes.
    asked = len(beefy_seen)
    for args, why in (((astra, "old-key-2", "Made-Up-Robot"), "BEefy has never heard this robot"),
                      ((astra, "new-key", "Other-Robot"), "key first seen 2 days ago"),
                      ((astra, "no-serial-key", "Other-Robot"), "key never sent a serial"),
                      ((astra, "idle-key", "Other-Robot"), "key not seen for 5 days"),
                      ((astra, "unknown-key", "Other-Robot"), "key BEaker has never seen"),
                      ((astra, "old-key", "Other-Robot"), "the key starred as Real-Robot before"),
                      ((astra, "old-key-2", None), "no robot name")):
        assert post(port, *args) == (403, {"__type": "Forbidden", "error": "Unable to star", "message": "Unable to star"}), why
    assert len(beefy_seen) == asked + 1, "BEefy is only asked once BEaker's own checks pass"
    assert post(port, astra, "old-key-2", "Real-Robot")[1]["stars"] == 6, "a second key for the same robot adds nothing"
    assert post(port, astra, "old-key-2", "Other-Robot")[0] == 403, "and that key is now Real-Robot's"
    assert post(port, astra, None)[0] == 401, "needs a robot key"
    assert post(port, {"repo": "nope/nope", "starred": True})[0] == 404
    assert post(port, {"repo": "../x", "starred": True})[0] == 400 and post(port, {"repo": "Zane/Astra", "starred": "yes"})[0] == 400
    beefy.shutdown(); beefy.server_close()
    assert post(port, {"repo": "Zane/Astra", "starred": False})[0] == 403, "BEefy unreachable: no star changes"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/store/repos/zane/astra", timeout=10) as res:
        assert res.headers["Access-Control-Allow-Origin"] == "*" and json.loads(res.read())["stargazers_count"] == 6
    status, headers, _ = get(port, "/store/repos/a/b")
    assert status == 404 and headers["Access-Control-Allow-Origin"] == "*"
    status, headers, body = get(port, "/store/manifest.json")
    assert status == 200 and body.decode() == store_list and headers["Access-Control-Allow-Origin"] == "*", "the Store list"
    for path in ("/store/raw/someone/else/HEAD/README.md", "/store/raw/Zane/Astra/HEAD/../x", "/store/raw/Zane/Astra/HEAD/%2e%2e/x"):
        assert get(port, path)[0] == 404, path  # outside the Store, or not a file path: never fetched
    print("PASS store vote http")
finally:
    server.terminate(); server.wait(5)
    mirror.unlink(); votes.unlink(missing_ok=True)
    for key in profiles:
        (robots / f"{key}.json").unlink(missing_ok=True)
    for extra in robots.glob("unknown-key*.json"):
        extra.unlink()
