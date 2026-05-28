#!/usr/bin/env python3
"""
Patch a Cap .cap bundle with externally captured audio, aligned to Cap's recording window.

Routes around Cap 0.4.84 bugs (GitHub #1740):
  - mic-stream lifecycle race (2nd+ recording silently has no mic)
  - muxer -67 finalize failure with system-audio enabled

The sidecar mic captures a window WIDER than Cap's recording (started before Cap,
stopped after). This script trims the sidecar audio to align with Cap's recording
window using wall-clock birth-times, then writes the trimmed audio as Opus/.ogg —
the format Cap's recovery path produces, so Cap won't re-transcode it.

Cap signals "this bundle has mic audio" via:
  1. content/segments/segment-0/audio-input.ogg present (>500 bytes)
  2. segments[0].mic = {path, start_time} in recording-meta.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_latest_bundle(recordings_dir: Path) -> Path:
    bundles = sorted(recordings_dir.glob("*.cap"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not bundles:
        sys.exit(f"no .cap bundles found in {recordings_dir}")
    return bundles[0]


def ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ]).decode().strip()
    return float(out)


def birth_time(path: Path) -> float:
    """macOS APFS birth-time. Falls back to mtime on filesystems without it."""
    st = path.stat()
    return getattr(st, "st_birthtime", st.st_mtime)


_ENCODER_INIT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z\s.*Initialized segmented video encoder",
    re.MULTILINE,
)


def parse_recording_start(bundle: Path) -> float | None:
    """Wall-clock epoch when video encoding actually started.

    The bundle directory is created the moment the user clicks record, but
    Cap then shows a 3-second countdown plus ~500ms pipeline init before
    video encoding actually begins. The recording-logs.log captures the
    encoder-init line — that's the true t=0 for the video timeline.
    """
    log = bundle / "recording-logs.log"
    if not log.is_file():
        return None
    m = _ENCODER_INIT_RE.search(log.read_text())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def compute_offset(bundle: Path, audio_src: Path) -> float:
    """Seconds into the sidecar audio where Cap's recording actually began."""
    sidecar_start = birth_time(audio_src)
    cap_start = parse_recording_start(bundle)
    if cap_start is None:
        # Fallback: bundle btime + a conservative 3s for the default countdown.
        # Less accurate (no 500ms pipeline-init margin), but works without the log.
        cap_start = birth_time(bundle) + 3.0
    offset = cap_start - sidecar_start
    return max(offset, 0.0)


def trim_audio_to_ogg(audio_src: Path, offset_secs: float, duration_secs: float, dest: Path) -> None:
    """
    Trim sidecar audio to [offset_secs, offset_secs + duration_secs] and write as Opus/.ogg.
    Matches Cap's recovery-path output format so Cap's editor doesn't re-transcode.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    # -ss before -i: fast seek; -t: duration. -c:a libopus -ar 48000 -ac 1: match Cap.
    # Bitrate 96k mono matches Cap's reference recordings (~172 kbps total at 48 kHz mono).
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-ss", f"{offset_secs:.3f}",
        "-i", str(audio_src),
        "-t", f"{duration_secs:.3f}",
        "-c:a", "libopus", "-ar", "48000", "-ac", "1", "-b:a", "96k",
        str(dest),
    ]
    subprocess.check_call(cmd)


def patch_bundle(bundle: Path, audio_src: Path) -> dict:
    seg0 = bundle / "content" / "segments" / "segment-0"
    if not seg0.is_dir():
        sys.exit(f"bundle has no segment-0 dir: {seg0}")

    display = seg0 / "display.mp4"
    if not display.is_file():
        sys.exit(f"bundle has no display.mp4 yet — wait for Cap finalization to finish: {display}")

    video_duration = ffprobe_duration(display)
    sidecar_duration = ffprobe_duration(audio_src)
    offset = compute_offset(bundle, audio_src)

    # If trim window extends past sidecar end, the trimmed audio will just be shorter.
    # Cap's timeline will pad with silence; harmless for narration use case.
    effective_duration = min(video_duration, max(sidecar_duration - offset, 0.0))
    if effective_duration < 0.1:
        sys.exit(
            f"computed trim window is degenerate: offset={offset:.2f}s, "
            f"sidecar_duration={sidecar_duration:.2f}s, video_duration={video_duration:.2f}s. "
            f"Did the sidecar start AFTER Cap recording ended?"
        )

    # Preserve the original full-length sidecar audio (debug + re-trim if alignment is off).
    raw_copy = bundle / f"audio-input.sidecar-raw{audio_src.suffix}"
    if not raw_copy.exists():
        raw_copy.write_bytes(audio_src.read_bytes())

    dest_audio = seg0 / "audio-input.ogg"
    # Remove any prior audio-input.* so Cap doesn't get confused by stale .m4a alongside .ogg.
    for stale in seg0.glob("audio-input.*"):
        if stale.name != "audio-input.ogg":
            stale.unlink()
    if dest_audio.exists():
        dest_audio.unlink()

    trim_audio_to_ogg(audio_src, offset, video_duration, dest_audio)

    if dest_audio.stat().st_size < 500:
        sys.exit(f"trimmed audio is <500 bytes; Cap recovery.rs:918-937 will ignore it")

    meta_path = bundle / "recording-meta.json"
    meta = json.loads(meta_path.read_text())
    if not meta.get("segments"):
        sys.exit(f"recording-meta.json has no segments")

    meta["segments"][0]["mic"] = {
        "path": "content/segments/segment-0/audio-input.ogg",
        "start_time": 0.0,
    }
    meta_path.write_text(json.dumps(meta, indent=4))

    return {
        "bundle": str(bundle),
        "video_duration_secs": round(video_duration, 3),
        "sidecar_duration_secs": round(sidecar_duration, 3),
        "trim_offset_secs": round(offset, 3),
        "effective_audio_secs": round(effective_duration, 3),
        "audio_file": str(dest_audio),
        "audio_bytes": dest_audio.stat().st_size,
        "audio_duration_secs": round(ffprobe_duration(dest_audio), 3),
        "raw_backup": str(raw_copy),
        "meta_path": str(meta_path),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audio", required=True, type=Path, help="sidecar-captured audio (any ffmpeg-readable format)")
    p.add_argument("--bundle", type=Path, help="path to .cap bundle (default: newest in Cap's recordings dir)")
    p.add_argument("--recordings-dir", type=Path,
                   default=Path.home() / "Library/Application Support/so.cap.desktop/recordings")
    args = p.parse_args()

    if not args.audio.is_file():
        sys.exit(f"audio file not found: {args.audio}")

    bundle = args.bundle or find_latest_bundle(args.recordings_dir)
    if not bundle.is_dir():
        sys.exit(f"bundle not a directory: {bundle}")

    result = patch_bundle(bundle, args.audio)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
