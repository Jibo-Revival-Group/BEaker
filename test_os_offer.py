"""The stock os and services rows are offered only to OOBE PM's `oobe` filter, and never to a robot already on 13.0.0."""
import json
import shutil
import tempfile
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parent


def main() -> None:
    work = Path(tempfile.mkdtemp())
    try:
        (work / "packages").mkdir()
        manifest = json.loads((ROOT / "updates" / "manifest.json").read_text())
        for entry in manifest:
            (work / "packages" / entry["package"]).write_bytes(b"x")
        (work / "manifest.json").write_text(json.dumps(manifest))
        server.MANIFEST_PATH, server.PACKAGES_DIR = work / "manifest.json", work / "packages"
        catalog = server.Catalog({"public_base_url": "https://example.invalid"})

        offer = catalog.get_from(subsystem="os", from_version="1.9.1", filt="oobe")
        assert offer and offer["toVersion"] == "13.0.0" and offer["fromVersion"] == "1.9.1", offer
        assert catalog.get_from(subsystem="os", from_version="13.0.0", filt="oobe") is None
        for automatic in ("fcs", "eau"):  # robots' own update checks must never see the os row
            assert catalog.get_from(subsystem="os", from_version="1.9.1", filt=automatic) is None
        services = catalog.get_from(subsystem="services", from_version="3.3.2", filt="oobe")
        assert services and services["_id"] == "stock-services-oobe", services
        for automatic in ("fcs", "eau"):  # robots' own checks still get BEnch, never stock services
            offer = catalog.get_from(subsystem="services", from_version="3.3.2", filt=automatic)
            assert offer and offer["_id"].startswith("bench-services"), offer
        print("ok")
    finally:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
