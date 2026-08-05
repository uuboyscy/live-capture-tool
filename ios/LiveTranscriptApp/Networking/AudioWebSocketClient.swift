import Foundation
import LiveTranscriptCore

enum AudioWebSocketError: LocalizedError {
    case unexpectedMessage
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .unexpectedMessage:
            "音訊服務沒有完成連線。"
        case let .rejected(message):
            message
        }
    }
}

actor AudioWebSocketClient {
    private let session = URLSession(configuration: .default)
    private var task: URLSessionWebSocketTask?
    private var pendingPackets: [Data] = []
    private var pendingBytes = 0
    private var isSending = false
    private let maximumPendingBytes = 1_048_576
    private var onDisconnect: (@Sendable (String) -> Void)?

    func connect(
        to url: URL,
        onDisconnect: @escaping @Sendable (String) -> Void
    ) async throws -> TranscriptSession {
        close()
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        let response = try await task.receive()
        guard case let .string(text) = response,
              let data = text.data(using: .utf8) else {
            throw AudioWebSocketError.unexpectedMessage
        }
        let message = try JSONDecoder().decode(AudioSocketMessage.self, from: data)
        if message.type == "error" {
            throw AudioWebSocketError.rejected(message.message ?? "伺服器拒絕錄音連線。")
        }
        guard message.type == "ready", let transcriptSession = message.session else {
            throw AudioWebSocketError.unexpectedMessage
        }
        self.onDisconnect = onDisconnect
        Task { await receiveMessages(from: task) }
        return transcriptSession
    }

    func enqueue(source: RemoteAudioSource, pcm: Data) {
        guard task != nil else { return }
        let packet = AudioPacket.encode(source: source, pcm: pcm)
        guard pendingBytes + packet.count <= maximumPendingBytes else {
            return
        }
        pendingPackets.append(packet)
        pendingBytes += packet.count
        guard !isSending else { return }
        isSending = true
        Task { await drainPackets() }
    }

    func close() {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        pendingPackets.removeAll(keepingCapacity: true)
        pendingBytes = 0
        isSending = false
        onDisconnect = nil
    }

    private func drainPackets() async {
        while let task, !pendingPackets.isEmpty {
            let packet = pendingPackets.removeFirst()
            pendingBytes -= packet.count
            do {
                try await task.send(.data(packet))
            } catch {
                connectionFailed(task: task, message: error.localizedDescription)
                return
            }
        }
        isSending = false
    }

    private func receiveMessages(from connectedTask: URLSessionWebSocketTask) async {
        while task === connectedTask {
            do {
                let message = try await connectedTask.receive()
                guard case let .string(text) = message,
                      let data = text.data(using: .utf8),
                      let serverMessage = try? JSONDecoder().decode(
                        AudioSocketMessage.self,
                        from: data
                      ),
                      serverMessage.type == "error" else {
                    continue
                }
                connectionFailed(
                    task: connectedTask,
                    message: serverMessage.message ?? "音訊服務中止了錄音。"
                )
                return
            } catch {
                guard task === connectedTask else { return }
                connectionFailed(task: connectedTask, message: error.localizedDescription)
                return
            }
        }
    }

    private func connectionFailed(task failedTask: URLSessionWebSocketTask, message: String) {
        guard task === failedTask else { return }
        let callback = onDisconnect
        close()
        callback?(message)
    }
}
