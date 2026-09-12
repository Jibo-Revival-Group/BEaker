#!/usr/bin/env python3
"""Walk ADLC.bin services partition (p4) and emit updates/services-reference.tsv.

Uses one `debugfs -R` per directory (parallelised). Batching many `ls -p` into a
single debugfs stdin session silently mis-attributes listings — do not "optimise"
that way.

Self-checks expected stock 13.0.0 counts before writing:
  17014 entries (excluding lost+found), 14864 files, 2128 dirs, 22 symlinks,
  uniform 0750 0:10 for regular files/dirs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = Path("/home/zane/jsih/ADLC.bin")
# GPT p4 (services /usr/local): start sector 4198434
DEFAULT_OFFSET = 4198434 * 512
DEFAULT_OUT = ROOT / "updates" / "services-reference.tsv"

EXPECTED_FILES = 14864
EXPECTED_DIRS = 2127  # stock dirs excluding lost+found
EXPECTED_SYMS = 22
EXPECTED_TOTAL = EXPECTED_FILES + EXPECTED_DIRS + EXPECTED_SYMS  # 17013

LS_RE = re.compile(r"^/(\d+)/(\d+)/(\d+)/(\d+)/(.*?)/(\d*)/\s*$")
FAST_LINK_RE = re.compile(r'Fast link dest:\s*"(.*)"')


def image_spec(image: Path, offset: int) -> str:
    return f"{image}?offset={offset}"


def debugfs_cmd(img: str, request: str) -> str:
    proc = subprocess.run(
        ["debugfs", "-R", request, img],
        capture_output=True,
        text=True,
    )
    # debugfs prints its banner on stderr; listings go to stdout.
    return proc.stdout


def ls_dir(img: str, directory: str) -> list[tuple[str, int, int, int, int]]:
    out = debugfs_cmd(img, f'ls -p "{directory}"')
    entries: list[tuple[str, int, int, int, int]] = []
    for line in out.splitlines():
        m = LS_RE.match(line)
        if not m:
            continue
        _ino, mode_s, uid_s, gid_s, name, size_s = m.groups()
        if name in (".", ".."):
            continue
        entries.append(
            (name, int(mode_s, 8), int(uid_s), int(gid_s), int(size_s or 0))
        )
    return entries


def symlink_target(img: str, path: str) -> str:
    stat_out = debugfs_cmd(img, f'stat "{path}"')
    m = FAST_LINK_RE.search(stat_out)
    if m:
        return m.group(1)
    # Out-of-line targets (>60 bytes): cat returns the link text.
    target = debugfs_cmd(img, f'cat "{path}"').rstrip("\n\0")
    if not target:
        raise RuntimeError(f"could not resolve symlink target for {path}")
    return target


def walk(img: str) -> dict[str, tuple[int, int, int, int, str]]:
    """Return path -> (mode, uid, gid, type, linktarget)."""
    entries: dict[str, tuple[int, int, int, int, str]] = {}
    level = ["/"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        while level:
            results = list(pool.map(lambda d: (d, ls_dir(img, d)), level))
            next_level: list[str] = []
            for directory, items in results:
                for name, mode, uid, gid, _size in items:
                    path = (
                        f"/{name}" if directory == "/" else f"{directory.rstrip('/')}/{name}"
                    )
                    typ = mode & 0o170000
                    entries[path] = (mode & 0o7777, uid, gid, typ, "")
                    if typ == 0o040000:
                        next_level.append(path)
            level = next_level

    # Resolve symlink targets (few of them).
    for path, (mode, uid, gid, typ, _) in list(entries.items()):
        if typ != 0o120000:
            continue
        target = symlink_target(img, path)
        entries[path] = (mode, uid, gid, typ, target)

    return entries


def self_check(entries: dict[str, tuple[int, int, int, int, str]]) -> None:
    # Drop lost+found (and any debugfs quirk variants) from the packaged set.
    cleaned = {
        p: v
        for p, v in entries.items()
        if not p.rstrip("/").endswith("lost+found") and v[3] in (
            0o100000,
            0o040000,
            0o120000,
        )
    }
    n_files = sum(1 for v in cleaned.values() if v[3] == 0o100000)
    n_dirs = sum(1 for v in cleaned.values() if v[3] == 0o040000)
    n_syms = sum(1 for v in cleaned.values() if v[3] == 0o120000)
    total = len(cleaned)

    errors: list[str] = []
    if total != EXPECTED_TOTAL:
        errors.append(f"total={total} expected {EXPECTED_TOTAL}")
    if n_files != EXPECTED_FILES:
        errors.append(f"files={n_files} expected {EXPECTED_FILES}")
    if n_dirs != EXPECTED_DIRS:
        errors.append(f"dirs={n_dirs} expected {EXPECTED_DIRS}")
    if n_syms != EXPECTED_SYMS:
        errors.append(f"syms={n_syms} expected {EXPECTED_SYMS}")

    # Regular files + dirs must be uniform 0750 0:10.
    bad_meta = [
        p
        for p, (mode, uid, gid, typ, _) in cleaned.items()
        if typ in (0o100000, 0o040000)
        and (mode != 0o750 or uid != 0 or gid != 10)
    ]
    if bad_meta:
        sample = ", ".join(bad_meta[:5])
        errors.append(f"{len(bad_meta)} file/dir entries not 0750 0:10 (e.g. {sample})")

    # Symlinks must all have targets.
    missing_tgt = [
        p for p, v in cleaned.items() if v[3] == 0o120000 and not v[4]
    ]
    if missing_tgt:
        errors.append(f"{len(missing_tgt)} symlinks missing targets")

    if errors:
        raise SystemExit("self-check failed:\n  - " + "\n  - ".join(errors))

    print(
        f"self-check ok: {total} entries "
        f"({n_files} files / {n_dirs} dirs / {n_syms} symlinks)"
    )


def write_tsv(
    entries: dict[str, tuple[int, int, int, int, str]], out: Path
) -> None:
    cleaned = {
        p: v
        for p, v in entries.items()
        if not p.rstrip("/").endswith("lost+found") and v[3] in (
            0o100000,
            0o040000,
            0o120000,
        )
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["path\tmode\tuid\tgid\ttype\tlinktarget"]
    for path in sorted(cleaned):
        mode, uid, gid, typ, link = cleaned[path]
        lines.append(f"{path}\t{mode:o}\t{uid}\t{gid}\t{typ:o}\t{link}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(cleaned)} rows)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    ap.add_argument("--offset", type=int, default=DEFAULT_OFFSET)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.image.is_file():
        raise SystemExit(f"missing image: {args.image}")

    img = image_spec(args.image, args.offset)
    print(f"walking {img} ...", flush=True)
    entries = walk(img)
    self_check(entries)
    write_tsv(entries, args.out)


if __name__ == "__main__":
    main()
