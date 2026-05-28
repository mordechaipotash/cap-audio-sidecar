// cap_audio_rec.swift — record mic to .m4a using Apple's AVAudioRecorder.
//
// Same API path MacWhisper and Voice Memos use, which negotiates with CoreAudio
// transparently and avoids the chipmunks bug that ffmpeg's avfoundation backend
// hits when the mic has multiple concurrent clients at different sample rates.
//
// Build:  swiftc -O cap_audio_rec.swift -o cap_audio_rec
// Usage:  cap_audio_rec <output.m4a>      (records until SIGINT/SIGTERM)

import AVFoundation
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: \(args[0]) <output.m4a>\n".data(using: .utf8)!)
    exit(64)
}

let outputURL = URL(fileURLWithPath: args[1])

// Match Cap's reference recordings: AAC, 48 kHz, mono, ~128 kbps. Cap's recovery
// path will accept this as-is alongside the metadata patch.
let settings: [String: Any] = [
    AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
    AVSampleRateKey: 48000,
    AVNumberOfChannelsKey: 1,
    AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
    AVEncoderBitRateKey: 128000,
]

let recorder: AVAudioRecorder
do {
    recorder = try AVAudioRecorder(url: outputURL, settings: settings)
} catch {
    FileHandle.standardError.write("AVAudioRecorder init failed: \(error)\n".data(using: .utf8)!)
    exit(2)
}

guard recorder.prepareToRecord() else {
    FileHandle.standardError.write("prepareToRecord() returned false\n".data(using: .utf8)!)
    exit(2)
}

guard recorder.record() else {
    FileHandle.standardError.write("record() returned false — likely a mic permission issue for the launching process\n".data(using: .utf8)!)
    exit(2)
}

print("recording to \(outputURL.path)")
fflush(stdout)

// SIGINT (Ctrl-C / kill -INT) finalizes the file cleanly. Default signal()
// disposition would terminate without flushing AAC frames.
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigint.setEventHandler {
    recorder.stop()
    // Give the encoder time to flush its tail buffer + write the moov atom.
    Thread.sleep(forTimeInterval: 0.5)
    exit(0)
}
sigint.resume()
signal(SIGINT, SIG_IGN)

let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigterm.setEventHandler {
    recorder.stop()
    Thread.sleep(forTimeInterval: 0.5)
    exit(0)
}
sigterm.resume()
signal(SIGTERM, SIG_IGN)

dispatchMain()
