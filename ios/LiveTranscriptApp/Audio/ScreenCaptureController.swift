import AVFoundation
import CoreMedia
import Foundation
import LiveTranscriptCore
import ScreenCaptureKit

enum ScreenCaptureError: LocalizedError {
    case unavailable
    case pickerAlreadyPresented
    case cancelled

    var errorDescription: String? {
        switch self {
        case .unavailable:
            "此裝置目前無法使用系統音訊擷取。"
        case .pickerAlreadyPresented:
            "系統分享選擇器已經開啟。"
        case .cancelled:
            "已取消選擇分享內容。"
        }
    }
}

@available(iOS 27.0, *)
final class ScreenCaptureController: NSObject, @unchecked Sendable {
    private let systemConverter = PCMConverter()
    private let microphoneConverter = PCMConverter()
    private let screenQueue = DispatchQueue(label: "LiveTranscript.ScreenFrames")
    private let systemQueue = DispatchQueue(label: "LiveTranscript.ScreenAudio")
    private let microphoneQueue = DispatchQueue(label: "LiveTranscript.ScreenMicrophone")
    private var selectionContinuation: CheckedContinuation<SCContentFilter, Error>?
    private var stream: SCStream?
    private var onAudio: (@Sendable (RemoteAudioSource, Data) -> Void)?
    private var onUnexpectedStop: (@Sendable (Error) -> Void)?

    func selectFullDisplay() async throws -> SCContentFilter {
        guard selectionContinuation == nil else {
            throw ScreenCaptureError.pickerAlreadyPresented
        }
        let picker = SCContentSharingPicker.shared
        guard picker.isAvailable else { throw ScreenCaptureError.unavailable }

        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowsChangingSelectedContent = false
        configuration.showsMicrophoneControl = true
        picker.defaultConfiguration = configuration
        picker.isActive = true
        picker.add(self)

        return try await withCheckedThrowingContinuation { continuation in
            selectionContinuation = continuation
            picker.present()
        }
    }

    func start(
        filter: SCContentFilter,
        onAudio: @escaping @Sendable (RemoteAudioSource, Data) -> Void,
        onUnexpectedStop: @escaping @Sendable (Error) -> Void
    ) async throws {
        if stream != nil { await stop() }
        SCContentSharingPicker.shared.isActive = true
        self.onAudio = onAudio
        self.onUnexpectedStop = onUnexpectedStop

        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.capturesAudio = true
        configuration.sampleRate = 16_000
        configuration.channelCount = 1
        configuration.excludesCurrentProcessAudio = true
        configuration.captureMicrophone = filter.isMicrophoneEnabled

        if filter.isMicrophoneEnabled {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.mixWithOthers, .allowBluetoothHFP]
            )
            try audioSession.setActive(true)
        }

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        // iOS requires a screen output for every full-display SCStream. The app
        // intentionally configures 2x2, 1 fps frames and ignores them because
        // only audio is transmitted to the transcription server.
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: screenQueue)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: systemQueue)
        if filter.isMicrophoneEnabled {
            try stream.addStreamOutput(
                self,
                type: .microphone,
                sampleHandlerQueue: microphoneQueue
            )
        }
        self.stream = stream
        try await stream.startCapture()
    }

    func stop() async {
        let runningStream = stream
        self.stream = nil
        if let runningStream {
            try? await runningStream.stopCapture()
        }
        onAudio = nil
        onUnexpectedStop = nil
        SCContentSharingPicker.shared.isActive = false
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: .notifyOthersOnDeactivation
        )
    }

    private func finishSelection(with result: Result<SCContentFilter, Error>) {
        let picker = SCContentSharingPicker.shared
        picker.remove(self)
        if case .failure = result {
            picker.isActive = false
        }
        let continuation = selectionContinuation
        selectionContinuation = nil
        continuation?.resume(with: result)
    }
}

@available(iOS 27.0, *)
extension ScreenCaptureController: SCContentSharingPickerObserver {
    func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didCancelFor stream: SCStream?
    ) {
        finishSelection(with: .failure(ScreenCaptureError.cancelled))
    }

    func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didUpdateWith filter: SCContentFilter,
        for stream: SCStream?
    ) {
        finishSelection(with: .success(filter))
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        finishSelection(with: .failure(error))
    }
}

@available(iOS 27.0, *)
extension ScreenCaptureController: SCStreamOutput, SCStreamDelegate {
    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        do {
            switch outputType {
            case .audio:
                if let pcm = try systemConverter.convert(sampleBuffer) {
                    onAudio?(.system, pcm)
                }
            case .microphone:
                if let pcm = try microphoneConverter.convert(sampleBuffer) {
                    onAudio?(.microphone, pcm)
                }
            default:
                break
            }
        } catch {
            // Route changes can invalidate one sample; following buffers can recover.
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        self.stream = nil
        onUnexpectedStop?(error)
    }
}
