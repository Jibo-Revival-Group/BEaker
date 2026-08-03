#!/bin/sh
# Stash jukebox library before @be/be destination is wiped by OTA apply.
set -e
JUKEBOX_MUSIC="/opt/jibo/Jibo/Skills/@be/be/node_modules/@be/jukebox/music"
JUKEBOX_MUSIC_STASH="/opt/tmp/jukebox-music"
if [ -d "$JUKEBOX_MUSIC" ]; then
  mkdir -p /opt/tmp
  rm -rf "$JUKEBOX_MUSIC_STASH"
  mv "$JUKEBOX_MUSIC" "$JUKEBOX_MUSIC_STASH"
  echo "stashed jukebox music to $JUKEBOX_MUSIC_STASH"
else
  echo "no jukebox music to stash"
fi
exit 0
