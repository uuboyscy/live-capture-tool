import Foundation

public enum ServerConfigurationError: LocalizedError, Equatable {
    case invalidURL
    case unsupportedScheme

    public var errorDescription: String? {
        switch self {
        case .invalidURL:
            "請輸入完整的伺服器網址。"
        case .unsupportedScheme:
            "伺服器網址必須使用 https:// 或 http://。"
        }
    }
}

public enum RecognitionLanguage: String, CaseIterable, Codable, Sendable {
    case automatic = "auto"
    case chinese = "zh"
    case english = "en"

    public var displayName: String {
        switch self {
        case .automatic: "Auto"
        case .chinese: "中文"
        case .english: "English"
        }
    }
}

public struct ServerConfiguration: Equatable, Sendable {
    public let baseURL: URL

    public init(baseURLString: String) throws {
        let trimmed = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              components.host != nil else {
            throw ServerConfigurationError.invalidURL
        }
        guard scheme == "https" || scheme == "http" else {
            throw ServerConfigurationError.unsupportedScheme
        }
        components.scheme = scheme
        components.query = nil
        components.fragment = nil
        let normalizedPath = components.path.trimmingCharacters(
            in: CharacterSet(charactersIn: "/")
        )
        components.path = normalizedPath.isEmpty ? "" : "/\(normalizedPath)"
        guard let url = components.url else {
            throw ServerConfigurationError.invalidURL
        }
        baseURL = url
    }

    public func httpURL(path: String) -> URL {
        baseURL.appending(path: path.trimmingCharacters(in: CharacterSet(charactersIn: "/")))
    }

    public var healthURL: URL { httpURL(path: "health") }
    public var eventsURL: URL { httpURL(path: "events") }

    public func modelURL(action: String) -> URL {
        httpURL(path: "model/\(action)")
    }

    public func audioWebSocketURL(
        sessionName: String,
        language: RecognitionLanguage
    ) throws -> URL {
        var components = URLComponents(
            url: httpURL(path: "audio"),
            resolvingAgainstBaseURL: false
        )
        components?.scheme = baseURL.scheme == "https" ? "wss" : "ws"
        components?.queryItems = [
            URLQueryItem(name: "name", value: sessionName),
            URLQueryItem(name: "language", value: language.rawValue),
        ]
        guard let url = components?.url else {
            throw ServerConfigurationError.invalidURL
        }
        return url
    }

    public func resolvedURL(pathOrURL: String?) -> URL? {
        guard let pathOrURL, !pathOrURL.isEmpty else { return nil }
        if let absolute = URL(string: pathOrURL), absolute.scheme != nil {
            return absolute
        }
        guard let relative = URLComponents(string: pathOrURL),
              var resolved = URLComponents(
                url: baseURL,
                resolvingAgainstBaseURL: false
              ) else {
            return nil
        }
        if relative.path.hasPrefix("/") {
            resolved.path = relative.path
        } else {
            let basePath = resolved.path.trimmingCharacters(
                in: CharacterSet(charactersIn: "/")
            )
            resolved.path = "/" + [basePath, relative.path]
                .filter { !$0.isEmpty }
                .joined(separator: "/")
        }
        resolved.query = relative.query
        resolved.fragment = relative.fragment
        return resolved.url
    }
}
