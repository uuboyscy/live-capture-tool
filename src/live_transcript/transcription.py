from __future__ import annotations

import gc
import json
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .audio import AudioSegment, AudioSource, SAMPLE_RATE


DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
SUPPORTED_LANGUAGES = frozenset({"zh", "en"})


def normalize_language(language: str | None) -> str | None:
    """Convert the UI/CLI language value to the Whisper API value."""
    if language in (None, "auto"):
        return None
    if language in SUPPORTED_LANGUAGES:
        return language
    raise ValueError("語言只能選擇中文、英文或 auto")


@dataclass(slots=True)
class TranscriptEvent:
    source: AudioSource
    start_seconds: float
    end_seconds: float
    text: str
    detected_language: str | None = None


@dataclass(slots=True)
class NoteEvent:
    timestamp_seconds: float
    text: str


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def normalized_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


def events_are_duplicates(left: TranscriptEvent, right: TranscriptEvent) -> bool:
    if left.source is right.source:
        return False
    if left.end_seconds < right.start_seconds - 1.25:
        return False
    if right.end_seconds < left.start_seconds - 1.25:
        return False
    left_text = normalized_text(left.text)
    right_text = normalized_text(right.text)
    if min(len(left_text), len(right_text)) < 3:
        return left_text == right_text and bool(left_text)
    similarity = SequenceMatcher(None, left_text, right_text).ratio()
    shorter, longer = sorted((left_text, right_text), key=len)
    containment = len(shorter) / len(longer) if shorter in longer else 0.0
    return max(similarity, containment) >= 0.78


class TranscriptWriter:
    def __init__(
        self,
        output_dir: Path,
        on_event: Callable[[TranscriptEvent], None] | None = None,
        on_note: Callable[[NoteEvent], None] | None = None,
        filename_stem: str = "transcript",
    ) -> None:
        if not filename_stem or Path(filename_stem).name != filename_stem:
            raise ValueError("逐字稿檔名不可包含路徑")
        output_dir.mkdir(parents=True, exist_ok=True)
        self.markdown_path = output_dir / f"{filename_stem}.md"
        self.jsonl_path = output_dir / f"{filename_stem}.jsonl"
        self._markdown = self.markdown_path.open("w", encoding="utf-8")
        self._jsonl = self.jsonl_path.open("w", encoding="utf-8")
        self._on_event = on_event
        self._on_note = on_note
        self._notes: list[NoteEvent] = []
        self._lock = threading.RLock()
        self._closed = False
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        self._markdown.write(f"# 會議逐字稿\n\n開始時間：{started}\n\n")
        self._markdown.flush()

    def write(self, event: TranscriptEvent) -> None:
        timestamp = format_timestamp(event.start_seconds)
        line = f"[{timestamp}] {event.source.display_name}：{event.text}"
        print(line, flush=True)
        payload = {
            "source": event.source.name.lower(),
            "speaker": event.source.display_name,
            "start_seconds": round(event.start_seconds, 3),
            "end_seconds": round(event.end_seconds, 3),
            "text": event.text,
            "detected_language": event.detected_language,
        }
        with self._lock:
            if self._closed:
                raise RuntimeError("逐字稿檔案已關閉")
            self._markdown.write(f"- {line}\n")
            self._jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._markdown.flush()
            self._jsonl.flush()
        if self._on_event is not None:
            self._on_event(event)

    def write_note(self, event: NoteEvent) -> None:
        text = event.text.strip()
        if not text:
            raise ValueError("筆記內容不可為空白")
        event = NoteEvent(max(0, event.timestamp_seconds), text)
        payload = {
            "type": "note",
            "timestamp_seconds": round(event.timestamp_seconds, 3),
            "text": event.text,
        }
        with self._lock:
            if self._closed:
                raise RuntimeError("逐字稿檔案已關閉")
            self._notes.append(event)
            self._jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._jsonl.flush()
        if self._on_note is not None:
            self._on_note(event)

    def rename(self, filename_stem: str) -> tuple[Path, Path]:
        if not filename_stem or Path(filename_stem).name != filename_stem:
            raise ValueError("逐字稿檔名不可包含路徑")
        with self._lock:
            new_markdown = self.markdown_path.with_name(f"{filename_stem}.md")
            new_jsonl = self.jsonl_path.with_name(f"{filename_stem}.jsonl")
            if new_markdown == self.markdown_path:
                return self.markdown_path, self.jsonl_path
            if new_markdown.exists() or new_jsonl.exists():
                raise FileExistsError("同名逐字稿已存在")
            self._markdown.flush()
            self._jsonl.flush()
            old_markdown = self.markdown_path
            old_jsonl = self.jsonl_path
            old_markdown.rename(new_markdown)
            try:
                old_jsonl.rename(new_jsonl)
            except BaseException:
                new_markdown.rename(old_markdown)
                raise
            self.markdown_path = new_markdown
            self.jsonl_path = new_jsonl
            return self.markdown_path, self.jsonl_path

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._notes:
                self._markdown.write("\n## 會議筆記\n\n")
                for note in self._notes:
                    timestamp = format_timestamp(note.timestamp_seconds)
                    self._markdown.write(f"### [{timestamp}]\n\n{note.text}\n\n")
            self._markdown.close()
            self._jsonl.close()
            self._closed = True


