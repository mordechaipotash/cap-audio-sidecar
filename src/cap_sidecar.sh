#!/bin/bash
# cap_sidecar.sh — one-shot orchestrator: start audio capture, wait for Cap to finish,
# then patch the newest bundle with the captured audio.
#
# Usage:
#   cap_sidecar.sh start          # starts ffmpeg, returns immediately. Then YOU start Cap recording.
#   cap_sidecar.sh stop-and-patch # stops ffmpeg, patches the newest .cap bundle, returns path.
#
# Workflow:
#   1. cap_sidecar.sh start
#   2. open Cap, start recording (audio OFF in Cap settings)
#   3. record what you want
#   4. stop Cap recording — bundle lands in ~/Library/Application Support/so.cap.desktop/recordings/
#   5. cap_sidecar.sh stop-and-patch
#   6. open the bundle in Cap — audio is there. Export. Upload. Done.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  start)
    "$HERE/cap_audio_rec.sh" start
    echo
    echo "Now: open Cap, ensure Mic+SystemAudio are OFF in settings, and start recording."
    echo "When done, run:  $HERE/cap_sidecar.sh stop-and-patch"
    ;;

  stop-and-patch)
    audio_path=$("$HERE/cap_audio_rec.sh" stop)
    echo "captured audio: $audio_path"
    python3 "$HERE/cap_bundle_patch.py" --audio "$audio_path"
    ;;

  *)
    echo "usage: $0 {start | stop-and-patch}" >&2
    exit 64
    ;;
esac
