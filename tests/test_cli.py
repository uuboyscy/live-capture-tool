import unittest

from live_transcript.cli import make_parser
from live_transcript.transcription import DEFAULT_MODEL


class CliTests(unittest.TestCase):
    def test_server_is_local_only_by_default(self) -> None:
        args = make_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8083)
        self.assertEqual(args.input, "remote")
        self.assertEqual(DEFAULT_MODEL, "mlx-community/whisper-large-v3-turbo")
        self.assertEqual(args.model, DEFAULT_MODEL)


if __name__ == "__main__":
    unittest.main()
