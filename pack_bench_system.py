#!/usr/bin/env python3
"""Pack ~/BEnch/usr/local into a stock-faithful services OTA package.

Builds updates/packages/bench-services.tar from a complete /usr/local tree,
rewriting ownership/modes from updates/services-reference.tsv, restoring the
22 ASR symlinks that the copy dereferenced, and merging services rows into
updates/manifest.json without clobbering skills.

toVersion is read from bin/jibo-service-version in the bench tree (you stamp
that binary yourself; this packer does not rewrite it). Use
tools/stamp_service_version.py to patch the ELF in place.
"""

from __future__ import annotations

import argparse
import bz2
import io
import json
import re
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from make_package import build_package  # noqa: E402

FROM_VERSIONS_PATH = ROOT / "updates" / "from-versions.json"
REFERENCE_PATH = ROOT / "updates" / "services-reference.tsv"
PACKAGES = ROOT / "updates" / "packages"
MANIFEST = ROOT / "updates" / "manifest.json"

DEFAULT_BENCH = Path.home() / "BEnch" / "usr" / "local"
DEFAULT_FILTER = "eau,fcs"
DEFAULT_PACKAGE = "bench-services.tar"
SUBSYSTEM = "services"

VERSION_BIN = "bin/jibo-service-version"
# Stock SM regex: ^Jibo Service Version: Release-(\d+\.\d+\.\d+).*$
RELEASE_RE = re.compile(rb"Release-(\d+\.\d+\.\d+)-\d{8}")

SENTINELS = (
    "bin/jibo-system-manager",
    "bin/jibo-service-version",
    "bin/jibo-bbfw-update",
    "etc/jibo-system-manager.json",
    "var/www",
)

EXCLUDE_SUFFIXES = (
    ".openjibo-orig",
    ".openjibo.orig",
    ".pre-beam-ota",
    ".point-at-server.bak",
)


def load_from_versions() -> dict[str, str]:
    if not FROM_VERSIONS_PATH.is_file():
        return {}
    return json.loads(FROM_VERSIONS_PATH.read_text(encoding="utf-8"))


def save_from_versions(data: dict[str, str]) -> None:
    FROM_VERSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FROM_VERSIONS_PATH.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def load_reference(path: Path) -> dict[str, dict]:
    """Return relpath (no leading /) -> {mode, uid, gid, type, linktarget}."""
    if not path.is_file():
        raise SystemExit(
            f"missing {path}; run: python3 tools/p4_reference.py "
            f"--image /home/zane/jsih/ADLC.bin"
        )
    rows: dict[str, dict] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if i == 0 and line.startswith("path\t"):
            continue
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 6:
            raise SystemExit(f"bad reference row {i + 1}: {line!r}")
        raw_path, mode_s, uid_s, gid_s, type_s, link = parts
        rel = raw_path.lstrip("/")
        rows[rel] = {
            "mode": int(mode_s, 8),
            "uid": int(uid_s),
            "gid": int(gid_s),
            "type": int(type_s, 8),
            "linktarget": link,
        }
    return rows


def should_exclude(rel: str) -> bool:
    name = Path(rel).name
    if any(name.endswith(suf) for suf in EXCLUDE_SUFFIXES):
        return True
    if rel == "lost+found" or rel.startswith("lost+found/"):
        return True
    return False


def check_complete(bench: Path, reference: dict[str, dict]) -> None:
    missing_sentinels = [s for s in SENTINELS if not (bench / s).exists()]
    if missing_sentinels:
        raise SystemExit(f"missing sentinels under {bench}: {missing_sentinels}")

    missing = sorted(rel for rel in reference if not (bench / rel).exists())
    if missing:
        sample = "\n  ".join(missing[:20])
        more = f"\n  ... and {len(missing) - 20} more" if len(missing) > 20 else ""
        raise SystemExit(
            f"{len(missing)} reference paths missing under {bench}:\n  {sample}{more}"
        )


