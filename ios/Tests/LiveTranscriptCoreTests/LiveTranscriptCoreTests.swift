import Foundation
import XCTest
@testable import LiveTranscriptCore

final class LiveTranscriptCoreTests: XCTestCase {
    func testServerConfigurationBuildsHTTPAndWebSocketURLs() throws {
        let configuration = try ServerConfiguration(baseURLString: "https://example.com/")
        XCTAssertEqual(configuration.healthURL.absoluteString, "https://example.com/health")

        let socketURL = try configuration.audioWebSocketURL(
            sessionName: "Weekly sync",
            language: .automatic
        )
        let components = URLComponents(url: socketURL, resolvingAgainstBaseURL: false)
        XCTAssertEqual(components?.scheme, "wss")
        XCTAssertEqual(components?.path, "/audio")
        XCTAssertEqual(
            components?.queryItems?.first(where: { $0.name == "name" })?.value,
            "Weekly sync"
        )
        XCTAssertEqual(
            components?.queryItems?.first(where: { $0.name == "language" })?.value,
            "auto"
        )
    }

    func testServerConfigurationRejectsNonHTTPURLs() {
        XCTAssertThrowsError(try ServerConfiguration(baseURLString: "ftp://example.com")) {
            XCTAssertEqual($0 as? ServerConfigurationError, .unsupportedScheme)
        }
    }

    func testServerConfigurationPreservesAnOptionalBasePath() throws {
        let configuration = try ServerConfiguration(
            baseURLString: "https://example.com/transcription/"
        )
        XCTAssertEqual(
            configuration.eventsURL.absoluteString,
            "https://example.com/transcription/events"
        )
        XCTAssertEqual(
            configuration.resolvedURL(
                pathOrURL: "/sessions/abc/download?format=jsonl"
            )?.absoluteString,
            "https://example.com/sessions/abc/download?format=jsonl"
        )
    }

    func testAudioPacketPrefixesTheSourceByte() {
        let packet = AudioPacket.encode(source: .microphone, pcm: Data([0x12, 0x34]))
        XCTAssertEqual(packet, Data([1, 0x12, 0x34]))
    }

    func testParserHandlesChunkedUnicodeSSEFrames() throws {
        let bytes = Data("event: transcript\ndata: {\"type\":\"transcript\",\"source\":\"system\",\"speaker\":\"對方\",\"timestamp\":\"00:00:01\",\"start_seconds\":1,\"end_seconds\":2,\"text\":\"測試\",\"detected_language\":\"zh\"}\n\n".utf8)
        var parser = SSEParser()
        let split = bytes.count / 2
        XCTAssertTrue(parser.append(bytes.prefix(split)).isEmpty)
        let events = parser.append(bytes.suffix(from: split))
        XCTAssertEqual(events.count, 1)

        let decoded = try ServerEventDecoder.decode(
            eventName: try XCTUnwrap(events.first?.name),
            data: try XCTUnwrap(events.first?.data)
        )
        guard case let .transcript(item) = decoded else {
            return XCTFail("Expected transcript event")
        }
        XCTAssertEqual(item.text, "測試")
        XCTAssertEqual(item.detectedLanguage, "zh")
    }

    func testParserIgnoresKeepAliveComments() {
        var parser = SSEParser()
        XCTAssertTrue(parser.append(Data(": keep-alive\n\n".utf8)).isEmpty)
    }

    func testParserAcceptsCRLFAndDecodesSession() throws {
        var parser = SSEParser()
        let events = parser.append(
            Data("event: session\r\ndata: {\"id\":\"abc\",\"name\":\"Meeting\",\"state\":\"saved\",\"download_url\":\"/sessions/abc/download\",\"jsonl_url\":\"/sessions/abc/download?format=jsonl\"}\r\n\r\n".utf8)
        )
        XCTAssertEqual(events.count, 1)
        let decoded = try ServerEventDecoder.decode(
            eventName: try XCTUnwrap(events.first?.name),
            data: try XCTUnwrap(events.first?.data)
        )
        guard case let .session(session) = decoded else {
            return XCTFail("Expected session event")
        }
        XCTAssertEqual(session.state, "saved")
        XCTAssertEqual(session.downloadURL, "/sessions/abc/download")
    }
}
