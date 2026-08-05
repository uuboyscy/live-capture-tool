import AVFoundation
import Foundation
import LiveTranscriptCore

final class MicrophoneCaptureController: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let converter = PCMConverter()
    private var isRunning = false

    func requestPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { granted in
                continuation.resume(returning: granted)
            }
        }
    }

    func start(onAudio: @escaping @Sendable (RemoteAudioSource, Data) -> Void) throws {
        guard !isRunning else { return }
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(
            .record,
            mode: .measurement,
            options: [.allowBluetoothHFP]
        )
        try audioSession.setActive(true)

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 4_096, format: format) {
            [converter] buffer, _ in
            do {
                if let pcm = try converter.convert(buffer) {
                    onAudio(.microphone, pcm)
                }
            } catch {
                // A later buffer may recover after an audio route change.
            }
        }
        engine.prepare()
        try engine.start()
        isRunning = true
    }

    func stop() {
        guard isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: .notifyOthersOnDeactivation
        )
        isRunning = false
    }
}
