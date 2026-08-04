import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

private enum CaptureSource: UInt8 {
    case system = 0
    case microphone = 1
}

private final class FramedWriter: @unchecked Sendable {
    private let lock = NSLock()
    private let output = FileHandle.standardOutput

    func write(source: CaptureSource, pcm: Data) {
        guard !pcm.isEmpty else { return }
        var frame = Data("LCT1".utf8)
        frame.append(source.rawValue)
        var length = UInt32(pcm.count).littleEndian
        withUnsafeBytes(of: &length) { frame.append(contentsOf: $0) }
        frame.append(pcm)

        lock.lock()
        defer { lock.unlock() }
        do {
            try output.write(contentsOf: frame)
        } catch {
            fputs("audio-capture: stdout write failed: \(error)\n", stderr)
        }
    }
}

private final class PCMConverter {
    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: true
    )!
    private var converter: AVAudioConverter?
    private var inputDescription = ""

    func convert(_ sampleBuffer: CMSampleBuffer) throws -> Data? {
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            return nil
        }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let inputBuffer = AVAudioPCMBuffer(
                pcmFormat: inputFormat,
                frameCapacity: frameCount
              ) else {
            return nil
        }
        inputBuffer.frameLength = frameCount
        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer,
            at: 0,
            frameCount: Int32(frameCount),
            into: inputBuffer.mutableAudioBufferList
        )
        guard copyStatus == noErr else {
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(copyStatus),
                userInfo: [NSLocalizedDescriptionKey: "Unable to copy PCM data"]
            )
        }

        let description = "\(inputFormat.sampleRate)-\(inputFormat.channelCount)-\(inputFormat.commonFormat.rawValue)-\(inputFormat.isInterleaved)"
        if converter == nil || description != inputDescription {
            converter = AVAudioConverter(from: inputFormat, to: targetFormat)
            inputDescription = description
        }
        guard let converter else {
            throw NSError(
                domain: "LiveCaptureTool",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Unable to create audio converter"]
            )
        }

        let ratio = targetFormat.sampleRate / inputFormat.sampleRate
        let outputCapacity = AVAudioFrameCount(ceil(Double(frameCount) * ratio) + 256)
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: outputCapacity
        ) else {
            return nil
        }

        var suppliedInput = false
        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError) {
            _, inputStatus in
            if suppliedInput {
                inputStatus.pointee = .noDataNow
                return nil
            }
            suppliedInput = true
            inputStatus.pointee = .haveData
            return inputBuffer
        }
        if let conversionError {
            throw conversionError
        }
        guard status == .haveData || status == .inputRanDry,
              outputBuffer.frameLength > 0,
              let samples = outputBuffer.int16ChannelData?.pointee else {
            return nil
        }
        return Data(
            bytes: samples,
            count: Int(outputBuffer.frameLength) * MemoryLayout<Int16>.size
        )
    }
}

private final class CaptureOutput: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let writer = FramedWriter()
    private let systemConverter = PCMConverter()
    private let microphoneConverter = PCMConverter()

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
                    writer.write(source: .system, pcm: pcm)
                }
            case .microphone:
                if let pcm = try microphoneConverter.convert(sampleBuffer) {
                    writer.write(source: .microphone, pcm: pcm)
                }
            default:
                break
            }
        } catch {
            fputs("audio-capture: conversion failed: \(error)\n", stderr)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("audio-capture: stream stopped: \(error.localizedDescription)\n", stderr)
        Darwin.exit(2)
    }
}

private final class SignalWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var sources: [DispatchSourceSignal] = []
    private var finished = false

    func wait() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            defer { lock.unlock() }
            if finished {
                continuation.resume()
                return
            }
            self.continuation = continuation
            let queue = DispatchQueue(label: "live-capture.termination")
            for signalNumber in [SIGINT, SIGTERM] {
                signal(signalNumber, SIG_IGN)
                let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: queue)
                source.setEventHandler { [weak self] in
                    self?.finish()
                }
                source.resume()
                sources.append(source)
            }
        }
    }

    private func finish() {
        lock.lock()
        guard !finished else {
            lock.unlock()
            return
        }
        finished = true
        let savedContinuation = continuation
        continuation = nil
        for source in sources {
            source.cancel()
        }
        sources.removeAll()
        lock.unlock()
        savedContinuation?.resume()
    }
}

@main
private struct AudioCaptureCLI {
    static func main() async {
        guard #available(macOS 15.0, *) else {
            fputs("audio-capture: macOS 15 or newer is required\n", stderr)
            Darwin.exit(1)
        }

        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: false
            )
            guard let display = content.displays.first else {
                throw NSError(
                    domain: "LiveCaptureTool",
                    code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "No display is available to capture"]
                )
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let configuration = SCStreamConfiguration()
            configuration.width = 2
            configuration.height = 2
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            configuration.queueDepth = 3
            configuration.capturesAudio = true
            configuration.sampleRate = 16_000
            configuration.channelCount = 1
            configuration.excludesCurrentProcessAudio = true
            configuration.captureMicrophone = true

            let output = CaptureOutput()
            let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
            let systemQueue = DispatchQueue(label: "live-capture.system-audio")
            let microphoneQueue = DispatchQueue(label: "live-capture.microphone")
            try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: systemQueue)
            try stream.addStreamOutput(output, type: .microphone, sampleHandlerQueue: microphoneQueue)
            try await stream.startCapture()

            fputs("audio-capture: capturing system audio and microphone\n", stderr)
            await SignalWaiter().wait()
            try await stream.stopCapture()
        } catch {
            fputs("audio-capture: \(error.localizedDescription)\n", stderr)
            fputs("Grant Microphone and Screen & System Audio Recording permissions in System Settings, then try again.\n", stderr)
            Darwin.exit(1)
        }
    }
}
