#!/usr/bin/env python3
"""
cap_sidecar_daemon.py — automatic Cap recording bypass.

Watches Cap's recordings directory. When a new .cap bundle appears (= you
clicked record in Cap), spawns the Swift audio recorder. When the bundle's
recording-logs.log shows finalization is complete, stops the recorder and
patches the bundle. You just click record in Cap; the daemon handles the rest.

Designed to run via launchd. No external Python dependencies — uses polling
rather than fsevents to keep the install footprint to zero.

State:
  ~/.cap-sidecar/auto/   — per-recording sidecar audio files (auto-named)
  stderr (captured by launchd) — operational log

Conventions:
  - One sidecar audio per recording; named after the bundle.
  - We start the recorder the moment the .cap dir appears (before Cap's
    countdown). The patch script reads the actual encoder-init timestamp
    from recording-logs.log to align trim, so the early-start is fine.
  - We finalize when "Recording finalization completed" appears in the log.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORDER = HERE / "cap_audio_rec_swift"
PATCH = HERE / "cap_bundle_patch.py"
RECORDINGS_DIR = Path.home() / "Library/Application Support/so.cap.desktop/recordings"
AUDIO_DIR = Path.home() / ".cap-sidecar/auto"

END_MARKER = "Recording finalization completed"
POLL_INTERVAL_SECS = 1.0
POST_END_SETTLE_SECS = 1.0  # let Cap's recovery flush before we patch
MAX_RECORDING_SECS = 14400  # 4 hours — sanity bound on any single recording

log = logging.getLogger("cap-sidecar")


class Recording:
    """One in-flight recording: sidecar process + bundle + log-tail cursor."""

    def __init__(self, bundle: Path):
        self.bundle = bundle
        self.audio_path = AUDIO_DIR / f"{bundle.stem}.m4a"
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.log_pos = 0
        self.started_at = time.time()
        self._finalized = False

    def start(self) -> None:
        log.info("start  bundle=%s audio=%s", self.bundle.name, self.audio_path.name)
        self.proc = subprocess.Popen(
            [str(RECORDER), str(self.audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def end_marker_seen(self) -> bool:
        """Tail recording-logs.log; True if Cap signaled finalization complete."""
        log_file = self.bundle / "recording-logs.log"
        if not log_file.exists():
            return False
        try:
            with log_file.open() as f:
                f.seek(self.log_pos)
                chunk = f.read()
                self.log_pos = f.tell()
        except OSError:
            return False
        return END_MARKER in chunk

    def is_stale(self) -> bool:
        return (time.time() - self.started_at) > MAX_RECORDING_SECS

    def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True

        if self.proc and self.proc.poll() is None:
            log.info("stop   bundle=%s", self.bundle.name)
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("recorder did not exit cleanly; killing")
                self.proc.kill()

        if not self.audio_path.exists() or self.audio_path.stat().st_size < 1000:
            log.warning("skip   bundle=%s reason=no_audio_captured", self.bundle.name)
            return

        # Don't clobber Cap-native audio. If you accidentally left Cap's Mic
        # toggle on, the bundle will already have audio-input.{m4a,ogg}; bail.
        seg0 = self.bundle / "content/segments/segment-0"
        existing = list(seg0.glob("audio-input.*"))
        existing = [p for p in existing if not p.name.endswith(".sidecar-raw.m4a") and p.stat().st_size > 1000]
        if existing:
            log.warning(
                "skip   bundle=%s reason=cap_already_has_audio (%s) — turn off Mic+SystemAudio in Cap settings",
                self.bundle.name, ", ".join(p.name for p in existing),
            )
            return

        log.info("patch  bundle=%s", self.bundle.name)
        result = subprocess.run(
            ["python3", str(PATCH), "--bundle", str(self.bundle), "--audio", str(self.audio_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("done   bundle=%s", self.bundle.name)
        else:
            log.error("patch failed: %s", result.stderr.strip())


class Daemon:
    def __init__(self) -> None:
        self.active: dict[str, Recording] = {}
        # Ignore bundles that already exist when we start — they're either done
        # already or were in-flight when the daemon died. Don't re-patch them.
        self.known: set[str] = set()
        if RECORDINGS_DIR.exists():
            self.known = {p.name for p in RECORDINGS_DIR.glob("*.cap")}
        log.info("init   known_bundles=%d", len(self.known))

    def scan(self) -> None:
        if not RECORDINGS_DIR.exists():
            return

        # Pick up new bundles
        for path in RECORDINGS_DIR.glob("*.cap"):
            name = path.name
            if name in self.known or name in self.active:
                continue
            self.known.add(name)
            rec = Recording(path)
            try:
                rec.start()
                self.active[name] = rec
            except Exception as e:
                log.error("start failed for %s: %s", name, e)

        # Finalize completed or stale recordings
        for name, rec in list(self.active.items()):
            if rec.is_stale():
                log.warning("stale  bundle=%s (>%ds), force-finalizing", name, MAX_RECORDING_SECS)
                rec.finalize()
                del self.active[name]
            elif rec.end_marker_seen():
                time.sleep(POST_END_SETTLE_SECS)
                rec.finalize()
                del self.active[name]

    def run(self) -> None:
        log.info("start  watching=%s", RECORDINGS_DIR)
        while True:
            try:
                self.scan()
            except Exception:
                log.exception("scan error")
            time.sleep(POLL_INTERVAL_SECS)

    def shutdown(self) -> None:
        log.info("stop   active=%d", len(self.active))
        for rec in self.active.values():
            rec.finalize()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("CAP_SIDECAR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Sanity checks
    for required in (RECORDER, PATCH):
        if not required.exists():
            log.error("missing required file: %s", required)
            sys.exit(1)
    if not os.access(RECORDER, os.X_OK):
        log.error("recorder not executable: %s — run `swiftc -O cap_audio_rec.swift -o %s`", RECORDER, RECORDER.name)
        sys.exit(1)

    daemon = Daemon()

    def handle_signal(signum, _frame):
        log.info("signal %d — shutting down", signum)
        daemon.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    daemon.run()


if __name__ == "__main__":
    main()
