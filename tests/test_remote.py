import tempfile
import unittest
from pathlib import Path

from live_transcript.audio import AudioSource, make_pcm
from live_transcript.remote import RemoteAudioReceiver, normalized_session_name
from live_transcript.transcription import TranscriptEvent


class FakeEngine:
    def __init__(self, *, loaded=True):
        self.loaded = loaded
        self.language = None
        self.in_use = False
        self.unload_count = 0

    def load(self):
        self.loaded = True

    def begin_session(self):
        if not self.loaded or self.in_use:
            return False
        self.in_use = True
        return True

    def set_language(self, language):
        if self.in_use:
            raise RuntimeError("錄音或辨識進行中，無法變更語言")
        self.language = language

    def end_session_and_unload(self):
        self.in_use = False
        self.loaded = False
        self.unload_count += 1

    def transcribe(self, segment):
        return TranscriptEvent(
            segment.source,
            segment.start_seconds,
            segment.end_seconds,
            "遠端語音測試",
            "zh",
        )


class RemoteAudioReceiverTests(unittest.TestCase):
    def test_each_recording_is_named_saved_renamed_and_downloadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = []
            sessions = []
            statuses = []
            engine = FakeEngine()
            receiver = RemoteAudioReceiver(
                engine,
                Path(directory),
                transcripts.append,
                sessions.append,
                lambda state, message: statuses.append((state, message)),
                threshold=100,
                silence_seconds=0.1,
                max_segment_seconds=15,
            )

            accepted, _, session = receiver.open("first", "產品會議.md", "zh")
            second_accepted, message, _ = receiver.open("second")
            self.assertTrue(accepted)
            self.assertFalse(second_accepted)
            self.assertIn("另一台電腦", message)
            self.assertEqual(session["name"], "產品會議")
            self.assertEqual(engine.language, "zh")

            session = receiver.rename_session(session["id"], "產品會議 final")
            receiver.feed("first", AudioSource.MICROPHONE, make_pcm([2_000] * 8_000))
            receiver.feed("first", AudioSource.MICROPHONE, make_pcm([0] * 2_000))
            receiver.close("first")

            markdown_path, filename = receiver.download(session["id"], "markdown")
            self.assertEqual(filename, "產品會議 final.md")
            self.assertIn("遠端語音測試", markdown_path.read_text())
            self.assertEqual(len(transcripts), 1)
            self.assertEqual(statuses[0][0], "recording")
            self.assertEqual(statuses[-1][0], "waiting")
            self.assertEqual(sessions[-1]["state"], "saved")
            self.assertEqual(engine.unload_count, 1)
            self.assertFalse(engine.loaded)

            renamed = receiver.rename_session(session["id"], "客戶訪談")
            renamed_path, renamed_filename = receiver.download(
                renamed["id"], "markdown"
            )
            self.assertEqual(renamed_filename, "客戶訪談.md")
            self.assertTrue(renamed_path.exists())
            self.assertFalse(markdown_path.exists())

    def test_normalizes_unsafe_or_empty_names(self) -> None:
        self.assertEqual(normalized_session_name("../Q3: review.md", "fallback"), "-Q3- review")
        self.assertEqual(normalized_session_name("  ", "fallback"), "fallback")

    def test_stale_audio_connection_is_saved_and_releases_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = []
            engine = FakeEngine()
            receiver = RemoteAudioReceiver(
                engine,
                Path(directory),
                lambda _event: None,
                sessions.append,
                lambda _state, _message: None,
                threshold=100,
                silence_seconds=0.1,
                max_segment_seconds=15,
            )
            accepted, _, first_session = receiver.open("stale")
            self.assertTrue(accepted)
            receiver.feed("stale", AudioSource.MICROPHONE, make_pcm([0] * 100))

            expired_client = receiver.expire_stale(audio_idle_seconds=0)

            self.assertEqual(expired_client, "stale")
            self.assertEqual(sessions[-1]["state"], "saved")
            self.assertIsNotNone(
                receiver.download(first_session["id"], "markdown")
            )
            second_accepted, message, _ = receiver.open("replacement")
            self.assertFalse(second_accepted)
            self.assertIn("啟用模型", message)
            engine.load()
            second_accepted, _, _ = receiver.open("replacement")
            self.assertTrue(second_accepted)
            receiver.close("replacement")

    def test_rejects_recording_until_model_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receiver = RemoteAudioReceiver(
                FakeEngine(loaded=False),
                Path(directory),
                lambda _event: None,
                lambda _session: None,
                lambda _state, _message: None,
                threshold=100,
                silence_seconds=0.1,
                max_segment_seconds=15,
            )
            accepted, message, session = receiver.open("waiting")
            self.assertFalse(accepted)
            self.assertIn("啟用模型", message)
            self.assertIsNone(session)

    def test_rejects_unknown_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receiver = RemoteAudioReceiver(
                FakeEngine(),
                Path(directory),
                lambda _event: None,
                lambda _session: None,
                lambda _state, _message: None,
                threshold=100,
                silence_seconds=0.1,
                max_segment_seconds=15,
            )
            accepted, message, session = receiver.open("invalid", requested_language="fr")
            self.assertFalse(accepted)
            self.assertIn("中文、英文或 auto", message)
            self.assertIsNone(session)


if __name__ == "__main__":
    unittest.main()
