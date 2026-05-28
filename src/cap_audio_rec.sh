#!/bin/bash
# cap_audio_rec.sh — start/stop sidecar mic capture in parallel with Cap.
#
# Uses cap_audio_rec_swift (an AVAudioRecorder-based binary) for capture.
# ffmpeg's avfoundation backend produces 2x-speed chipmunks audio when the mic
# has multiple concurrent clients at different sample rates. AVAudioRecorder is
# the API MacWhisper and Voice Memos use, which negotiates with CoreAudio
# transparently.
#
# Usage:
#   cap_audio_rec.sh start [output_path]   # spawns recorder in background, writes PID
#   cap_audio_rec.sh stop                  # SIGINT to recorder, waits for clean finalize
#   cap_audio_rec.sh status                # is it running?
#
# Output: AAC in .m4a at 48 kHz mono — matches Cap's expected recovery format.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RECORDER="$HERE/cap_audio_rec_swift"

if [[ ! -x "$RECORDER" ]]; then
  echo "Swift recorder binary missing at $RECORDER" >&2
  echo "  Build it:  swiftc -O $HERE/cap_audio_rec.swift -o $RECORDER" >&2
  exit 1
fi

STATE_DIR="$HOME/.cap-sidecar"
mkdir -p "$STATE_DIR"
PIDFILE="$STATE_DIR/recorder.pid"
PATHFILE="$STATE_DIR/audio.path"
LOGFILE="$STATE_DIR/recorder.log"

cmd="${1:-}"

case "$cmd" in
  start)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PIDFILE"))" >&2
      exit 1
    fi
    out_path="${2:-$STATE_DIR/$(date +%Y%m%dT%H%M%S).m4a}"
    echo "$out_path" > "$PATHFILE"
    nohup "$RECORDER" "$out_path" >"$LOGFILE" 2>&1 &
    pid=$!
    echo "$pid" > "$PIDFILE"
    # Give the recorder ~400ms to fail fast on mic permission denial.
    sleep 0.4
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "recorder died at startup; tail of log:" >&2
      tail -20 "$LOGFILE" >&2
      rm -f "$PIDFILE" "$PATHFILE"
      exit 2
    fi
    echo "started pid=$pid out=$out_path"
    ;;

  stop)
    if [[ ! -f "$PIDFILE" ]]; then
      echo "not running (no pid file)" >&2
      exit 1
    fi
    pid=$(cat "$PIDFILE")
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "process $pid already gone" >&2
      rm -f "$PIDFILE"
      exit 1
    fi
    # SIGINT triggers AVAudioRecorder's clean shutdown — flushes AAC tail buffer
    # and writes the moov atom. The Swift binary sleeps 0.5s after stop() before exit.
    kill -INT "$pid"
    # Wait up to 5s for the recorder to finalize the file.
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "recorder didn't exit in 5s; force-killing" >&2
      kill -KILL "$pid" 2>/dev/null || true
    fi
    out_path=$(cat "$PATHFILE")
    rm -f "$PIDFILE" "$PATHFILE"
    if [[ ! -s "$out_path" ]]; then
      echo "output file empty or missing: $out_path" >&2
      exit 3
    fi
    echo "$out_path"
    ;;

  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PIDFILE") out=$(cat "$PATHFILE")"
    else
      echo "not running"
      exit 1
    fi
    ;;

  *)
    echo "usage: $0 {start [out_path] | stop | status}" >&2
    exit 64
    ;;
esac
