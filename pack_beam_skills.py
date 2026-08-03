#!/usr/bin/env python3
"""Pack BEam skills into ota-server Update packages + manifest.

Reads installed-version targets from updates/from-versions.json so robots
can discover packages via System Manager the same way as stock OTA.
"""

from __future__ import annotations

import argparse
import bz2
import json
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from make_package import build_package  # noqa: E402

FROM_VERSIONS_PATH = ROOT / "updates" / "from-versions.json"
PACKAGES = ROOT / "updates" / "packages"
MANIFEST = ROOT / "updates" / "manifest.json"
SCRIPTS = ROOT / "scripts"
# Comma-separated OTA filter flags published on every package (prefix-matched).
DEFAULT_FILTER = "eau,fcs"


def skills_root() -> Path:
    home = Path.home()
    for name in ("BEam-Skills", "BEast-Skills", "BEam"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / "BEam-Skills"


BEAM = skills_root()

SKILL_SPECS = [
    {
        "subsystem": "@be/be",
        "path": BEAM / "@be" / "be",
        "package": "beam-be.tar",
        "preinstall": SCRIPTS / "be-preinstall.sh",
        "postinstall": SCRIPTS / "be-postinstall.sh",
        "exclude": [".git", ".gitignore", "*.map"],
        "changes": "BEam @be/be",
    },
    {
        "subsystem": "oobe-config",
        "path": BEAM / "oobe-config",
        "package": "beam-oobe-config.tar",
        "exclude": [".git", ".gitignore", "*.map"],
        "changes": "BEam oobe-config",
    },
    {
        "subsystem": "jibo-diagnostics",
        "path": BEAM / "jibo-diagnostics",
        "package": "beam-jibo-diagnostics.tar",
        "exclude": [".git", ".gitignore", "*.map"],
        "changes": "BEam jibo-diagnostics",
    },
    {
        "subsystem": "jibo-tbd",
        "path": BEAM / "jibo-tbd",
        "package": "beam-jibo-tbd.tar",
        "exclude": [".git", ".gitignore"],
        "changes": "BEam jibo-tbd",
    },
    {
        "subsystem": "fin-goods-test",
        "path": BEAM / "fin-goods-test",
        "package": "beam-fin-goods-test.tar",
        "exclude": [".git", ".gitignore", "*.map"],
        "changes": "BEam fin-goods-test",
    },
]


def pkg_version(skill_dir: Path) -> str:
    data = json.loads((skill_dir / "package.json").read_text(encoding="utf-8"))
    return str(data["version"])


def load_from_versions() -> dict[str, str]:
    if not FROM_VERSIONS_PATH.is_file():
        return {}
    return json.loads(FROM_VERSIONS_PATH.read_text(encoding="utf-8"))


def save_from_versions(data: dict[str, str]) -> None:
    FROM_VERSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FROM_VERSIONS_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


BEAM_BUILD_RE = re.compile(r"^(?P<base>.+?)\+beam(?:\.(?P<n>\d+))?$")


def to_version(from_version: str, pkg_ver: str) -> str:
    """Version the robot will report once this package is installed.

    SystemManager checks skills by reading the installed package.json, so a
    toVersion the robot can never report means the same update is offered on
    every check, forever. When BEam's own version matches what the robot
    already runs, mint a fresh +beam.N build id (valid semver build metadata)
    and stamp it into the packaged package.json.
    """
    match = BEAM_BUILD_RE.match(from_version)
    base = match.group("base") if match else from_version
    if pkg_ver != base:
        return pkg_ver
    previous = int(match.group("n") or 0) if match else 0
    return f"{pkg_ver}+beam.{previous + 1}"


def packaged_version(package: Path) -> str | None:
    """Version declared by the package.json inside a built package."""
    import io

    with tarfile.open(package) as outer:
        member = None
        for name in ("./filesystem.tar.bz2", "filesystem.tar.bz2"):
            try:
                member = outer.getmember(name)
                break
            except KeyError:
                continue
        if member is None:
            return None
        stream = outer.extractfile(member)
        if stream is None:
            return None
        raw = bz2.decompress(stream.read())
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as inner:
            for name in ("./package.json", "package.json"):
                try:
                    body = inner.extractfile(name)
                except KeyError:
                    continue
                if body is None:
                    continue
                return json.load(body).get("version")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--filter",
        default=DEFAULT_FILTER,
        help="Comma-separated OTA filter flags (default: eau,fcs). "
        "Robots match when their otaFilter is a prefix of any listed flag.",
    )
    ap.add_argument(
        "--advance-from",
        action="store_true",
        help="After packing, set from-versions.json to each package toVersion "
        "(use after robots have installed this release, before packing the next)",
    )
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="Rewrite manifest from existing packages + from-versions (no retar)",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SUBSYSTEM",
        help="Repack just this subsystem (repeatable); others keep their existing "
        "package and are still listed in the manifest",
    )
    args = ap.parse_args()

    if not BEAM.is_dir() and not args.manifest_only:
        raise SystemExit(f"missing skills tree: {BEAM}")

    from_versions = load_from_versions()
    PACKAGES.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    next_from = dict(from_versions)

    for spec in SKILL_SPECS:
        subsystem = spec["subsystem"]
        skill_dir: Path = spec["path"]
        dest = PACKAGES / spec["package"]

        repack = not args.manifest_only and (
            not args.only or subsystem in args.only
        )

        if repack:
            if not skill_dir.is_dir():
                print(f"skip missing {skill_dir}", file=sys.stderr)
                continue
            ver = pkg_version(skill_dir)
        else:
            if not dest.is_file():
                print(f"skip missing package {dest}", file=sys.stderr)
                continue
            if skill_dir.is_dir():
                ver = pkg_version(skill_dir)
            else:
                ver = from_versions.get(subsystem, "0.0.0")

        from_ver = from_versions.get(subsystem)
        if not from_ver:
            print(
                f"warning: no fromVersion for {subsystem} in {FROM_VERSIONS_PATH}; "
                f"using package version {ver}",
                file=sys.stderr,
            )
            from_ver = ver

        to_ver = to_version(from_ver, ver)

        if repack:
            print(f"\n=== packing {subsystem} ({skill_dir}) ===")
            build_package(
                outfile=dest,
                content_dir=skill_dir,
                preinstall=spec.get("preinstall"),
                postinstall=spec.get("postinstall"),
                exclude=list(spec.get("exclude") or []),
                stamp_version=to_ver if to_ver != ver else None,
            )
            if to_ver != ver:
                stamped = packaged_version(dest)
                if stamped != to_ver:
                    raise SystemExit(
                        f"{subsystem}: packaged version {stamped!r} does not match "
                        f"advertised toVersion {to_ver!r}"
                    )
                print(f"stamped {subsystem} package.json version {ver} -> {to_ver}")
        elif to_ver != ver:
            on_disk = packaged_version(dest)
            if on_disk != to_ver:
                print(
                    f"warning: {subsystem} package on disk declares {on_disk!r} but "
                    f"the manifest advertises {to_ver!r}, so robots would be offered "
                    f"it forever. Repack with --only {subsystem}",
                    file=sys.stderr,
                )
        # Prefer package basename so @be/be -> beam-be (not beam-be-be).
        pkg_id = Path(spec["package"]).stem
        filters = [f.strip() for f in str(args.filter).split(",") if f.strip()]
        if not filters:
            filters = ["eau", "fcs"]
        for filt in filters:
            # One row per flag so stock prefix-match works for fcs and eau.
            entry_id = pkg_id if len(filters) == 1 else f"{pkg_id}-{filt}"
            entry = {
                "_id": entry_id,
                "fromVersion": from_ver,
                "toVersion": to_ver,
                "subsystem": subsystem,
                "filter": filt,
                "changes": f"{spec['changes']} ({ver})",
                "package": spec["package"],
                "dependencies": {},
            }
            manifest.append(entry)
            print(f"manifest: {subsystem} {from_ver} -> {to_ver} filter={filt} id={entry_id}")
        next_from[subsystem] = to_ver

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {MANIFEST} ({len(manifest)} updates)")

    if args.advance_from:
        save_from_versions(next_from)
        print(f"advanced {FROM_VERSIONS_PATH} to installed targets for next release")

    print("reload running server: curl -s http://127.0.0.1:8042/reload")


if __name__ == "__main__":
    main()
