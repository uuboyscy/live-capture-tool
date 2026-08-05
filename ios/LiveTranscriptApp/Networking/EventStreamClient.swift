import Foundation
import LiveTranscriptCore

final class EventStreamClient: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let onEvent: @Sendable (SSEEvent) -> Void
    private let onDisconnect: @Sendable (Error?) -> Void
    private let delegateQueue: OperationQueue
    private var parser = SSEParser()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var intentionallyDisconnected = false

    init(
        onEvent: @escaping @Sendable (SSEEvent) -> Void,
        onDisconnect: @escaping @Sendable (Error?) -> Void
    ) {
        self.onEvent = onEvent
        self.onDisconnect = onDisconnect
        delegateQueue = OperationQueue()
        delegateQueue.maxConcurrentOperationCount = 1
        delegateQueue.name = "LiveTranscript.EventStream"
        super.init()
    }

    func connect(to url: URL) {
        disconnect()
        intentionallyDisconnected = false
        parser = SSEParser()
        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(
            configuration: .default,
            delegate: self,
            delegateQueue: delegateQueue
        )
        self.session = session
        task = session.dataTask(with: request)
        task?.resume()
    }

    func disconnect() {
        intentionallyDisconnected = true
        task?.cancel()
        session?.invalidateAndCancel()
        task = nil
        session = nil
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            completionHandler(.cancel)
        } else {
            completionHandler(.allow)
        }
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        parser.append(data).forEach(onEvent)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        guard !intentionallyDisconnected else { return }
        onDisconnect(error)
    }
}
