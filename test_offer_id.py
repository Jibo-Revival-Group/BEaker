"""A republished package must get a new offer id: robots' system manager keeps a known id's old URL."""
import json, shutil, tempfile
from pathlib import Path
import server

def main():
    work = Path(tempfile.mkdtemp())
    try:
        (work / "packages").mkdir()
        (work / "manifest.json").write_text(json.dumps([{"_id": "bench-services-fcs", "package": "bench-services.tar",
            "subsystem": "services", "fromVersion": "13.0.0", "toVersion": "13.0.8", "filter": "fcs"}]))
        server.MANIFEST_PATH, server.PACKAGES_DIR = work / "manifest.json", work / "packages"
        (work / "packages" / "bench-services.tar").write_bytes(b"old")
        old = server.Catalog({"public_base_url": "https://example.invalid"}).get_from(subsystem="services", from_version="13.0.5", filt="fcs")
        (work / "packages" / "bench-services.tar").write_bytes(b"new")
        new = server.Catalog({"public_base_url": "https://example.invalid"}).get_from(subsystem="services", from_version="13.0.5", filt="fcs")
        assert old["_id"].startswith("bench-services-fcs-") and old["_id"] != new["_id"], (old["_id"], new["_id"])
        assert new["shaHash"] in new["url"] and new["_id"].endswith(new["shaHash"][:12])
        print("ok")
    finally:
        shutil.rmtree(work)

if __name__ == "__main__":
    main()