class EchoDeduplicator:
    """Briefly buffers events and prefers direct system audio over speaker echo."""

    def __init__(self, emit: Callable[[TranscriptEvent], None], delay_seconds: float = 1.5):
        self._emit = emit
        self._delay_seconds = delay_seconds
        self._pending: list[tuple[float, TranscriptEvent]] = []

    def add(self, event: TranscriptEvent) -> None:
        for index, (_, existing) in enumerate(self._pending):
            if not events_are_duplicates(existing, event):
                continue
            if existing.source is AudioSource.SYSTEM:
                return
            if event.source is AudioSource.SYSTEM:
                self._pending[index] = (time.monotonic() + self._delay_seconds, event)
                return
            return
        self._pending.append((time.monotonic() + self._delay_seconds, event))
        self.flush_ready()

    def flush_ready(self, *, force: bool = False) -> None:
        now = time.monotonic()
        ready = [item for item in self._pending if force or item[0] <= now]
        self._pending = [item for item in self._pending if not (force or item[0] <= now)]
        for _, event in sorted(ready, key=lambda item: item[1].start_seconds):
            self._emit(event)

    def next_timeout(self) -> float:
        if not self._pending:
            return 0.25
        return max(0.01, min(0.25, min(item[0] for item in self._pending) - time.monotonic()))


