import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from live_transcript.audio import AudioSource
from live_transcript.transcription import (
    EchoDeduplicator,
    NoteEvent,
    TranscriptEvent,
    TranscriptWriter,
    WhisperEngine,
    events_are_duplicates,
    format_timestamp,
)


class TranscriptTests(unittest.TestCase):
    def test_timestamp(self) -> None:
        self.assertEqual(format_timestamp(3_661.9), "01:01:01")

    def test_echo_duplicate(self) -> None:
        mic = TranscriptEvent(AudioSource.MICROPHONE, 10, 14, "Hello, 大家好！")
        system = TranscriptEvent(AudioSource.SYSTEM, 10.2, 13.8, "Hello 大家好")
        self.assertTrue(events_are_duplicates(mic, system))

    def test_different_speech_is_not_duplicate(self) -> None:
        mic = TranscriptEvent(AudioSource.MICROPHONE, 10, 14, "I agree")
        system = TranscriptEvent(AudioSource.SYSTEM, 10, 14, "Let's discuss the plan")
        self.assertFalse(events_are_duplicates(mic, system))

    def test_deduplicator_prefers_system_audio(self) -> None:
        emitted = []
        deduplicator = EchoDeduplicator(emitted.append, delay_seconds=60)
        deduplicator.add(TranscriptEvent(AudioSource.MICROPHONE, 5, 7, "Hello 大家好"))
        deduplicator.add(TranscriptEvent(AudioSource.SYSTEM, 5.1, 6.9, "Hello, 大家好！"))
        deduplicator.flush_ready(force=True)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].source, AudioSource.SYSTEM)

    def test_writer_outputs_markdown_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = []
            writer = TranscriptWriter(Path(directory), on_event=events.append)
            event = TranscriptEvent(AudioSource.SYSTEM, 1, 2, "測試")
            writer.write(event)
            writer.close()
            self.assertIn("對方／系統：測試", (Path(directory) / "transcript.md").read_text())
            self.assertIn('"text": "測試"', (Path(directory) / "transcript.jsonl").read_text())
            self.assertEqual(events, [event])

    def test_writer_appends_markdown_notes_after_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notes = []
            writer = TranscriptWriter(Path(directory), on_note=notes.append)
            writer.write_note(NoteEvent(65.8, "## 決策\n\n- 先做登入流程"))
            writer.write(TranscriptEvent(AudioSource.SYSTEM, 70, 72, "稍後才辨識完成"))
            writer.close()

            markdown = (Path(directory) / "transcript.md").read_text()
            jsonl = (Path(directory) / "transcript.jsonl").read_text()
            self.assertLess(markdown.index("稍後才辨識完成"), markdown.index("## 會議筆記"))
            self.assertIn("### [00:01:05]", markdown)
            self.assertIn("- 先做登入流程", markdown)
            self.assertIn('"type": "note"', jsonl)
            self.assertEqual(notes, [NoteEvent(65.8, "## 決策\n\n- 先做登入流程")])

    def test_model_lifecycle_loads_on_demand_and_unloads_after_session(self) -> None:
        states = []
        engine = WhisperEngine(
            "example/model",
            None,
            on_state=lambda state, _name, _message: states.append(state),
        )
        with (
            patch.object(engine, "_load_model") as load_model,
            patch.object(engine, "_drop_model_references") as drop_model,
        ):
            self.assertFalse(engine.is_loaded)
            engine.load()
            self.assertTrue(engine.is_loaded)
            load_model.assert_called_once()
            self.assertTrue(engine.begin_session())
            with self.assertRaises(RuntimeError):
                engine.unload()
            engine.end_session_and_unload()
            self.assertFalse(engine.is_loaded)
            drop_model.assert_called_once()
        self.assertEqual(
            states,
            ["unloaded", "loading", "loaded", "unloading", "unloaded"],
        )

    def test_background_unload_reports_immediately_and_finishes(self) -> None:
        states = []
        unloaded = threading.Event()

        def on_state(state, _name, _message):
            states.append(state)
            if state == "unloaded":
                unloaded.set()

        engine = WhisperEngine("example/model", None, on_state=on_state)
        with (
            patch.object(engine, "_load_model"),
            patch.object(engine, "_drop_model_references"),
        ):
            engine.load()
            unloaded.clear()
            self.assertTrue(engine.start_unload())
            self.assertIn(engine.state, {"unloading", "unloaded"})
            self.assertTrue(unloaded.wait(timeout=2))
            self.assertEqual(engine.state, "unloaded")
        self.assertIn("unloading", states)

    def test_writer_can_be_renamed_while_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = TranscriptWriter(Path(directory), filename_stem="預設名稱")
            markdown_path, jsonl_path = writer.rename("客戶訪談")
            writer.write(TranscriptEvent(AudioSource.MICROPHONE, 1, 2, "Hello"))
            writer.close()

            self.assertEqual(markdown_path.name, "客戶訪談.md")
            self.assertEqual(jsonl_path.name, "客戶訪談.jsonl")
            self.assertIn("Hello", markdown_path.read_text())
            self.assertFalse((Path(directory) / "預設名稱.md").exists())


if __name__ == "__main__":
    unittest.main()
