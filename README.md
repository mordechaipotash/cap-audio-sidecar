# cap-audio-sidecar

> # ⚠️ RETIRED — 2026-07-26
>
> **Do not install this. Update Cap instead.**
>
> This repo existed to route around audio bugs in Cap v0.4.84. Those bugs are fixed
> upstream. [PR #1866 by @ManthanNimodiya](https://github.com/CapSoftware/Cap/pull/1866)
> (merged 2026-05-26, shipped in 0.4.85+) fixed the mic-lifecycle race, and Cap 0.5
> fixed the rest. **The sidecar is now not just unnecessary — it is harmful**, because it
> opens a second mic client alongside Cap's own working capture, which is the exact
> multi-client contamination described in [Why `AVAudioRecorder` and not `ffmpeg`](#why-avaudiorecorder-and-not-ffmpeg).
>
> Measured on Cap **0.5.6** (2026-07-26): Cap writes both `audio-input.ogg` and
> `system_audio.ogg` into the bundle itself. The daemon confirms this on every recording
> and refuses to act —
> `skip reason=cap_already_has_audio (audio-input.ogg)` — while still recording a
> throwaway shadow track first. Two months of that cost 2.3 GB of orphan `.m4a`.
>
> **If you installed the daemon, remove it:**
> ```bash
> launchctl bootout "gui/$(id -u)/com.cap-sidecar.daemon"
> rm -f ~/Library/LaunchAgents/com.cap-sidecar.daemon.plist
> # then re-enable Mic + System Audio in Cap → Settings → Recording
> ```
> Check `~/.cap-sidecar/auto/` for orphaned shadow recordings before deleting the directory.
>
> Everything below is kept as a record of the debugging, not as instructions.
> The write-up in [`docs/architecture.md`](docs/architecture.md) — three stacked bugs biting
> at once — is the part still worth reading.
>
> Thanks to [@ManthanNimodiya](https://github.com/ManthanNimodiya),
> [@richiemcilroy](https://github.com/richiemcilroy), and the
> [Cap team](https://github.com/CapSoftware/Cap) for fixing it properly upstream.

---

**A stopgap for the audio bugs in [Cap](https://cap.so) v0.4.84** ([GitHub #1740](https://github.com/CapSoftware/Cap/issues/1740)).

If your Cap recordings have any of:

- Second-and-later recordings have no mic audio
- First three seconds of audio are clean, then everything distorts ("chipmunks" / "trip-monk")
- `Muxer finish failed: Unknown error: -67` in `recording-diagnostics.json`

…this records audio outside Cap using Apple's `AVAudioRecorder` (the same API MacWhisper and Voice Memos use, which doesn't hit the bug), then patches it into Cap's `.cap` bundle before you export. cap.so upload and share work normally. You keep using Cap exactly as before.

> **First, try updating Cap.** v0.4.84 is the latest GitHub release, but Cap also auto-updates through Crabnebula's CDN — v0.4.85, v0.4.86, v0.4.87 have all shipped via that channel, and **v0.5 is rolling out now**. The mic-lifecycle bug is fixed in [PR #1866 by @ManthanNimodiya](https://github.com/CapSoftware/Cap/pull/1866) (merged 2026-05-26, included in 0.4.85+). If you have a recent Cap build, your problem may already be solved. This sidecar is for users still stuck on the GitHub-downloaded 0.4.84 binary without auto-update.
>
> **Status: ARCHIVED 2026-07-26** — the condition below was met. Cap 0.5 shipped, the muxer-67 finalize bug is fixed, and this repo is read-only. *(Original text: This will be archived once Cap **v0.5** is widely deployed and the muxer-67 finalize bug is confirmed fixed for everyone.)* Huge thanks to [@ManthanNimodiya](https://github.com/ManthanNimodiya) for the upstream PR and [@richiemcilroy](https://github.com/richiemcilroy) and the Cap team for the open source, the fast reviews, and the active 0.5 work.
>
> **Scope:** macOS only. Cap Studio mode only (Instant mode streams audio segments live to cap.so before we can patch). Tested on Cap v0.4.84.

---

## Install

```bash
git clone https://github.com/<you>/cap-audio-sidecar.git
cd cap-audio-sidecar
./install.sh             # manual mode (recommended for first run)
./install.sh --auto      # also install the launchd auto-daemon
```

You need:
- macOS (tested on Sonoma 14 / Sequoia 15)
- Xcode Command Line Tools (`xcode-select --install`) — for `swiftc`
- `python3` (ships with macOS 12+)
- `ffmpeg` for the patch step — `brew install ffmpeg`

The first time you run a recording, macOS will prompt you to grant **Microphone access to Terminal** (or whatever process invokes the recorder). Grant it.

## One-time Cap settings

Open Cap → Settings → Recording:

- **Microphone** → **OFF**
- **System Audio** → **OFF**

Cap will record video only; the sidecar owns the audio layer. (This is also what sidesteps the muxer-67 bug — Cap never touches the audio muxer that crashes.)

## Use it

### Manual mode

```bash
# 1. Quit & relaunch Cap (works around the mic-lifecycle race)
# 2. Start the sidecar:
src/cap_sidecar.sh start

# 3. Record in Cap normally (Studio mode, Display target)

# 4. Stop Cap. The editor will auto-open.
# 5. Patch the bundle:
src/cap_sidecar.sh stop-and-patch

# 6. Close + reopen the bundle in Cap. Export. Share. The cap.so video has your audio.
```

### Auto mode (daemon)

If you ran `install.sh --auto`, you just click record in Cap. The daemon detects:

- A new `.cap` directory appearing under `~/Library/Application Support/so.cap.desktop/recordings/` → starts the audio recorder
- The line `Recording finalization completed` appearing in that bundle's `recording-logs.log` → stops the recorder and patches the bundle

When Cap's editor refreshes the bundle, your audio is there.

Daemon logs: `tail -F ~/.cap-sidecar/daemon.{out,err}`

Stop the daemon: `launchctl bootout "gui/$(id -u)/com.cap-sidecar.daemon"`

Re-enable: `launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.cap-sidecar.daemon.plist`

## How it works

```
        ┌─────────────────────────────────────────────┐
        │  Cap.app  (Mic OFF, System Audio OFF)       │
        │  records video + cursor + camera only       │
        └────────────────────┬────────────────────────┘
                             │ writes
                             ▼
        ~/Library/.../recordings/<X>.cap/
            content/segments/segment-0/display.mp4
            content/segments/segment-0/camera.mp4
            recording-logs.log

   In parallel (manual or daemon-triggered):

        ┌─────────────────────────────────────────────┐
        │  cap_audio_rec_swift  (AVAudioRecorder)     │
        │  records mic → .m4a at 48kHz mono           │
        └────────────────────┬────────────────────────┘
                             │
                             ▼
        cap_bundle_patch.py
          - reads true video t=0 from recording-logs.log
          - trims sidecar audio to [t=0, t=video_duration]
          - encodes Opus mono 48kHz, writes to
              content/segments/segment-0/audio-input.ogg
          - patches recording-meta.json segments[0].mic
                             │
                             ▼
        Cap editor opens bundle → sees mic track →
        export → upload to cap.so with audio.
```

The patch is a few hundred bytes of JSON and one short Opus file dropped into the bundle's directory. Cap doesn't know the difference; the editor and upload paths are unchanged.

## Why `AVAudioRecorder` and not `ffmpeg`

`ffmpeg`'s `avfoundation` indev produces 2× speed ("chipmunks") audio when the mic has multiple concurrent clients at different sample rates (e.g., Screenpipe at 16 kHz + MacWhisper + Apple Dictation + your sidecar). It reads the device's advertised rate but receives samples at the negotiated rate. `AVAudioRecorder` (Apple's high-level API used by MacWhisper, Voice Memos) negotiates with CoreAudio transparently and avoids this.

## Why we parse `recording-logs.log` for the timestamp

Cap creates the `.cap` directory the moment you click record — about **3.5 seconds before** video encoding actually starts (3-sec countdown + ~500ms pipeline init). If we used the directory's birth time as the trim anchor, audio would be 2–3 seconds ahead of video. Parsing `Initialized segmented video encoder` from `recording-logs.log` gives the true video t=0.

## Known limitations

- **Studio mode only.** Instant mode streams audio segments to cap.so as you record; the sidecar can't intervene in time.
- **Cap audio must be OFF.** If you forget and leave Cap's Mic toggle on, the daemon detects existing `audio-input.*` in the bundle and skips patching to avoid clobbering. Manual mode will simply overwrite — turn Cap audio off.
- **macOS only.** Swift recorder uses Apple frameworks. The patch script could in principle work on a Cap bundle from any platform if your audio capture works there, but we haven't tested.
- **Cap v0.4.84.** The bundle metadata schema may change in future Cap versions. If `cap_bundle_patch.py` errors on a newer Cap, check `recording-meta.json` for schema changes.

## Architecture details

See [`docs/architecture.md`](docs/architecture.md) for the full debugging story — three stacked bugs (concurrent-mic-client contamination, Cap v0.4.84 muxer-67 finalize, ffmpeg avfoundation chipmunks) all biting at once.

## Acknowledgements

- [@ManthanNimodiya](https://github.com/ManthanNimodiya) for [PR #1866](https://github.com/CapSoftware/Cap/pull/1866) — the actual upstream fix for the mic-restore-after-recording lifecycle bug.
- [@richiemcilroy](https://github.com/richiemcilroy) and the [Cap team](https://github.com/CapSoftware/Cap) for the open-source recorder, fast PR reviews, and ongoing 0.5 work.
- The commenters in [#1740](https://github.com/CapSoftware/Cap/issues/1740) — @aspectrr, @electerious, @tembo, @Kat-May-Kat, @schuon, and others — whose symptom reports made the bug reproducible.

## License

MIT — see [LICENSE](LICENSE).