class WhisperEngine:
    def __init__(
        self,
        model: str,
        language: str | None,
        on_state: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.model = model
        self.language = language
        self._on_state = on_state
        self._state = "unloaded"
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._in_use = False
        if on_state is not None:
            self._notify_state(self._state, self._message_for(self._state))

    def set_language(self, language: str | None) -> None:
        language = normalize_language(language)
        with self._operation_lock:
            with self._state_lock:
                if self._in_use:
                    raise RuntimeError("錄音或辨識進行中，無法變更語言")
                self.language = language

    @property
    def display_name(self) -> str:
        return self.model.rsplit("/", 1)[-1]

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def is_loaded(self) -> bool:
        return self.state == "loaded"

    def set_state_listener(
        self,
        listener: Callable[[str, str, str], None] | None,
    ) -> None:
        with self._state_lock:
            self._on_state = listener
            state = self._state
        self._notify_state(state, self._message_for(state))

    def warm_up(self) -> None:
        self.load()

    def load(self) -> None:
        with self._operation_lock:
            if self.is_loaded:
                return
            self._set_state("loading", f"正在載入 {self.display_name}…")
            try:
                self._load_model()
            except BaseException:
                self._drop_model_references()
                self._set_state("error", "模型載入失敗")
                raise
            self._set_state("loaded", f"{self.display_name} 已載入記憶體")

    def unload(self) -> None:
        with self._operation_lock:
            with self._state_lock:
                if self._in_use:
                    raise RuntimeError("錄音或辨識進行中，無法卸載模型")
                if self._state in {"unloaded", "unloading"}:
                    return
            self._unload_locked()

    def start_unload(self) -> bool:
        """Start releasing model memory without blocking the HTTP request."""
        with self._state_lock:
            if self._in_use:
                raise RuntimeError("錄音或辨識進行中，無法卸載模型")
            if self._state in {"unloaded", "unloading"}:
                return False
            if self._state == "loading":
                raise RuntimeError("模型仍在載入中，請稍候再卸載")
            self._state = "unloading"
        self._notify_state("unloading", self._message_for("unloading"))
        threading.Thread(
            target=self._finish_background_unload,
            name="model-unloader",
            daemon=True,
        ).start()
        return True

    def begin_session(self) -> bool:
        with self._operation_lock:
            with self._state_lock:
                if self._state != "loaded" or self._in_use:
                    return False
                self._in_use = True
                return True

    def end_session_and_unload(self) -> None:
        with self._operation_lock:
            with self._state_lock:
                self._in_use = False
            self._unload_locked()

    def _load_model(self) -> None:
        import numpy as np
        import mlx_whisper

        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        mlx_whisper.transcribe(
            silence,
            path_or_hf_repo=self.model,
            language=self.language,
            verbose=None,
            condition_on_previous_text=False,
        )

    def _unload_locked(self) -> None:
        self._set_state("unloading", "正在釋放模型與 Metal 記憶體…")
        self._drop_model_references()
        self._set_state("unloaded", "模型未載入（低記憶體待機）")

    def _finish_background_unload(self) -> None:
        try:
            with self._operation_lock:
                self._drop_model_references()
        except BaseException:
            self._set_state("error", "模型卸載失敗")
            return
        self._set_state("unloaded", "模型未載入（低記憶體待機）")

    @staticmethod
    def _drop_model_references() -> None:
        try:
            from mlx_whisper.transcribe import ModelHolder

            ModelHolder.model = None
            ModelHolder.model_path = None
        except ImportError:
            pass
        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
        except (ImportError, AttributeError, RuntimeError):
            pass
        gc.collect()

    def _set_state(self, state: str, message: str) -> None:
        with self._state_lock:
            self._state = state
        self._notify_state(state, message)

    def _notify_state(self, state: str, message: str) -> None:
        with self._state_lock:
            listener = self._on_state
        if listener is not None:
            listener(state, self.model, message)

    def _message_for(self, state: str) -> str:
        messages = {
            "unloaded": "模型未載入（低記憶體待機）",
            "loading": f"正在載入 {self.display_name}…",
            "loaded": f"{self.display_name} 已載入記憶體",
            "unloading": "正在釋放模型與 Metal 記憶體…",
            "error": "模型載入失敗",
        }
        return messages[state]

    def transcribe(self, segment: AudioSegment) -> TranscriptEvent | None:
        if not self.is_loaded:
            raise RuntimeError("模型尚未載入")
        import numpy as np
        import mlx_whisper

        waveform = np.frombuffer(segment.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            waveform,
            path_or_hf_repo=self.model,
            language=self.language,
            verbose=None,
            condition_on_previous_text=False,
            no_speech_threshold=0.7,
        )
        text = re.sub(r"\s+", " ", str(result.get("text", ""))).strip()
        if not text:
            return None
        return TranscriptEvent(
            source=segment.source,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            text=text,
            detected_language=result.get("language"),
        )


class TranscriptionWorker:
    def __init__(self, engine: WhisperEngine, writer: TranscriptWriter) -> None:
        self._engine = engine
        self._writer = writer
        self._queue: queue.Queue[AudioSegment | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="transcription", daemon=False)
        self._error: BaseException | None = None
        self._deduplicator = EchoDeduplicator(writer.write)

    def start(self) -> None:
        self._thread.start()

    def submit(self, segment: AudioSegment) -> None:
        if self._error is not None:
            raise RuntimeError("逐字稿工作已停止") from self._error
        self._queue.put(segment)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join()
        self._writer.close()
        if self._error is not None:
            raise RuntimeError("逐字稿工作執行失敗") from self._error

    def _run(self) -> None:
        try:
            stopping = False
            while not stopping:
                try:
                    item = self._queue.get(timeout=self._deduplicator.next_timeout())
                except queue.Empty:
                    self._deduplicator.flush_ready()
                    continue
                if item is None:
                    stopping = True
                    continue
                event = self._engine.transcribe(item)
                if event is not None:
                    self._deduplicator.add(event)
                self._deduplicator.flush_ready()
            self._deduplicator.flush_ready(force=True)
        except BaseException as exc:
            self._error = exc
