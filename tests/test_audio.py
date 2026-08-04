import io
import struct
import unittest

from live_transcript.audio import (
    FRAME_HEADER,
    FRAME_MAGIC,
    AudioSource,
    VoiceSegmenter,
    make_pcm,
    read_frame,
)


class FrameProtocolTests(unittest.TestCase):
    def test_reads_complete_frame(self) -> None:
        payload = make_pcm([1, -2, 3])
        stream = io.BytesIO(
            FRAME_HEADER.pack(FRAME_MAGIC, AudioSource.SYSTEM, len(payload)) + payload
        )
        self.assertEqual(read_frame(stream), (AudioSource.SYSTEM, payload))
        self.assertIsNone(read_frame(stream))

    def test_rejects_bad_magic(self) -> None:
        stream = io.BytesIO(struct.pack("<4sBI", b"NOPE", 0, 0))
        with self.assertRaises(ValueError):
            read_frame(stream)


class VoiceSegmenterTests(unittest.TestCase):
    def test_emits_speech_after_silence(self) -> None:
        rate = 1_000
        segmenter = VoiceSegmenter(
            AudioSource.MICROPHONE,
            sample_rate=rate,
            threshold=100,
            silence_seconds=0.2,
            pre_roll_seconds=0.1,
            trailing_seconds=0.05,
            min_speech_seconds=0.1,
        )
        frames = (
            [[0] * 100] * 2
            + [[2_000] * 100] * 4
            + [[0] * 100] * 3
        )
        emitted = []
        for frame in frames:
            emitted.extend(segmenter.feed(make_pcm(frame)))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].source, AudioSource.MICROPHONE)
        self.assertLessEqual(emitted[0].start_seconds, 0.2)
        self.assertGreater(emitted[0].end_seconds, 0.6)

    def test_ignores_short_noise(self) -> None:
        segmenter = VoiceSegmenter(
            AudioSource.SYSTEM,
            sample_rate=1_000,
            threshold=100,
            silence_seconds=0.1,
            min_speech_seconds=0.3,
        )
        emitted = []
        for frame in ([1_000] * 50, [0] * 100):
            emitted.extend(segmenter.feed(make_pcm(frame)))
        self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()

