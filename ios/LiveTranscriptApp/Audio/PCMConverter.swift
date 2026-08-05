import AVFoundation
import CoreMedia
import Foundation

final class PCMConverter: @unchecked Sendable {
    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: true
    )!
    private var converter: AVAudioConverter?
    private var inputDescription = ""

    func convert(_ sampleBuffer: CMSampleBuffer) throws -> Data? {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            return nil
        }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: description)
        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let inputBuffer = AVAudioPCMBuffer(
                pcmFormat: inputFormat,
                frameCapacity: frameCount
              ) else {
            return nil
        }
        inputBuffer.frameLength = frameCount
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer,
            at: 0,
            frameCount: Int32(frameCount),
            into: inputBuffer.mutableAudioBufferList
        )
        guard status == noErr else {
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: "無法讀取音訊取樣。"]
            )
        }
        return try convert(inputBuffer)
    }

    func convert(_ inputBuffer: AVAudioPCMBuffer) throws -> Data? {
        let inputFormat = inputBuffer.format
        let description = "\(inputFormat.sampleRate)-\(inputFormat.channelCount)-\(inputFormat.commonFormat.rawValue)-\(inputFormat.isInterleaved)"
        if converter == nil || description != inputDescription {
            converter = AVAudioConverter(from: inputFormat, to: targetFormat)
            inputDescription = description
        }
        guard let converter else {
            throw NSError(
                domain: "LiveTranscript",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "無法建立音訊格式轉換器。"]
            )
        }

        let ratio = targetFormat.sampleRate / inputFormat.sampleRate
        let outputCapacity = AVAudioFrameCount(
            ceil(Double(inputBuffer.frameLength) * ratio) + 256
        )
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
        if let conversionError { throw conversionError }
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
