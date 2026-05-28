# Architecture

Why this tool exists, and what we learned debugging.

## The three stacked bugs

A user on Cap v0.4.84 reporting "trip-monk" / chipmunks audio is probably hitting **three independent failures simultaneously**, not one:

### 1. Cap's mic-stream lifecycle race ([#1740](https://github.com/CapSoftware/Cap/issues/1740))

After the first recording in a Cap session, Cap tears down the mic stream. When you start the second recording, Cap kicks off the recording pipeline *before* the mic stream is rebuilt. Looking at `recording-logs.log` for a second-recording in the session:

```
segment{index=0}:screen-out:  Built pipeline ✓
segment{index=0}:camera-out:  Built pipeline ✓
                              ← no mic-out line, ever
🎤 Building stream (id 1, …)  ← mic re-init fires AFTER recording ended
editor: No audio segments found
```

**Upstream fix:** [PR #1866](https://github.com/CapSoftware/Cap/pull/1866) (merged 2026-05-26 to `main`, awaiting release).

### 2. Cap's M4A muxer finalize error -67

Even when the mic stream *is* attached, the M4A muxer fails to finalize after the first 3-second segment boundary:

```
[t=2.91s] WARN  Audio gap detected, inserting silence gap_ms=152, mic-out
[t=…]     WARN  Muxer finish failed: Unknown error: -67  (track=Microphone)
[t=…]     WARN  Muxer finish failed: Unknown error: -67  (track=SystemAudio)
[t=…]     INFO  Transcoding single mic fragment to audio-input.ogg  ← recovery path
```

Cap then falls through to a recovery transcoder that writes a distorted `.ogg`. The first 3-second segment plays cleanly; everything after that is the distortion users hear.

This bug fires for **both mic and system audio**, on both macOS and Windows (confirmed in the #1740 thread).

### 3. ffmpeg's `avfoundation` indev chipmunks bug

If you try to work around the Cap bugs by using ffmpeg to capture audio in parallel (the obvious first approach), you hit a third bug. When the built-in mic has multiple concurrent clients at different sample rates — e.g.:

- Apple Dictation (16 kHz expected)
- Screenpipe (16 kHz)
- MacWhisper (48 kHz)
- A homegrown 24/7 capture (96 kHz)

…CoreAudio aggregates everything into a shared input path. ffmpeg's `avfoundation` indev reads the device's *advertised* sample rate (typically 96 kHz on M-series MacBooks) but receives sample buffers at the *negotiated* rate (often 48 kHz). ffmpeg then resamples 96→48 by discarding samples, producing 2× speed playback — chipmunks.

**Apple's high-level `AVAudioRecorder` API** (used by MacWhisper, Voice Memos, and now this tool) doesn't have this problem — it handles the negotiation transparently. That's why those apps record cleanly while ffmpeg sounds like a chipmunk.

## Why we work around all three

This tool removes Cap from the audio path entirely:

- **Cap captures video only** (Mic and System Audio toggled OFF in Cap settings) — bypasses bugs #1 and #2 because Cap never opens the audio muxer that crashes.
- **`AVAudioRecorder` captures mic** — bypasses bug #3 because we don't use ffmpeg's avfoundation indev.
- **The patch script writes Opus mono 48 kHz** at `content/segments/segment-0/audio-input.ogg` plus the `mic` field in `recording-meta.json` — Cap's editor reads it, exports include it, cap.so receives it.

## Cap bundle schema (what we touch)

A `.cap` bundle is a directory:

```
<name>.cap/
├── content/
│   ├── segments/segment-0/
│   │   ├── display.mp4
│   │   ├── camera.mp4
│   │   ├── audio-input.ogg     ← we write this
│   │   ├── cursor.json
│   │   └── keyboard.bin
│   └── cursors/cursor_*.png
├── recording-logs.log          ← we read this for true video t=0
├── recording-meta.json         ← we patch segments[0].mic into this
├── recording-diagnostics.json
└── project-config.json
```

The single field we add to `recording-meta.json`:

```json
{
  "segments": [
    {
      "display":  { "path": "content/segments/segment-0/display.mp4", ... },
      "camera":   { "path": "content/segments/segment-0/camera.mp4",  ... },
      "mic":      { "path": "content/segments/segment-0/audio-input.ogg",
                    "start_time": 0.0 },   ← THIS
      "cursor":   "content/segments/segment-0/cursor.json",
      "keyboard": "content/segments/segment-0/keyboard.bin"
    }
  ]
}
```

That's the entire surface area. Cap's editor + export + upload code paths read this and work as if Cap captured the audio itself.

## Trim alignment

The trickiest part isn't writing the file; it's aligning the audio to the video timeline.

- `.cap` directory **birth time** = the moment you click the red record button in Cap.
- **Actual video t=0** = ~3.5 seconds later, after Cap's 3-second countdown plus ~500ms pipeline init.

We measure the offset from sidecar-start to the encoder-init line in `recording-logs.log`:

```
2026-05-28T08:52:58.572833Z  INFO …  Initialized segmented video encoder
                                     ↑ this is video t=0
```

Trim window = `[encoder_init - sidecar_start, encoder_init - sidecar_start + video_duration]`.

Earlier iterations used the bundle directory's birth time as t=0, which left audio 2-3 seconds ahead of video.

## Why we save the original .m4a in the bundle

The patch script copies the raw sidecar capture into the bundle as `audio-input.sidecar-raw.m4a`. If you ever need to re-trim or re-export with different alignment, the source data is right there. The file is excluded from Cap's editor view (Cap only looks at `audio-input.{m4a,ogg}` directly in the segment directory).
