# BEaker

This is a simple public OTA server for BEam. The update catalog and package
downloads are the real service; the other API routes exist only to keep Jibo's
classic clients moving through their expected protocol calls.

## Compatibility no-ops

- Backup uploads are consumed and checksummed, then discarded. Backup metadata
  is kept only in memory so the stock upload flow receives an ETag and a
  `Backup.List` response. No backup payload is persisted, and its download URL
  is intentionally unavailable for restore.
- Media creation returns an API-shaped record but does not write the media.
  Media list, get, and remove calls return empty successful responses.
- Loop, key, notification, and robot responses remain available for client
  compatibility.

## Reload

`GET /reload` rereads selected settings from `config.json` and reloads the
manifest/package catalog, including package sizes and SHA-1 hashes. It does not
restart the process or reload Python code.

Reload is allowed from the `192.168.0.0/16` LAN except `192.168.7.55`, the
tunnel peer. Other source addresses receive `403 Forbidden`.
