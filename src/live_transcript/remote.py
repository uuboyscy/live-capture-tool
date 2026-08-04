from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .audio import AudioSource, VoiceSegmenter
from .transcription import (
    TranscriptWriter,
    TranscriptionWorker,
    WhisperEngine,
    normalize_language,
)


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    name: str
    output_dir: Path
    markdown_path: Path
    jsonl_path: Path
    state: str
    writer: TranscriptWriter | None

    def payload(self) -> dict[str, str | None]:
        saved = self.state == "saved"
        return {
            "id": self.session_id,
            "name": self.name,
            "state": self.state,
            "download_url": (
                f"/sessions/{self.session_id}/download" if saved else None
            ),
            "jsonl_url": (
                f"/sessions/{self.session_id}/download?format=jsonl" if saved else None
            ),
        }


def normalized_session_name(value: str | None, default: str) -> str:
    name = unicodedata.normalize("NFKC", value or "").strip()
    name = re.sub(r"\.(?:md|jsonl)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[/\\:\x00-\x1f]", "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:80].strip() or default)


class RemoteAudioReceiver:
    """Creates one saved transcript session per browser recording."""

    def __init__(
        self,
        engine: WhisperEngine,
        output_root: Path,
        publish_transcript: Callable[[object], None],
        publish_session: Callable[[dict[str, str | None]], None],
        set_status: Callable[[str, str], None],
        *,
        threshold: float,
        silence_seconds: float,
        max_segment_seconds: float,
    ) -> None:
        self._engine = engine
        self._output_root = output_root
        self._publish_transcript = publish_transcript
        self._publish_session = publish_session
        self._set_status = set_status
        self._threshold = threshold
        self._silence_seconds = silence_seconds
        self._max_segment_seconds = max_segment_seconds
        self._lock = threading.RLock()
        self._active_client: str | None = None
        self._active_session_id: str | None = None
        self._worker: TranscriptionWorker | None = None
        self._segmenters: dict[AudioSource, VoiceSegmenter] = {}
        self._sessions: dict[str, SessionRecord] = {}
        self._opened_at: float | None = None
        self._last_audio_at: float | None = None

    def open(
        self,
        client_id: str,
        requested_name: str | None = None,
        requested_language: str | None = None,
    ) -> tuple[bool, str, dict[str, str | None] | None]:
        with self._lock:
            if self._active_client is not None:
                return False, "已有另一台電腦正在傳送音訊", None
            try:
                language = (
                    normalize_language(requested_language)
                    if requested_language is not None
                    else self._engine.language
                )
                self._engine.set_language(language)
            except (AttributeError, ValueError, RuntimeError) as error:
                return False, str(error) or "無法設定辨識語言", None
            if not self._engine.begin_session():
                return False, "請先按「啟用模型」，等待模型載入完成", None

            try:
                session_id, output_dir = self._allocate_session_dir()
                default_name = f"transcript-{session_id}"
                name = normalized_session_name(requested_name, default_name)
                writer = TranscriptWriter(
                    output_dir,
                    on_event=self._publish_transcript,
                    filename_stem=name,
                )
                worker = TranscriptionWorker(self._engine, writer)
                worker.start()
                record = SessionRecord(
                    session_id=session_id,
                    name=name,
                    output_dir=output_dir,
                    markdown_path=writer.markdown_path,
                    jsonl_path=writer.jsonl_path,
                    state="recording",
                    writer=writer,
                )
                self._sessions[session_id] = record
                self._active_client = client_id
                self._active_session_id = session_id
                self._worker = worker
                self._opened_at = time.monotonic()
                self._last_audio_at = None
                self._segmenters = {
                    source: VoiceSegmenter(
                        source,
                        threshold=self._threshold,
                        silence_seconds=self._silence_seconds,
                        max_segment_seconds=self._max_segment_seconds,
                    )
                    for source in AudioSource
                }
                payload = record.payload()
            except BaseException:
                self._engine.end_session_and_unload()
                raise
        self._publish_session(payload)
        self._set_status("recording", "正在接收遠端音訊")
        return True, "", payload

    def feed(self, client_id: str, source: AudioSource, pcm: bytes) -> None:
        with self._lock:
            if client_id != self._active_client or self._worker is None:
                return
            self._last_audio_at = time.monotonic()
            segments = self._segmenters[source].feed(pcm)
            worker = self._worker
        for segment in segments:
            worker.submit(segment)

    def close(self, client_id: str) -> None:
        with self._lock:
            if client_id != self._active_client:
                return
            session_id = self._active_session_id
            segmenters = self._segmenters
            worker = self._worker
            self._segmenters = {}
            self._worker = None
            self._active_client = None
            self._active_session_id = None
            self._opened_at = None
            self._last_audio_at = None
            assert session_id is not None
            record = self._sessions[session_id]
            record.state = "finishing"

        try:
            assert worker is not None
            for segmenter in segmenters.values():
                segment = segmenter.flush()
                if segment is not None:
                    worker.submit(segment)
            worker.stop()
        except BaseException:
            with self._lock:
                record.state = "error"
                record.writer = None
                payload = record.payload()
            self._publish_session(payload)
            self._set_status("error", "逐字稿保存失敗")
            return
        finally:
            self._engine.end_session_and_unload()

        with self._lock:
            assert record.writer is not None
            record.markdown_path = record.writer.markdown_path
            record.jsonl_path = record.writer.jsonl_path
            record.writer = None
            record.state = "saved"
            payload = record.payload()
        self._publish_session(payload)
        self._set_status("waiting", "逐字稿已保存；可開始下一次錄音")

    def close_active(self) -> None:
        with self._lock:
            client_id = self._active_client
        if client_id is not None:
            self.close(client_id)

    def expire_stale(
        self,
        *,
        audio_idle_seconds: float = 15.0,
        connection_idle_seconds: float = 300.0,
    ) -> str | None:
        now = time.monotonic()
        with self._lock:
            client_id = self._active_client
            if client_id is None or self._opened_at is None:
                return None
            if self._last_audio_at is None:
                stale = now - self._opened_at >= connection_idle_seconds
            else:
                stale = now - self._last_audio_at >= audio_idle_seconds
        if not stale:
            return None
        self.close(client_id)
        self._set_status("waiting", "遠端音訊逾時；逐字稿已自動保存")
        return client_id

    def rename_session(
        self,
        session_id: str,
        requested_name: str,
    ) -> dict[str, str | None]:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                raise KeyError("找不到逐字稿 session")
            name = normalized_session_name(requested_name, record.name)
            if name == record.name:
                return record.payload()

            if record.writer is not None:
                markdown_path, jsonl_path = record.writer.rename(name)
            else:
                markdown_path = record.output_dir / f"{name}.md"
                jsonl_path = record.output_dir / f"{name}.jsonl"
                if markdown_path.exists() or jsonl_path.exists():
                    raise FileExistsError("同名逐字稿已存在")
                record.markdown_path.rename(markdown_path)
                try:
                    record.jsonl_path.rename(jsonl_path)
                except BaseException:
                    markdown_path.rename(record.markdown_path)
                    raise

            record.name = name
            record.markdown_path = markdown_path
            record.jsonl_path = jsonl_path
            payload = record.payload()
        self._publish_session(payload)
        return payload

    def download(
        self,
        session_id: str,
        file_format: str,
    ) -> tuple[Path, str] | None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None or record.state != "saved":
                return None
            if file_format == "jsonl":
                return record.jsonl_path, f"{record.name}.jsonl"
            return record.markdown_path, f"{record.name}.md"

    def _allocate_session_dir(self) -> tuple[str, Path]:
        self._output_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        session_id = stamp
        output_dir = self._output_root / session_id
        suffix = 1
        while output_dir.exists():
            session_id = f"{stamp}_{suffix}"
            output_dir = self._output_root / session_id
            suffix += 1
        output_dir.mkdir()
        return session_id, output_dir
