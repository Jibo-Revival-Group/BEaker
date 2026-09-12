#!/usr/bin/env python3
"""Stamp Release-X.Y.Z-YYYYMMDD into bin/jibo-service-version (same-length ELF patch).

System Manager reads the version via:
  Jibo Service Version: Release-(\\d+\\.\\d+\\.\\d+).*

The embedded literal must stay exactly 23 bytes (stock: Release-13.0.0-20190225).
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

DEFAULT_BIN = Path.home() / "BEnch" / "usr" / "local" / "bin" / "jibo-service-version"
RELEASE_RE = re.compile(rb"Release-\d+\.\d+\.\d+-\d{8}")
LITERAL_LEN = 23  # len(b"Release-13.0.0-20190225")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bin",
        type=Path,
        default=DEFAULT_BIN,
        help=f"Path to jibo-service-version (default: {DEFAULT_BIN})",
    )
    ap.add_argument(
        "--version",
        required=True,
        help="X.Y.Z to embed (e.g. 13.0.1). Release-<ver>-YYYYMMDD must be 23 bytes.",
    )
    ap.add_argument(
        "--date",
        default=None,
        help="YYYYMMDD suffix (default: today)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the replacement without writing",
    )
    args = ap.parse_args()

    path = args.bin.resolve()
    if not path.is_file():
        raise SystemExit(f"missing {path}")

    day = args.date or date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", day):
        raise SystemExit(f"--date must be YYYYMMDD, got {day!r}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit(f"--version must be X.Y.Z, got {args.version!r}")

    new = f"Release-{args.version}-{day}".encode("ascii")
    if len(new) != LITERAL_LEN:
        raise SystemExit(
            f"{new!r} is {len(new)} bytes; need exactly {LITERAL_LEN}. "
            "Shorten X.Y.Z or use a different date format is not allowed."
        )

    data = path.read_bytes()
    matches = list(RELEASE_RE.finditer(data))
    if len(matches) != 1:
        raise SystemExit(
            f"{path}: expected exactly one Release-X.Y.Z-YYYYMMDD, found {len(matches)}"
        )
    old = matches[0].group(0)
    if old == new:
        print(f"already {new!r}")
        return

    print(f"{path}: {old!r} -> {new!r}")
    if args.dry_run:
        return

    patched = data[: matches[0].start()] + new + data[matches[0].end() :]
    path.write_bytes(patched)
    print("wrote", path)


if __name__ == "__main__":
    main()
