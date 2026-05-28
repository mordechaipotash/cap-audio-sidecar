#!/usr/bin/env python3
"""
cap_sidecar_daemon.py — automatic Cap recording bypass.

Watches Cap's recordings directory. When a new .cap bundle appears (= you
clicked record in Cap), spawns the Swift audio recorder. When Cap's GLOBAL
log writes "Successfully recovered recording at <bundle>" (= finalization
done), stops the recorder and patches the bundle.

Note: Cap writes its finalization-complete signal to its *global* log at
~/Library/Logs/so.cap.desktop/cap-desktop.log.YYYY-MM-DD, not to the
per-bundle recording-logs.log. We tail the global log and match by the
bundle path that appears in the line.

Designed to run via launchd. No external Python dependencies — uses polling
rather than fsevents to keep the install footprint to zero.

State:
  ~/.cap-sidecar/auto/   — per-recording sidecar audio files (auto-named)
  stderr (captured by launchd) — operational log
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORDER = HERE / "cap_audio_rec_swift"
PATCH = HERE / "cap_bundle_patch.py"
RECORDINGS_DIR = Path.home() / "Library/Application Support/so.cap.desktop/recordings"
GLOBAL_LOG_DIR = Path.home() / "Library/Logs/so.cap.desktop"
AUDIO_DIR = Path.home() / ".cap-sidecar/auto"

# Cap fires this in the global log immediately after finalization completes.
# The quoted path inside lets us match against an active recording unambiguously.
END_MARKER_RE = re.compile(r'Successfully recovered recording at "([^"]+\.cap)"')

POLL_INTERVAL_SECS = 1.0
POST_END_SETTLE_SECS = 1.0  # let Cap's recovery flush before we patch
MAX_RECORDING_SECS = 14400  # 4 hours — sanity bound

log = logging.getLogger("cap-sidecar")


class Recording:
    """One in-flight recording: sidecar process + bundle path."""

    def __init__(self, bundle: Path):
        self.bundle = bundle
        self.audio_path = AUDIO_DIR / f"{bundle.stem}.m4a"
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.started_at = time.time()
        self._finalized = False

    def start(self) -> None:
        log.info("start  bundle=%s audio=%s", self.bundle.name, self.audio_path.name)
        self.proc = subprocess.Popen(
            [str(RECORDER), str(self.audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

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
        # toggle on, the bundle already has audio-input.{m4a,ogg}; bail.
        seg0 = self.bundle / "content/segments/segment-0"
        existing = [
            p for p in seg0.glob("audio-input.*")
            if not p.name.endswith(".sidecar-raw.m4a") and p.stat().st_size > 1000
        ]
        if existing:
            log.warning(
                "skip   bundle=%s reason=cap_already_has_audio (%s) — set Cap Mic+SystemAudio OFF",
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
        # Ignore bundles that existed when we started — they're either finished
        # already or were in-flight when the daemon crashed. Don't re-process.
        self.known: set[str] = set()
        if RECORDINGS_DIR.exists():
            self.known = {p.name for p in RECORDINGS_DIR.glob("*.cap")}
        log.info("init   known_bundles=%d", len(self.known))

        # Seek to end of current global log on startup so we don't fire on
        # old "Successfully recovered" lines for already-processed bundles.
        self._global_log_path: Path | None = None
        self._global_log_pos = 0
        self._refresh_global_log_path(seek_to_end=True)

    def _refresh_global_log_path(self, seek_to_end: bool = False) -> None:
        """Resolve today's global Cap log path. Handles date-rollover."""
        candidate = GLOBAL_LOG_DIR / f"cap-desktop.log.{date.today()}"
        if candidate != self._global_log_path:
            self._global_log_path = candidate
            if seek_to_end and candidate.exists():
                self._global_log_pos = candidate.stat().st_size
            else:
                self._global_log_pos = 0

    def _scan_global_log(self) -> None:
        """Tail the global Cap log for finalization lines."""
        self._refresh_global_log_path(seek_to_end=False)
        if self._global_log_path is None or not self._global_log_path.exists():
            return
        try:
            with self._global_log_path.open() as f:
                f.seek(self._global_log_pos)
                chunk = f.read()
                self._global_log_pos = f.tell()
        except OSError:
            return
        if not chunk:
            return
        for m in END_MARKER_RE.finditer(chunk):
            bundle_path = Path(m.group(1))
            name = bundle_path.name
            rec = self.active.get(name)
            if rec is None:
                continue
            time.sleep(POST_END_SETTLE_SECS)
            rec.finalize()
            del self.active[name]

    def _scan_new_bundles(self) -> None:
        if not RECORDINGS_DIR.exists():
            return
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

    def _evict_stale(self) -> None:
        for name, rec in list(self.active.items()):
            if rec.is_stale():
                log.warning("stale  bundle=%s (>%ds), force-finalizing", name, MAX_RECORDING_SECS)
                rec.finalize()
                del self.active[name]

    def run(self) -> None:
        log_name = self._global_log_path.name if self._global_log_path else "?"
        log.info("start  watching=%s log=%s", RECORDINGS_DIR.name, log_name)
        while True:
            try:
                self._scan_new_bundles()
                self._scan_global_log()
                self._evict_stale()
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

    for required in (RECORDER, PATCH):
        if not required.exists():
            log.error("missing required file: %s", required)
            sys.exit(1)
    if not os.access(RECORDER, os.X_OK):
        log.error("recorder not executable: %s — compile with: swiftc -O cap_audio_rec.swift -o %s", RECORDER, RECORDER.name)
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
