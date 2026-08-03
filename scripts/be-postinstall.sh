#!/bin/sh
# Restore jukebox library after @be/be OTA extract.
set -e
JUKEBOX_MUSIC="/opt/jibo/Jibo/Skills/@be/be/node_modules/@be/jukebox/music"
JUKEBOX_MUSIC_STASH="/opt/tmp/jukebox-music"
if [ -d "$JUKEBOX_MUSIC_STASH" ]; then
  mkdir -p "$(dirname "$JUKEBOX_MUSIC")"
  rm -rf "$JUKEBOX_MUSIC"
  mv "$JUKEBOX_MUSIC_STASH" "$JUKEBOX_MUSIC"
  echo "restored jukebox music"
else
  echo "no stashed jukebox music"
fi
# Match update-beam.sh permission habit for Skills tree
chmod -R a+rwX /opt/jibo/Jibo/Skills/@be/be 2>/dev/null || true
exit 0
