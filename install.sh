#!/bin/bash
# install.sh — set up cap-audio-sidecar.
#
# Usage:
#   ./install.sh           # compile recorder, install scripts; no daemon
#   ./install.sh --auto    # also install the launchd auto-daemon
#
# Verbs printed in dry-run style so you can see what each step does.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$REPO_DIR/src"
STATE_DIR="$HOME/.cap-sidecar"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.cap-sidecar.daemon.plist"
DAEMON_LABEL="com.cap-sidecar.daemon"

want_auto=false
for arg in "$@"; do
  case "$arg" in
    --auto) want_auto=true ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 64 ;;
  esac
done

step() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m %s\n" "$*" >&2; }

step "checking prerequisites"
command -v swiftc >/dev/null || { warn "swiftc not found — install Xcode Command Line Tools: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { warn "python3 not found — macOS 12+ ships it by default"; exit 1; }
command -v ffmpeg  >/dev/null || warn "ffmpeg not found — required only for the patch step (libopus encode). Install with: brew install ffmpeg"

step "compiling Swift recorder"
swiftc -O "$SRC_DIR/cap_audio_rec.swift" -o "$SRC_DIR/cap_audio_rec_swift"
chmod +x "$SRC_DIR/cap_audio_rec_swift"
echo "    built: $SRC_DIR/cap_audio_rec_swift"

step "ensuring state directory exists"
mkdir -p "$STATE_DIR/auto"
echo "    state at: $STATE_DIR"

step "ensuring scripts are executable"
chmod +x "$SRC_DIR"/*.sh "$SRC_DIR"/*.py

if [[ "$want_auto" == "true" ]]; then
  step "installing launchd auto-daemon"
  if launchctl print "gui/$(id -u)/$DAEMON_LABEL" >/dev/null 2>&1; then
    echo "    daemon already loaded; bootout first to reinstall"
    launchctl bootout "gui/$(id -u)/$DAEMON_LABEL" 2>/dev/null || true
  fi
  sed -e "s|__INSTALL_DIR__|$REPO_DIR|g" \
      -e "s|__STATE_DIR__|$STATE_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      "$REPO_DIR/launchd/com.cap-sidecar.daemon.plist.template" > "$LAUNCH_AGENT"
  launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"
  echo "    daemon installed and started"
  echo "    logs:  tail -F $STATE_DIR/daemon.{out,err}"
  echo "    stop:  launchctl bootout \"gui/\$(id -u)/$DAEMON_LABEL\""
fi

step "done"
cat <<EOF

Manual workflow (no daemon):
  $SRC_DIR/cap_sidecar.sh start
  # record in Cap (Mic OFF, System Audio OFF in Cap settings)
  $SRC_DIR/cap_sidecar.sh stop-and-patch

Auto workflow (daemon, if you ran with --auto):
  Just click record in Cap. The daemon does the rest.

Cap settings you must set ONCE:
  Cap > Settings > Recording > Microphone OFF
  Cap > Settings > Recording > System Audio OFF
  (Cap captures video only; we own the audio layer.)
EOF
