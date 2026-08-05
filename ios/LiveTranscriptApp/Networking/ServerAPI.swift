import Foundation
import LiveTranscriptCore

enum ServerAPIError: LocalizedError {
    case invalidResponse
    case requestFailed(status: Int, message: String)

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            "伺服器回傳了無法辨識的內容。"
        case let .requestFailed(status, message):
            message.isEmpty ? "伺服器請求失敗（HTTP \(status)）。" : message
        }
    }
}

struct ServerAPI {
    private struct MessageResponse: Decodable {
        let message: String?
    }

    func checkHealth(configuration: ServerConfiguration) async throws {
        let (_, response) = try await URLSession.shared.data(from: configuration.healthURL)
        try validate(response: response, data: nil)
    }

    func setModel(
        action: String,
        configuration: ServerConfiguration
    ) async throws {
        try await post(
            url: configuration.modelURL(action: action),
            body: Data("{}".utf8)
        )
    }

    func clearTranscript(
        confirmed: Bool,
        configuration: ServerConfiguration
    ) async throws {
        let body = try JSONSerialization.data(withJSONObject: ["confirmed": confirmed])
        try await post(
            url: configuration.httpURL(path: "transcript/clear"),
            body: body
        )
    }

    private func post(url: URL, body: Data) async throws {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        let (data, response) = try await URLSession.shared.data(for: request)
        try validate(response: response, data: data)
    }

    private func validate(response: URLResponse, data: Data?) throws {
        guard let response = response as? HTTPURLResponse else {
            throw ServerAPIError.invalidResponse
        }
        guard 200..<300 ~= response.statusCode else {
            let message = data.flatMap {
                try? JSONDecoder().decode(MessageResponse.self, from: $0).message
            } ?? ""
            throw ServerAPIError.requestFailed(status: response.statusCode, message: message)
        }
    }
}
