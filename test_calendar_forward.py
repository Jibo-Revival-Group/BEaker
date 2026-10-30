#!/usr/bin/env python3
"""BEacon's calendar settings are forwarded to BEefy: python3 test_calendar_forward.py"""
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

seen: list[dict] = []
reply = {"status": 200, "body": b'{"loopId":"l","members":[]}'}


class FakeBeefy(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        seen.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        self.send_response(reply["status"])
        self.send_header("Content-Length", str(len(reply["body"])))
        self.end_headers()
        self.wfile.write(reply["body"])

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def post(port: int, target: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), method="POST", headers={
        "X-Amz-Target": target, "Authorization": "AWS3 AWSAccessKeyId=k,Algorithm=HmacSHA256,SignedHeaders=host,Signature=s",
        "X-Amz-Date": "Fri, 30 Oct 2026 03:00:00 GMT", "X-Jibo-RobotId": "robot-1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def main() -> None:
    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeBeefy)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = 18742
    here = Path(__file__).resolve().parent
    env = dict(os.environ, BEAKER_BEEFY_URL=f"http://127.0.0.1:{fake.server_address[1]}/")
    server = subprocess.Popen([sys.executable, str(here / "server.py"), "--port", str(port)], cwd=here, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        status, data = post(port, "Loop_20160324.GetCalendarFeeds", {})
        assert (status, data) == (200, {"loopId": "l", "members": []}), (status, data)
        h = seen[-1]["headers"]
        assert h["x-amz-target"] == "Loop_20160324.GetCalendarFeeds" and h["x-jibo-robotid"] == "robot-1" and h["authorization"].startswith("AWS3 ")
        reply.update(status=400, body=b'{"error":"memberId is required."}')
        assert post(port, "Loop_20160324.SetCalendarFeed", {"icalUrl": "https://x"}) == (400, {"error": "memberId is required."})
        assert json.loads(seen[-1]["body"]) == {"icalUrl": "https://x"}, "body passes through unchanged"
        count = len(seen)
        post(port, "Loop_20160324.ListLoops", {})
        assert len(seen) == count, "other Loop targets stay local"
        fake.shutdown(); fake.server_close()
        status, data = post(port, "Loop_20160324.TestCalendarFeed", {"memberId": "m"})
        assert status == 502 and "calendar" in data["error"], (status, data)
        print("calendar targets forward to BEefy (status, body and robot headers kept); other targets stay local")
    finally:
        server.terminate(); server.wait(5)


if __name__ == "__main__":
    main()