def read_service_version(path: Path) -> tuple[str, bytes]:
    """Return (X.Y.Z, full Release-X.Y.Z-YYYYMMDD literal) from the ELF."""
    data = path.read_bytes()
    matches = list(RELEASE_RE.finditer(data))
    if len(matches) != 1:
        raise SystemExit(
            f"{path}: expected exactly one Release-X.Y.Z-YYYYMMDD literal, "
            f"found {len(matches)}. Stamp it first with "
            f"tools/stamp_service_version.py"
        )
    m = matches[0]
    return m.group(1).decode("ascii"), m.group(0)


def build_filesystem_tbz(
    bench: Path,
    reference: dict[str, dict],
    outfile: Path,
) -> tuple[int, int, int]:
    """Write filesystem.tar.bz2. Returns (n_files, n_dirs, n_syms)."""
    n_files = n_dirs = n_syms = 0
    extras: list[str] = []

    outfile.parent.mkdir(parents=True, exist_ok=True)
    if outfile.exists():
        outfile.unlink()

    with tarfile.open(
        outfile, mode="w:bz2", dereference=False, format=tarfile.GNU_FORMAT
    ) as tar:
        ref_paths = sorted(reference.keys(), key=lambda p: (p.count("/"), p))
        emitted: set[str] = set()

        for rel in ref_paths:
            meta = reference[rel]
            if should_exclude(rel):
                continue
            disk = bench / rel
            arcname = "./" + rel
            typ = meta["type"]

            if typ == 0o120000:
                info = tarfile.TarInfo(name=arcname)
                info.type = tarfile.SYMTYPE
                info.linkname = meta["linktarget"]
                info.mode = meta["mode"]
                info.uid = meta["uid"]
                info.gid = meta["gid"]
                info.uname = "root"
                info.gname = "root"
                info.size = 0
                info.mtime = int(disk.lstat().st_mtime) if disk.exists() else 0
                tar.addfile(info)
                n_syms += 1
                emitted.add(rel)
                continue

            if typ == 0o040000:
                if not disk.is_dir():
                    raise SystemExit(f"reference dir is not a directory on disk: {rel}")
                info = tarfile.TarInfo(name=arcname)
                info.type = tarfile.DIRTYPE
                info.mode = meta["mode"]
                info.uid = meta["uid"]
                info.gid = meta["gid"]
                info.uname = "root"
                info.gname = "jibo"
                info.mtime = int(disk.stat().st_mtime)
                info.size = 0
                tar.addfile(info)
                n_dirs += 1
                emitted.add(rel)
                continue

            if typ == 0o100000:
                if disk.is_symlink() or not disk.is_file():
                    raise SystemExit(f"reference file missing or is symlink: {rel}")
                data = disk.read_bytes()
                info = tarfile.TarInfo(name=arcname)
                info.type = tarfile.REGTYPE
                info.mode = meta["mode"]
                info.uid = meta["uid"]
                info.gid = meta["gid"]
                info.uname = "root"
                info.gname = "jibo"
                info.mtime = int(disk.stat().st_mtime)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                n_files += 1
                emitted.add(rel)
                continue

            raise SystemExit(f"unknown reference type {oct(typ)} for {rel}")
        for path in sorted(bench.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = str(path.relative_to(bench))
            if rel in emitted or should_exclude(rel):
                continue
            extras.append(rel)
            data = path.read_bytes()
            info = tarfile.TarInfo(name="./" + rel)
            info.type = tarfile.REGTYPE
            info.mode = 0o750
            info.uid = 0
            info.gid = 10
            info.uname = "root"
            info.gname = "jibo"
            info.mtime = int(path.stat().st_mtime)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            n_files += 1

    if extras:
        print(f"included {len(extras)} extra mod file(s) not in ADLC reference:")
        for rel in extras:
            print(f"  + {rel}")

    return n_files, n_dirs, n_syms


def merge_manifest(
    *,
    from_version: str,
    to_version: str,
    package_name: str,
    filters: list[str],
    changes: str,
) -> list[dict]:
    existing: list[dict] = []
    if MANIFEST.is_file():
        existing = json.loads(MANIFEST.read_text(encoding="utf-8"))

    kept = [e for e in existing if e.get("subsystem") != SUBSYSTEM]
    pkg_id = Path(package_name).stem
    for filt in filters:
        entry_id = pkg_id if len(filters) == 1 else f"{pkg_id}-{filt}"
        kept.append(
            {
                "_id": entry_id,
                "fromVersion": from_version,
                "toVersion": to_version,
                "subsystem": SUBSYSTEM,
                "filter": filt,
                "changes": changes,
                "package": package_name,
                "dependencies": {},
            }
        )
        print(
            f"manifest: {SUBSYSTEM} {from_version} -> {to_version} "
            f"filter={filt} id={entry_id}"
        )

    MANIFEST.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST} ({len(kept)} updates)")
    return kept


