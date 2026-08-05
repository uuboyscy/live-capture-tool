import Foundation

public enum RemoteAudioSource: UInt8, Sendable {
    case system = 0
    case microphone = 1
}

public enum AudioPacket {
    public static func encode(source: RemoteAudioSource, pcm: Data) -> Data {
        var packet = Data([source.rawValue])
        packet.append(pcm)
        return packet
    }
}
