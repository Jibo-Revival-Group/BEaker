#!/usr/bin/env python3
"""Signed GetUpdateFrom smoke test against ota-server."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def hmac_sha256(key: bytes, msg: str | bytes) -> bytes:
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


def sign_request(
    *,
    method: str,
    url: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
) -> dict[str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-date"
    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    k_date = hmac_sha256(("AWS4" + secret_key).encode(), date_stamp)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    k_signing = hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": auth,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": "Update_20160301.GetUpdateFrom",
        "Content-Type": "application/x-amz-json-1.1",
        "Host": host,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:8042")
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    ap.add_argument("--subsystem", default="@be/be")
    ap.add_argument("--from-version", default="12.0.0")
    ap.add_argument("--filter", default="eau")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())
    access_key, secret_key = next(iter(cfg["accounts"].items()))
    body = json.dumps(
        {
            "fromVersion": args.from_version,
            "subsystem": args.subsystem,
            "filter": args.filter,
        }
    ).encode()
    url = args.endpoint.rstrip("/") + "/"
    headers = sign_request(
        method="POST",
        url=url,
        body=body,
        access_key=access_key,
        secret_key=secret_key,
        region=cfg.get("region", "api"),
        service=cfg.get("service", "update"),
    )
    # urllib sets Host itself; drop explicit Host to avoid mismatch
    headers.pop("Host", None)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        print(resp.read().decode())


if __name__ == "__main__":
    main()
