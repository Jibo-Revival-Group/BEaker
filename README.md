# BEaker

This is a simple public OTA server for BEam. The update catalog and package
downloads are the real service; the other API routes exist only to keep Jibo's
classic clients moving through their expected protocol calls.

It is **HEAVILY** encouraged that you do not self-host this. If you are to self-host
BEaker you get zero extra capability from using the BEam update script, and it makes
troubleshooting on the Discord quite difficult as we are unaware as to what version
of BEam or other Jibo Software you may be running. Another reason you shouldn't self-host
BEaker is that it has many environment specific code, that is specifically so that it will
run well on the 5x1 servers. There may also be a few security flaws, idk, I don't really
care. Host it if you'd like, I just don't encourage you do so.

## Compatibility no-ops

- Backup uploads are consumed and checksummed, then discarded. Backup metadata
  is kept on disk under `updates/backups/<robot>/*.meta.json` so a mid-Install
  restart still answers `Backup.List`. No backup payload is persisted, and its
  download URL is intentionally unavailable for restore.
- Media creation returns an API-shaped record but does not write the media.
  Media list, get, and remove calls return empty successful responses.
- Loop, key, notification, and robot responses remain available for client
  compatibility.

## Packages, checksums, and Cloudflare

Stock `jibo-download-update` fails with `"checksum does not match"` when the
SHA-1 of the downloaded body ≠ `shaHash` from `GetUpdateFrom`. That is different
from a `"timeout"` (120s idle abort).

`joap.5x1.com` sits behind Cloudflare. `GetUpdateFrom` is a POST (origin), but
stable `GET /packages/<name>` URLs were cacheable at the edge. After a BEnch
repack + restart, robots could get a fresh `shaHash` while still downloading a
**stale** cached tarball → worldwide checksum failures.

BEaker now:

- Serves packages with `Cache-Control: no-store` (and `Pragma: no-cache`)
- Advertises **only** content-addressed URLs: `/packages/<shaHash>/<file>`
- Rejects legacy `/packages/<file>` (404). That path let robots keep an old
  `shaHash` from Check while downloading a newer tarball after republish —
  exactly `checksum <new> != <old>` from `jibo-download-update`.

After deploying a new `bench-services.tar` (or any package) to the live host:

1. Finish the copy, then start/reload BEaker
2. **Purge Cloudflare cache** for `/packages/*` on the joap zone (existing STALE
   objects will not disappear on their own while origin is down)
3. Confirm `GET /health` shows `ok: true` and matching package sizes
4. Robots that already cached an update must **Check again** so they pick up the
   new URL/`shaHash` pair before Install

## Reload

`GET /reload` rereads selected settings from `config.json` and reloads the
manifest/package catalog, including package sizes and SHA-1 hashes. It does not
restart the process or reload Python code. Catalog reload builds the new list
fully, then swaps it in so concurrent clients never see an empty catalog.

Reload is allowed from the `192.168.0.0/16` LAN except `192.168.7.55`, the
tunnel peer. Other source addresses receive `403 Forbidden`.
