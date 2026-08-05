import Foundation

public struct ServiceStatus: Codable, Equatable, Sendable {
    public let state: String
    public let message: String
}

public struct ModelStatus: Codable, Equatable, Sendable {
    public let state: String
    public let name: String?
    public let message: String
}

public struct TranscriptItem: Codable, Equatable, Identifiable, Sendable {
    public var id: String { "\(source)-\(startSeconds)-\(text)" }
    public let source: String
    public let speaker: String
    public let timestamp: String
    public let startSeconds: Double
    public let endSeconds: Double
    public let text: String
    public let detectedLanguage: String?

    enum CodingKeys: String, CodingKey {
        case source, speaker, timestamp, text
        case startSeconds = "start_seconds"
        case endSeconds = "end_seconds"
        case detectedLanguage = "detected_language"
    }
}

public struct TranscriptSession: Codable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let state: String
    public let downloadURL: String?
    public let jsonlURL: String?

    enum CodingKeys: String, CodingKey {
        case id, name, state
        case downloadURL = "download_url"
        case jsonlURL = "jsonl_url"
    }
}

public enum LiveServerEvent: Equatable, Sendable {
    case service(ServiceStatus)
    case model(ModelStatus)
    case transcript(TranscriptItem)
    case transcriptClear
    case session(TranscriptSession)
}

public enum ServerEventDecodingError: LocalizedError, Equatable {
    case unknownType(String)

    public var errorDescription: String? {
        switch self {
        case let .unknownType(type): "未知的伺服器事件：\(type)"
        }
    }
}

public enum ServerEventDecoder {
    private struct EventType: Decodable {
        let type: String?
    }

    public static func decode(eventName: String, data: Data) throws -> LiveServerEvent {
        if eventName == "transcript-clear" { return .transcriptClear }
        let decoder = JSONDecoder()
        let embeddedType = try? decoder.decode(EventType.self, from: data).type
        let type = eventName == "message" ? (embeddedType ?? eventName) : eventName
        switch type {
        case "status": return .service(try decoder.decode(ServiceStatus.self, from: data))
        case "model": return .model(try decoder.decode(ModelStatus.self, from: data))
        case "transcript": return .transcript(try decoder.decode(TranscriptItem.self, from: data))
        case "session": return .session(try decoder.decode(TranscriptSession.self, from: data))
        case "transcript-clear": return .transcriptClear
        default: throw ServerEventDecodingError.unknownType(type)
        }
    }
}

public struct AudioSocketMessage: Codable, Sendable {
    public let type: String
    public let message: String?
    public let session: TranscriptSession?
}
