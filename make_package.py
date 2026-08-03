#!/usr/bin/env python3
"""Build a Jibo OTA outer tar: filesystem.tar.bz2 [+ optional pre/postinstall]."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _stage_content(
    content_dir: Path, dest: Path, *, stamp_version: str | None
) -> None:
    """Copy skill tree, optionally rewriting top-level package.json.

    SystemManager reads the installed version from that file, so a stamped
    toVersion must land on disk. Prefer hardlinks when src/dest share a
    filesystem; on cross-device (Errno 18) fall back to a real copy after
    clearing any partial tree.
    """
    if dest.exists():
        shutil.rmtree(dest)

    try:
        shutil.copytree(
            content_dir,
            dest,
            copy_function=os.link,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
    except (OSError, shutil.Error):
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(content_dir, dest, symlinks=True)

    if stamp_version is None:
        return
    pkg_path = dest / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg["version"] = stamp_version
    # Drop hardlink (or copy) so we never mutate the source tree.
    pkg_path.unlink(missing_ok=True)
    pkg_path.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")


def build_package(
    *,
    outfile: Path,
    content_dir: Path | None = None,
    preinstall: Path | None = None,
    postinstall: Path | None = None,
    noop: bool = False,
    exclude: list[str] | None = None,
    stamp_version: str | None = None,
) -> Path:
    outfile = outfile.resolve()
    outfile.parent.mkdir(parents=True, exist_ok=True)
    if outfile.exists():
        outfile.unlink()

    # Prefer a temp dir on the same filesystem as the skill tree so hardlinks
    # work; /tmp and /var/tmp are often a different device.
    tmp_parent = None
    if content_dir is not None and stamp_version is not None:
        candidate = content_dir.resolve().parent / ".jibo-ota-tmp"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            tmp_parent = candidate
        except OSError:
            tmp_parent = outfile.parent

    with tempfile.TemporaryDirectory(prefix="jibo-ota-", dir=tmp_parent) as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()

        fs_tbz = stage / "filesystem.tar.bz2"
        if noop:
            fs_root = Path(tmp) / "fs"
            fs_root.mkdir()
            (fs_root / "README.local-ota").write_text(
                "noop package from jsih/ota-server\n", encoding="utf-8"
            )
            post = stage / "postinstall"
            post.write_text(
                "#!/bin/sh\necho 'local ota-server noop postinstall ok'\nexit 0\n",
                encoding="utf-8",
            )
            post.chmod(0o755)
            subprocess.check_call(
                ["tar", "-C", str(fs_root), "-cjf", str(fs_tbz), "."]
            )
        else:
            if content_dir is None:
                raise ValueError("content_dir required unless noop")
            content_dir = content_dir.resolve()
            if not content_dir.is_dir():
                raise FileNotFoundError(content_dir)
            pack_root = content_dir
            if stamp_version is not None:
                pack_root = Path(tmp) / "fs"
                _stage_content(content_dir, pack_root, stamp_version=stamp_version)
            cmd = ["tar", "-C", str(pack_root), "-cjf", str(fs_tbz)]
            for pat in exclude or []:
                cmd.extend(["--exclude", pat])
            cmd.append(".")
            subprocess.check_call(cmd)

        if preinstall:
            dest = stage / "preinstall"
            dest.write_bytes(preinstall.read_bytes())
            dest.chmod(0o755)
        if postinstall and not noop:
            dest = stage / "postinstall"
            dest.write_bytes(postinstall.read_bytes())
            dest.chmod(0o755)

        subprocess.check_call(["tar", "-C", str(stage), "-cf", str(outfile), "."])

    print(f"wrote {outfile} ({outfile.stat().st_size} bytes)")
    return outfile


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--content-dir",
        type=Path,
        help="Directory packed into filesystem.tar.bz2 (extracted at skill destination)",
    )
    ap.add_argument("--postinstall", type=Path, help="Optional postinstall script")
    ap.add_argument("--preinstall", type=Path, help="Optional preinstall script")
    ap.add_argument("--outfile", type=Path, required=True)
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="tar --exclude pattern (repeatable)",
    )
    ap.add_argument(
        "--noop",
        action="store_true",
        help="Build a tiny no-op package for download testing",
    )
    args = ap.parse_args()

    if not args.noop and not args.content_dir:
        ap.error("provide --content-dir or --noop")

    build_package(
        outfile=args.outfile,
        content_dir=args.content_dir,
        preinstall=args.preinstall,
        postinstall=args.postinstall,
        noop=args.noop,
        exclude=args.exclude,
    )


if __name__ == "__main__":
    main()