def verify_inner_package(package: Path, *, to_version: str) -> None:
    with tarfile.open(package, mode="r:") as outer:
        names = set(outer.getnames())
        if "./filesystem.tar.bz2" not in names and "filesystem.tar.bz2" not in names:
            raise SystemExit(f"{package}: missing filesystem.tar.bz2")
        member = None
        for n in ("./filesystem.tar.bz2", "filesystem.tar.bz2"):
            if n in names:
                member = outer.getmember(n)
                break
        assert member is not None
        raw = outer.extractfile(member)
        assert raw is not None
        data = bz2.decompress(raw.read())

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as inner:
        infos = [m for m in inner.getmembers() if m.name not in (".", "./")]
        dirs = [m for m in infos if m.isdir()]
        syms = [m for m in infos if m.issym()]
        files = [m for m in infos if m.isreg()]
        bad_mode = [
            m
            for m in dirs + files
            if (m.mode & 0o777) != 0o750 or m.uid != 0 or m.gid != 10
        ]
        if bad_mode:
            sample = ", ".join(
                f"{m.name}:{oct(m.mode)} {m.uid}:{m.gid}" for m in bad_mode[:5]
            )
            raise SystemExit(f"inner tar metadata wrong: {sample}")
        if len(syms) != 22:
            raise SystemExit(f"expected 22 symlinks, found {len(syms)}")

        ver_member = None
        for n in ("./" + VERSION_BIN, VERSION_BIN):
            try:
                ver_member = inner.getmember(n)
                break
            except KeyError:
                continue
        if ver_member is None:
            raise SystemExit(f"missing {VERSION_BIN} in package")
        stream = inner.extractfile(ver_member)
        assert stream is not None
        blob = stream.read()
        matches = list(RELEASE_RE.finditer(blob))
        if len(matches) != 1:
            raise SystemExit(
                f"{VERSION_BIN}: expected one Release literal, found {len(matches)}"
            )
        packaged = matches[0].group(1).decode("ascii")
        if packaged != to_version:
            raise SystemExit(
                f"{VERSION_BIN}: packaged version {packaged!r} != "
                f"manifest toVersion {to_version!r}"
            )
        print(
            f"verify ok: {len(files)} files / {len(dirs)} dirs / {len(syms)} symlinks; "
            f"version from binary {matches[0].group(0)!r}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bench",
        type=Path,
        default=DEFAULT_BENCH,
        help=f"Path to usr/local tree (default: {DEFAULT_BENCH})",
    )
    ap.add_argument(
        "--to-version",
        default=None,
        help="Optional: require this X.Y.Z (must match bin/jibo-service-version). "
        "Default: read from the binary.",
    )
    ap.add_argument(
        "--filter",
        default=DEFAULT_FILTER,
        help=f"Comma-separated OTA filter flags (default: {DEFAULT_FILTER})",
    )
    ap.add_argument(
        "--package-name",
        default=DEFAULT_PACKAGE,
        help=f"Output basename under updates/packages/ (default: {DEFAULT_PACKAGE})",
    )
    ap.add_argument(
        "--advance-from",
        action="store_true",
        help="After packing, set from-versions.json services=toVersion",
    )
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="Rewrite services manifest rows from the existing package's "
        "jibo-service-version (no retar)",
    )
    ap.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip post-pack inner-tar verification",
    )
    args = ap.parse_args()

    filters = [f.strip() for f in str(args.filter).split(",") if f.strip()]
    if not filters:
        filters = ["eau", "fcs"]

    from_versions = load_from_versions()
    from_ver = from_versions.get(SUBSYSTEM, "13.0.0")
    package_name = args.package_name
    dest = PACKAGES / package_name

    if args.manifest_only:
        if not dest.is_file():
            raise SystemExit(f"missing package {dest}")
        # Read toVersion from the already-built package binary.
        import bz2 as _bz2

        with tarfile.open(dest, mode="r:") as outer:
            member = outer.getmember("./filesystem.tar.bz2")
            raw = _bz2.decompress(outer.extractfile(member).read())
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as inner:
            blob = inner.extractfile("./" + VERSION_BIN).read()
        matches = list(RELEASE_RE.finditer(blob))
        if len(matches) != 1:
            raise SystemExit(f"package {VERSION_BIN}: bad Release literal count")
        to_ver = matches[0].group(1).decode("ascii")
        if args.to_version and args.to_version != to_ver:
            raise SystemExit(
                f"--to-version {args.to_version!r} != package binary {to_ver!r}"
            )
        changes = f"BEnch services ({to_ver})"
        merge_manifest(
            from_version=from_ver,
            to_version=to_ver,
            package_name=package_name,
            filters=filters,
            changes=changes,
        )
        if args.advance_from:
            from_versions[SUBSYSTEM] = to_ver
            save_from_versions(from_versions)
            print(f"advanced {FROM_VERSIONS_PATH} services -> {to_ver}")
        print("reload running server: curl -s http://127.0.0.1:8042/reload")
        return

    bench = args.bench.resolve()
    if not bench.is_dir():
        raise SystemExit(f"missing bench tree: {bench}")
    if bench.name != "local" or bench.parent.name != "usr":
        raise SystemExit(
            f"refusing to pack {bench}: expected .../usr/local "
            "(members must be ./bin, ./etc, not ./usr/local/...)"
        )

    ver_path = bench / VERSION_BIN
    to_ver, version_literal = read_service_version(ver_path)
    if args.to_version and args.to_version != to_ver:
        raise SystemExit(
            f"--to-version {args.to_version!r} != {ver_path} reports {to_ver!r} "
            f"({version_literal!r})"
        )
    if to_ver == from_ver:
        raise SystemExit(
            f"{ver_path} still reports {to_ver}, same as from-versions.json. "
            f"Stamp a new Release literal first "
            f"(tools/stamp_service_version.py --version X.Y.Z)."
        )
    print(f"toVersion {to_ver} from {ver_path} ({version_literal!r})")
    changes = f"BEnch services ({to_ver})"

    reference = load_reference(REFERENCE_PATH)
    print(f"loaded {len(reference)} reference entries from {REFERENCE_PATH}")
    print(f"checking completeness under {bench} ...", flush=True)
    check_complete(bench, reference)

    PACKAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bench-services-", dir=str(PACKAGES)) as tmp:
        fs_tbz = Path(tmp) / "filesystem.tar.bz2"
        print(f"building {fs_tbz.name} (this takes a few minutes) ...", flush=True)
        n_files, n_dirs, n_syms = build_filesystem_tbz(bench, reference, fs_tbz)
        print(
            f"inner tar: {n_files} files / {n_dirs} dirs / {n_syms} symlinks; "
            f"{fs_tbz.stat().st_size} bytes"
        )
        print(f"wrapping outer package {dest} ...", flush=True)
        build_package(outfile=dest, filesystem_tbz=fs_tbz)

    if not args.skip_verify:
        verify_inner_package(dest, to_version=to_ver)

    merge_manifest(
        from_version=from_ver,
        to_version=to_ver,
        package_name=package_name,
        filters=filters,
        changes=changes,
    )

    if args.advance_from:
        from_versions[SUBSYSTEM] = to_ver
        save_from_versions(from_versions)
        print(f"advanced {FROM_VERSIONS_PATH} services -> {to_ver}")

    print("reload running server: curl -s http://127.0.0.1:8042/reload")

if __name__ == "__main__":
    main()
