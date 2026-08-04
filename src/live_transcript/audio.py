from __future__ import annotations

import audioop
import struct
from array import array
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import BinaryIO, Iterable


SAMPLE_RATE = 16_000
FRAME_MAGIC = b"LCT1"
FRAME_HEADER = struct.Struct("<4sBI")


class AudioSource(IntEnum):
    SYSTEM = 0
    MICROPHONE = 1

    @property
    def display_name(self) -> str:
        return "對方／系統" if self is AudioSource.SYSTEM else "你／麥克風"


@dataclass(frozen=True, slots=True)
class AudioSegment:
    source: AudioSource
    start_seconds: float
    end_seconds: float
    pcm: bytes


def read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if not chunks:
                return b""
            raise EOFError("音訊擷取器在資料框中途結束")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> tuple[AudioSource, bytes] | None:
    header = read_exact(stream, FRAME_HEADER.size)
    if not header:
        return None
    magic, source_value, payload_size = FRAME_HEADER.unpack(header)
    if magic != FRAME_MAGIC:
        raise ValueError("收到無效的音訊資料框")
    if payload_size > SAMPLE_RATE * 2 * 10:
        raise ValueError(f"音訊資料框過大：{payload_size} bytes")
    try:
        source = AudioSource(source_value)
    except ValueError as exc:
        raise ValueError(f"未知的音源編號：{source_value}") from exc
    payload = read_exact(stream, payload_size)
    if len(payload) != payload_size:
        raise EOFError("音訊擷取器在 PCM 資料中途結束")
    return source, payload


def pcm_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    return float(audioop.rms(pcm, 2))


class VoiceSegmenter:
    """Energy-based streaming VAD tuned for clean meeting audio.

    ScreenCaptureKit's system-audio track has an effectively zero noise floor;
    the microphone threshold adapts while idle. Speech is emitted after a short
    silence, with pre-roll so initial consonants are not lost.
    """

    def __init__(
        self,
        source: AudioSource,
        *,
        sample_rate: int = SAMPLE_RATE,
        threshold: float = 360.0,
        silence_seconds: float = 0.8,
        pre_roll_seconds: float = 0.3,
        trailing_seconds: float = 0.2,
        min_speech_seconds: float = 0.35,
        max_segment_seconds: float = 15.0,
    ) -> None:
        self.source = source
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.silence_samples = int(silence_seconds * sample_rate)
        self.pre_roll_samples = int(pre_roll_seconds * sample_rate)
        self.trailing_samples = int(trailing_seconds * sample_rate)
        self.min_speech_samples = int(min_speech_seconds * sample_rate)
        self.max_segment_samples = int(max_segment_seconds * sample_rate)

        self._sample_cursor = 0
        self._noise_floor = 40.0
        self._pre_roll: deque[array[int]] = deque()
        self._pre_roll_count = 0
        self._active: list[array[int]] = []
        self._active_start = 0
        self._active_count = 0
        self._last_voice_count = 0

    def feed(self, pcm: bytes) -> list[AudioSegment]:
        if not pcm:
            return []
        if len(pcm) % 2:
            raise ValueError("16-bit PCM 資料長度必須是偶數")

        samples = array("h")
        samples.frombytes(pcm)
        frame_count = len(samples)
        level = pcm_rms(pcm)
        adaptive_threshold = max(self.threshold, self._noise_floor * 3.2)
        is_voice = level >= adaptive_threshold
        emitted: list[AudioSegment] = []

        if not self._active:
            if is_voice:
                self._active_start = self._sample_cursor - self._pre_roll_count
                self._active = list(self._pre_roll)
                self._active_count = self._pre_roll_count
                self._append_active(samples)
                self._last_voice_count = self._active_count
                self._pre_roll.clear()
                self._pre_roll_count = 0
            else:
                self._noise_floor = (self._noise_floor * 0.98) + (level * 0.02)
                self._append_pre_roll(samples)
        else:
            self._append_active(samples)
            if is_voice:
                self._last_voice_count = self._active_count

            silence_count = self._active_count - self._last_voice_count
            if silence_count >= self.silence_samples:
                segment = self._finish_active(
                    keep_samples=min(silence_count, self.trailing_samples)
                )
                if segment is not None:
                    emitted.append(segment)
                self._append_pre_roll(samples)
            elif self._active_count >= self.max_segment_samples:
                segment = self._finish_active(keep_samples=self._active_count)
                if segment is not None:
                    emitted.append(segment)

        self._sample_cursor += frame_count
        return emitted

    def flush(self) -> AudioSegment | None:
        if not self._active:
            return None
        return self._finish_active(keep_samples=self._active_count)

    def _append_active(self, samples: array[int]) -> None:
        self._active.append(samples)
        self._active_count += len(samples)

    def _append_pre_roll(self, samples: array[int]) -> None:
        self._pre_roll.append(samples)
        self._pre_roll_count += len(samples)
        while self._pre_roll and self._pre_roll_count > self.pre_roll_samples:
            removed = self._pre_roll.popleft()
            self._pre_roll_count -= len(removed)

    def _finish_active(self, *, keep_samples: int) -> AudioSegment | None:
        all_samples = array("h")
        for chunk in self._active:
            all_samples.extend(chunk)

        desired_count = min(len(all_samples), max(0, self._last_voice_count + keep_samples))
        if keep_samples == self._active_count:
            desired_count = len(all_samples)
        trimmed = all_samples[:desired_count]
        start = self._active_start
        self._active = []
        self._active_count = 0
        self._last_voice_count = 0

        if len(trimmed) < self.min_speech_samples:
            return None
        return AudioSegment(
            source=self.source,
            start_seconds=max(0.0, start / self.sample_rate),
            end_seconds=max(0.0, (start + len(trimmed)) / self.sample_rate),
            pcm=trimmed.tobytes(),
        )


def make_pcm(samples: Iterable[int]) -> bytes:
    values = array("h", samples)
    return values.tobytes()
