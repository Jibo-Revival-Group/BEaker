#!/usr/bin/env python3
"""BEaker's copy of GitHub's answers for the 5x1 Store: python3 test_store_mirror.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from server import STORE_FULL_REFRESH as FULL_REFRESH, StoreMirror, store_headers, store_repositories as repositories

MANIFEST = {"themes": ["Astra", {"url": "http://github.com/Zane/Astra"}, "None", {"url": "."}],
            "skills": ["Bad Apple", {"url": "https://github.com/Zane/BadApple.git"}, {"name": "Other", "url": "https://example.com/m.json"}]}
assert repositories(MANIFEST) == ["zane/astra", "zane/badapple"], repositories(MANIFEST)

tags = {"zane/astra": "v1.0.3", "zane/badapple": "v2"}
api_calls: list[str] = []
api_status = 200


def fetch(url: str, follow: bool = True):
    if url.endswith("manifest.json"):
        return 200, {}, json.dumps(MANIFEST).encode()
    if url.startswith("https://github.com/"):
        name = url.split("github.com/")[1].split("/releases")[0]
        assert not follow
        return 302, {"Location": f"https://github.com/{name}/releases/tag/{tags[name]}"}, b""
    api_calls.append(url)
    if api_status != 200:
        return api_status, {}, b""
    name = url.split("/repos/")[1].split("/releases")[0]
    if url.endswith("/releases/latest"):
        return 200, {}, json.dumps({"id": 7, "tag_name": tags[name], "draft": False, "prerelease": False, "author": {"login": "x"},
                                    "assets": [{"name": "A.zip", "state": "uploaded", "browser_download_url": "https://github.com/a.zip", "size": 9}]}).encode()
    return 200, {}, json.dumps({"owner": {"login": "Zane", "id": 1}, "stargazers_count": 42, "private": False}).encode()


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "store-mirror.json"
    mirror = StoreMirror(path, fetch)
    assert mirror.get("Zane", "Astra", False) is None
    assert mirror.refresh(now=1000) == {"listed": 2, "updated": 2, "failed": 0}
    assert mirror.get("Zane", "Astra", False) == {"owner": {"login": "Zane"}, "stargazers_count": 42}, "only what BEacon reads"
    release = mirror.get("zane", "astra", True)
    assert release["tag_name"] == "v1.0.3" and release["assets"] == [{"name": "A.zip", "state": "uploaded", "browser_download_url": "https://github.com/a.zip"}]
    assert mirror.get("someone", "else", True) is None, "repositories outside the Store are not served"
    assert len(api_calls) == 4

    # Nothing changed: the next pass spends no API requests.
    assert mirror.refresh(now=1900)["updated"] == 0 and len(api_calls) == 4
    # A new release is picked up on the next pass; only that repository is asked.
    tags["zane/astra"] = "v1.0.4"
    assert mirror.refresh(now=2800)["updated"] == 1 and len(api_calls) == 6
    assert mirror.get("Zane", "Astra", True)["tag_name"] == "v1.0.4"
    # Rate limited: the old copy stays, and the pass stops asking the API.
    tags["zane/astra"] = "v1.0.5"; tags["zane/badapple"] = "v3"; api_status = 403
    assert mirror.refresh(now=3700) == {"listed": 2, "updated": 0, "failed": 2} and len(api_calls) == 8
    assert mirror.get("Zane", "Astra", True)["tag_name"] == "v1.0.4"
    api_status = 200
    assert mirror.refresh(now=3700 + 1)["updated"] == 2, "and catches up on the next pass"
    # A day later everything is asked again even with the same tags.
    assert mirror.refresh(now=3701 + FULL_REFRESH)["updated"] == 2
    # The copy survives a restart, and delisted repositories go away.
    assert StoreMirror(path, fetch).get("Zane", "BadApple", True)["tag_name"] == "v3"
    del MANIFEST["skills"]
    mirror.refresh(now=3702 + FULL_REFRESH)
    assert mirror.get("Zane", "BadApple", True) is None and mirror.get("Zane", "Astra", True)

# The GitHub token goes to GitHub's API and nowhere else.
with tempfile.TemporaryDirectory() as tmp:
    token = Path(tmp) / "github-token"
    assert "Authorization" not in store_headers("https://api.github.com/repos/a/b", token), "no token file: logged out"
    token.write_text("abc123\n")
    assert store_headers("https://api.github.com/repos/a/b", token)["Authorization"] == "Bearer abc123"
    for other in ("https://github.com/a/b/releases/latest", "https://store.5x1.com/manifest.json", "https://api.github.com.evil.test/x"):
        assert "Authorization" not in store_headers(other, token), other
print("PASS store mirror")
