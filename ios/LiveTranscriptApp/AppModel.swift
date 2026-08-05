import Foundation
import LiveTranscriptCore
import ScreenCaptureKit

enum CaptureMode: String, CaseIterable, Identifiable {
    case screen = "系統音訊"
    case microphone = "僅麥克風"

    var id: String { rawValue }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var serverURLString: String
    @Published private(set) var isConnected = false
    @Published private(set) var isConnecting = false
    @Published private(set) var serviceState = "disconnected"
    @Published private(set) var serviceMessage = "尚未連線"
    @Published private(set) var modelState = "unloaded"
    @Published private(set) var modelMessage = "模型狀態未知"
    @Published private(set) var modelName = ""
    @Published private(set) var transcripts: [TranscriptItem] = []
    @Published private(set) var currentSession: TranscriptSession?
    @Published var language: RecognitionLanguage = .automatic
    @Published var sessionName = ""
    @Published var captureMode: CaptureMode = .screen
    @Published private(set) var isStarting = false
    @Published private(set) var isCapturing = false
    @Published private(set) var isStopping = false
    @Published private(set) var isFinishing = false
    @Published var errorMessage: String?

    private let api = ServerAPI()
    private let audioSocket = AudioWebSocketClient()
    private let screenCapture = ScreenCaptureController()
    private let microphoneCapture = MicrophoneCaptureController()
    private var configuration: ServerConfiguration?
    private var eventStream: EventStreamClient?
    private var reconnectTask: Task<Void, Never>?

    init() {
        serverURLString = UserDefaults.standard.string(forKey: "serverURL") ?? ""
    }

    var canToggleModel: Bool {
        isConnected && !isCapturing && !isStarting && !isStopping && !isFinishing
    }

    var canStart: Bool {
        isConnected && modelState == "loaded" && !isStarting && !isCapturing &&
            !isStopping && !isFinishing
    }

    var markdownURL: URL? {
        configuration?.resolvedURL(pathOrURL: currentSession?.downloadURL)
    }

    var jsonlURL: URL? {
        configuration?.resolvedURL(pathOrURL: currentSession?.jsonlURL)
    }

