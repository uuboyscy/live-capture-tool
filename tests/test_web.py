import http.client
import re
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from live_transcript.audio import AudioSource
from live_transcript.transcription import TranscriptEvent
from live_transcript.web import EventHub, LiveTranscriptServer, _encode_event


class EventHubTests(unittest.TestCase):
    def test_new_subscriber_receives_status_and_transcript_history(self) -> None:
        hub = EventHub()
        hub.set_status("recording", "錄製中")
        hub.publish_transcript(TranscriptEvent(AudioSource.SYSTEM, 3, 5, "Hello"))

        subscriber, initial = hub.subscribe()
        self.addCleanup(hub.unsubscribe, subscriber)

        self.assertEqual(initial[0]["state"], "recording")
        transcript = next(item for item in initial if item["type"] == "transcript")
        self.assertEqual(transcript["text"], "Hello")
        self.assertEqual(transcript["timestamp"], "00:00:03")

    def test_clear_requires_confirmation_before_session_is_saved(self) -> None:
        hub = EventHub()
        hub.publish_session({"id": "demo", "state": "recording"})
        hub.publish_transcript(TranscriptEvent(AudioSource.SYSTEM, 3, 5, "Hello"))

        cleared, message = hub.clear_transcripts(confirmed=False)
        self.assertFalse(cleared)
        self.assertIn("尚未完成保存", message)

        cleared, _ = hub.clear_transcripts(confirmed=True)
        self.assertTrue(cleared)
        subscriber, initial = hub.subscribe()
        self.addCleanup(hub.unsubscribe, subscriber)
        self.assertFalse(any(item["type"] == "transcript" for item in initial))

    def test_saved_session_can_be_cleared_without_confirmation(self) -> None:
        hub = EventHub()
        hub.publish_session({"id": "saved", "state": "saved"})
        hub.publish_transcript(TranscriptEvent(AudioSource.SYSTEM, 1, 2, "已保存"))
        cleared, message = hub.clear_transcripts(confirmed=False)
        self.assertTrue(cleared)
        self.assertIn("已保存檔案不受影響", message)

    def test_sse_event_preserves_unicode(self) -> None:
        encoded = _encode_event({"type": "status", "state": "recording", "message": "錄製中"})
        self.assertIn("event: status", encoded.decode())
        self.assertIn("錄製中", encoded.decode())


