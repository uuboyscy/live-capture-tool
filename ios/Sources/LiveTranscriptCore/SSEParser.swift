import Foundation

public struct SSEEvent: Equatable, Sendable {
    public let name: String
    public let data: Data
    public let identifier: String?

    public init(name: String, data: Data, identifier: String? = nil) {
        self.name = name
        self.data = data
        self.identifier = identifier
    }
}

public struct SSEParser: Sendable {
    private var buffer = Data()

    public init() {}

    public mutating func append(_ data: Data) -> [SSEEvent] {
        buffer.append(data)
        var events: [SSEEvent] = []
        while let boundary = nextBoundary() {
            let frame = buffer.subdata(in: buffer.startIndex..<boundary.lowerBound)
            buffer.removeSubrange(buffer.startIndex..<boundary.upperBound)
            if let event = parse(frame) {
                events.append(event)
            }
        }
        return events
    }

    private func nextBoundary() -> Range<Data.Index>? {
        let lf = buffer.range(of: Data([0x0A, 0x0A]))
        let crlf = buffer.range(of: Data([0x0D, 0x0A, 0x0D, 0x0A]))
        return switch (lf, crlf) {
        case let (left?, right?): left.lowerBound <= right.lowerBound ? left : right
        case let (left?, nil): left
        case let (nil, right?): right
        case (nil, nil): nil
        }
    }

    private func parse(_ frame: Data) -> SSEEvent? {
        let text = String(decoding: frame, as: UTF8.self)
        var name = "message"
        var identifier: String?
        var dataLines: [String] = []

        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.last == "\r" ? rawLine.dropLast() : rawLine[...]
            if line.hasPrefix(":") { continue }
            let pieces = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
            let field = String(pieces[0])
            var value = pieces.count > 1 ? String(pieces[1]) : ""
            if value.hasPrefix(" ") { value.removeFirst() }
            switch field {
            case "event": name = value
            case "data": dataLines.append(value)
            case "id": identifier = value
            default: break
            }
        }

        guard !dataLines.isEmpty else { return nil }
        return SSEEvent(
            name: name,
            data: Data(dataLines.joined(separator: "\n").utf8),
            identifier: identifier
        )
    }
}