    func connect() {
        guard !isConnecting else { return }
        do {
            let configuration = try ServerConfiguration(baseURLString: serverURLString)
            isConnecting = true
            serviceMessage = "正在連線…"
            Task {
                do {
                    try await api.checkHealth(configuration: configuration)
                    self.configuration = configuration
                    UserDefaults.standard.set(
                        configuration.baseURL.absoluteString,
                        forKey: "serverURL"
                    )
                    isConnected = true
                    isConnecting = false
                    startEventStream(configuration: configuration)
                } catch {
                    isConnected = false
                    isConnecting = false
                    serviceState = "error"
                    serviceMessage = "無法連線"
                    errorMessage = error.localizedDescription
                }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func toggleModel() {
        guard let configuration, canToggleModel else { return }
        let action = modelState == "loaded" ? "unload" : "load"
        let previousState = modelState
        let previousMessage = modelMessage
        modelState = action == "load" ? "loading" : "unloading"
        modelMessage = action == "load" ? "正在要求載入模型…" : "正在要求釋放模型…"
        Task {
            do {
                try await api.setModel(action: action, configuration: configuration)
            } catch {
                modelState = previousState
                modelMessage = previousMessage
                errorMessage = error.localizedDescription
            }
        }
    }

    func startRecording() {
        guard let configuration, canStart else { return }
        isStarting = true
        Task {
            do {
                let filter: SCContentFilter?
                if captureMode == .screen {
                    filter = try await screenCapture.selectFullDisplay()
                } else {
                    guard await microphoneCapture.requestPermission() else {
                        throw NSError(
                            domain: "LiveTranscript",
                            code: 2,
                            userInfo: [NSLocalizedDescriptionKey: "請在設定中允許使用麥克風。"]
                        )
                    }
                    filter = nil
                }

                let socketURL = try configuration.audioWebSocketURL(
                    sessionName: sessionName.trimmingCharacters(in: .whitespacesAndNewlines),
                    language: language
                )
                let session = try await audioSocket.connect(
                    to: socketURL,
                    onDisconnect: { [weak self] message in
                        Task { @MainActor in
                            self?.audioConnectionLost(message)
                        }
                    }
                )
                prepareForSession(session)

                let socket = audioSocket
                let audioHandler: @Sendable (RemoteAudioSource, Data) -> Void = {
                    source, pcm in
                    Task {
                        await socket.enqueue(source: source, pcm: pcm)
                    }
                }

                if let filter {
                    try await screenCapture.start(
                        filter: filter,
                        onAudio: audioHandler,
                        onUnexpectedStop: { [weak self] error in
                            Task { @MainActor in
                                self?.captureStoppedUnexpectedly(error)
                            }
                        }
                    )
                } else {
                    try microphoneCapture.start(onAudio: audioHandler)
                }
                isStarting = false
                isCapturing = true
                serviceState = "recording"
                serviceMessage = "正在傳送 iPhone 音訊"
            } catch ScreenCaptureError.cancelled {
                isStarting = false
            } catch {
                await cleanupCapture()
                isStarting = false
                errorMessage = error.localizedDescription
            }
        }
    }

    func stopRecording() {
        guard isCapturing, !isStopping else { return }
        isStopping = true
        serviceState = "processing"
        serviceMessage = "正在完成剩餘辨識並保存逐字稿"
        Task {
            if captureMode == .screen {
                await screenCapture.stop()
            } else {
                microphoneCapture.stop()
            }
            await audioSocket.close()
            isCapturing = false
            isStopping = false
            isFinishing = true
        }
    }

    func clearTranscript() {
        guard let configuration else { return }
        Task {
            do {
                try await api.clearTranscript(confirmed: true, configuration: configuration)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func prepareForSession(_ session: TranscriptSession) {
        if currentSession?.id != session.id {
            transcripts.removeAll()
        }
        currentSession = session
        sessionName = session.name
        isFinishing = false
    }

    private func cleanupCapture() async {
        microphoneCapture.stop()
        await screenCapture.stop()
        await audioSocket.close()
        isCapturing = false
        isStopping = false
    }

    private func captureStoppedUnexpectedly(_ error: Error) {
        guard isCapturing else { return }
        errorMessage = error.localizedDescription
        stopRecording()
    }

    private func audioConnectionLost(_ message: String) {
        guard isCapturing || isStarting else { return }
        errorMessage = "音訊連線已中斷：\(message)"
        if isCapturing {
            stopRecording()
        } else {
            Task {
                await cleanupCapture()
                isStarting = false
            }
        }
    }

    private func startEventStream(configuration: ServerConfiguration) {
        reconnectTask?.cancel()
        eventStream?.disconnect()
        let stream = EventStreamClient(
            onEvent: { [weak self] event in
                Task { @MainActor in
                    self?.receive(event)
                }
            },
            onDisconnect: { [weak self] _ in
                Task { @MainActor in
                    self?.eventStreamDisconnected()
                }
            }
        )
        eventStream = stream
        stream.connect(to: configuration.eventsURL)
    }

    private func receive(_ event: SSEEvent) {
        do {
            isConnected = true
            switch try ServerEventDecoder.decode(eventName: event.name, data: event.data) {
            case let .service(status):
                serviceState = status.state
                serviceMessage = status.message
            case let .model(status):
                modelState = status.state
                modelMessage = status.message
                modelName = status.name ?? modelName
            case let .transcript(item):
                if !transcripts.contains(where: { $0.id == item.id }) {
                    transcripts.append(item)
                }
            case .transcriptClear:
                transcripts.removeAll()
            case let .session(session):
                prepareForSession(session)
                isFinishing = session.state == "finishing"
                if session.state == "saved" || session.state == "error" {
                    isFinishing = false
                }
            }
        } catch {
            // Ignore forward-compatible event types until the app is updated.
        }
    }

    private func eventStreamDisconnected() {
        guard let configuration else { return }
        isConnected = false
        serviceState = "connecting"
        serviceMessage = "即時狀態重新連線中…"
        reconnectTask?.cancel()
        reconnectTask = Task {
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled else { return }
            startEventStream(configuration: configuration)
        }
    }
}