class ServerTests(unittest.TestCase):
    def test_serves_page_and_health_check(self) -> None:
        server = LiveTranscriptServer("127.0.0.1", 0)
        server.start()
        self.addCleanup(server.stop)

        connection = http.client.HTTPConnection("127.0.0.1", server._server.server_port)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("即時逐字稿", body)
        self.assertIn('id="compactToggle"', body)
        self.assertIn('id="modelToggle"', body)
        self.assertIn('id="clearTranscript"', body)
        self.assertIn('id="resetSessionName"', body)
        self.assertIn('id="language"', body)
        self.assertIn('class="topbar"', body)
        self.assertIn('id="modalBackdrop"', body)
        self.assertIn('id="transcriptCount"', body)
        self.assertIn('id="processingNotice"', body)
        self.assertIn("正在完成剩餘辨識", body)
        self.assertIn("prefers-reduced-motion", body)
        self.assertIn("--radius-control: 12px", body)
        self.assertIn('<option value="zh">中文</option>', body)
        self.assertIn('<option value="en">英文</option>', body)
        self.assertIn('<option value="auto">Auto</option>', body)
        self.assertIn("readJsonBody", body)
        self.assertIn("登入狀態已過期", body)
        self.assertIn("const displayAudioTrack", body)
        self.assertIn("本次將繼續只錄麥克風", body)
        self.assertNotIn("__DEFAULT_LANGUAGE__", body)
        self.assertIn("liveTranscriptControlsCompact", body)
        self.assertNotIn("__MODEL_CONTROL_ENABLED__", body)
        element_ids = re.findall(r'\bid="([^"]+)"', body)
        self.assertEqual(len(element_ids), len(set(element_ids)))
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", server._server.server_port)
        connection.request("GET", "/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'{"ok":true}\n')
        connection.close()

    def test_model_control_and_confirmed_transcript_clear_endpoints(self) -> None:
        hub = EventHub("example/model")
        model = FakeModelController()
        hub.publish_session({"id": "demo", "state": "recording"})
        hub.publish_transcript(TranscriptEvent(AudioSource.SYSTEM, 1, 2, "尚未保存"))
        server = LiveTranscriptServer(
            "127.0.0.1",
            0,
            hub=hub,
            model_controller=model,
        )
        server.start()
        self.addCleanup(server.stop)
        port = server._server.server_port
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
        }

        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("POST", "/model/load", body=b"{}", headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 202)
        response.read()
        self.assertTrue(model.loaded.wait(timeout=2))
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("POST", "/model/unload", body=b"{}", headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 202)
        self.assertIn('"state": "unloading"', response.read().decode())
        model.allow_unload.set()
        self.assertTrue(model.unloaded.wait(timeout=2))
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request(
            "POST",
            "/transcript/clear",
            body=b'{"confirmed":false}',
            headers=headers,
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 409)
        self.assertIn("confirmation_required", response.read().decode())
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request(
            "POST",
            "/transcript/clear",
            body=b'{"confirmed":true}',
            headers=headers,
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('"ok": true', response.read().decode())
        connection.close()

    def test_accepts_browser_pcm_over_websocket(self) -> None:
        receiver = FakeAudioReceiver()
        server = LiveTranscriptServer("127.0.0.1", 0, audio_receiver=receiver)
        server.start()
        self.addCleanup(server.stop)
        port = server._server.server_port

        connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.addCleanup(connection.close)
        request = (
            f"GET /audio HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "\r\n"
        ).encode()
        connection.sendall(request)
        response = connection.recv(4096)
        self.assertIn(b"101 Switching Protocols", response)

        pcm_packet = bytes([AudioSource.MICROPHONE]) + struct.pack("<hh", 1, -2)
        connection.sendall(masked_client_frame(0x2, pcm_packet))
        connection.sendall(masked_client_frame(0x8, b""))

        self.assertTrue(receiver.received.wait(timeout=2))
        self.assertEqual(receiver.source, AudioSource.MICROPHONE)
        self.assertEqual(receiver.pcm, struct.pack("<hh", 1, -2))

    def test_renames_and_downloads_saved_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receiver = FakeAudioReceiver()
            receiver.download_path = Path(directory) / "renamed.md"
            receiver.download_path.write_text("# Saved")
            server = LiveTranscriptServer("127.0.0.1", 0, audio_receiver=receiver)
            server.start()
            self.addCleanup(server.stop)
            port = server._server.server_port

            body = b'{"name":"renamed"}'
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request(
                "POST",
                "/sessions/demo/name",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('"name": "renamed"', response.read().decode())
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/sessions/demo/download")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("attachment", response.getheader("Content-Disposition"))
            self.assertEqual(response.read(), b"# Saved")
            connection.close()


class FakeAudioReceiver:
    def __init__(self) -> None:
        self.received = threading.Event()
        self.source = None
        self.pcm = None
        self.download_path = None

    def open(self, _client_id: str, requested_name=None, requested_language=None):
        self.requested_language = requested_language
        return True, "", {
            "id": "demo",
            "name": requested_name or "transcript-demo",
            "state": "recording",
            "download_url": None,
            "jsonl_url": None,
        }

    def feed(self, _client_id: str, source: AudioSource, pcm: bytes) -> None:
        self.source = source
        self.pcm = pcm
        self.received.set()

    def close(self, _client_id: str) -> None:
        return

    def rename_session(self, session_id: str, requested_name: str):
        return {
            "id": session_id,
            "name": requested_name,
            "state": "saved",
            "download_url": f"/sessions/{session_id}/download",
            "jsonl_url": f"/sessions/{session_id}/download?format=jsonl",
        }

    def download(self, _session_id: str, _file_format: str):
        if self.download_path is None:
            return None
        return self.download_path, self.download_path.name


class FakeModelController:
    def __init__(self) -> None:
        self.model = "example/model"
        self.state = "unloaded"
        self.loaded = threading.Event()
        self.allow_unload = threading.Event()
        self.unloaded = threading.Event()

    def load(self) -> None:
        self.state = "loaded"
        self.loaded.set()

    def unload(self) -> None:
        self.state = "unloaded"

    def start_unload(self) -> bool:
        if self.state == "unloaded":
            return False
        self.state = "unloading"

        def finish() -> None:
            self.allow_unload.wait(timeout=2)
            self.state = "unloaded"
            self.unloaded.set()

        threading.Thread(target=finish, daemon=True).start()
        return True


def masked_client_frame(opcode: int, payload: bytes) -> bytes:
    mask = b"\x11\x22\x33\x44"
    if len(payload) >= 126:
        raise ValueError("test helper only supports short frames")
    header = bytes([0x80 | opcode, 0x80 | len(payload)])
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + masked


if __name__ == "__main__":
    unittest.main()
