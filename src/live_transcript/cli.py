from __future__ import annotations

import argparse
import platform
import shutil
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

from .audio import AudioSource, SAMPLE_RATE, VoiceSegmenter, read_frame
from .remote import RemoteAudioReceiver
from .transcription import DEFAULT_MODEL, TranscriptWriter, TranscriptionWorker, WhisperEngine
from .web import EventHub, LiveTranscriptServer


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SWIFT_SOURCE = PROJECT_ROOT / "native" / "AudioCapture.swift"
BUILD_DIR = PROJECT_ROOT / ".build"
CAPTURE_HELPER = BUILD_DIR / "audio-capture"


def build_capture_helper(*, force: bool = False) -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("目前版本只支援 macOS")
    if not SWIFT_SOURCE.exists():
        raise RuntimeError(f"找不到音訊擷取器原始碼：{SWIFT_SOURCE}")
    if not force and CAPTURE_HELPER.exists():
        if CAPTURE_HELPER.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
            return CAPTURE_HELPER

    swiftc = shutil.which("swiftc")
    if swiftc is None:
        raise RuntimeError("找不到 swiftc；請先安裝 Xcode Command Line Tools")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    module_cache = BUILD_DIR / "swift-module-cache"
    module_cache.mkdir(parents=True, exist_ok=True)
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise RuntimeError(f"不支援的 Mac 架構：{architecture}")
    print("正在編譯 macOS 音訊擷取器…", file=sys.stderr, flush=True)
    command = [
        swiftc,
        "-target",
        f"{architecture}-apple-macosx15.0",
        "-module-cache-path",
        str(module_cache),
        "-O",
        "-parse-as-library",
        str(SWIFT_SOURCE),
        "-o",
        str(CAPTURE_HELPER),
        "-framework",
        "ScreenCaptureKit",
        "-framework",
        "AVFoundation",
        "-framework",
        "CoreMedia",
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"音訊擷取器編譯失敗：\n{completed.stderr.strip()}")
    return CAPTURE_HELPER


def session_output_dir(base_dir: Path) -> Path:
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    candidate = base_dir / stamp
    suffix = 1
    while candidate.exists():
        candidate = base_dir / f"{stamp}_{suffix}"
        suffix += 1
    return candidate


class AudioArchive:
    def __init__(self, output_dir: Path) -> None:
        self._files: dict[AudioSource, wave.Wave_write] = {}
        for source, filename in (
            (AudioSource.SYSTEM, "system.wav"),
            (AudioSource.MICROPHONE, "microphone.wav"),
        ):
            handle = wave.open(str(output_dir / filename), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            self._files[source] = handle

    def write(self, source: AudioSource, pcm: bytes) -> None:
        self._files[source].writeframesraw(pcm)

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()


def run_remote_server(args: argparse.Namespace) -> int:
    output_root = args.output_dir.expanduser().resolve()
    hub = EventHub(args.model)
    language = None if args.language == "auto" else args.language
    engine = WhisperEngine(args.model, language, on_state=hub.set_model_status)
    receiver = RemoteAudioReceiver(
        engine,
        output_root,
        hub.publish_transcript,
        hub.publish_session,
        hub.set_status,
        publish_note=hub.publish_note,
        threshold=args.vad_threshold,
        silence_seconds=args.silence,
        max_segment_seconds=args.max_segment,
    )
    server = LiveTranscriptServer(
        args.host,
        args.port,
        audio_receiver=receiver,
        hub=hub,
        model_controller=engine,
    )

    server.start()
    print(f"逐字稿將儲存在：{output_root}", file=sys.stderr, flush=True)
    print(f"遠端錄音網頁：{server.url}", file=sys.stderr, flush=True)
    print("服務已進入低記憶體待機；請從網頁啟用模型。", file=sys.stderr, flush=True)
    hub.set_status("waiting", "服務待機中；請先啟用模型")
    stop_requested = False
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stop_requested:
            if not server.is_alive:
                raise RuntimeError("網頁伺服器執行緒異常停止")
            stale_client = receiver.expire_stale()
            if stale_client is not None:
                server.disconnect_audio(stale_client)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except BaseException:
        hub.set_status("error", "遠端錄音伺服器發生錯誤")
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        hub.set_status("finishing", "正在完成剩餘逐字稿…")
        server.stop()
        receiver.close_active()
        engine.unload()
    print("遠端錄音伺服器已停止。", file=sys.stderr, flush=True)
    return 0


def run_local_capture(args: argparse.Namespace) -> int:
    helper = build_capture_helper()
    output_dir = session_output_dir(args.output_dir.expanduser().resolve())
    server = LiveTranscriptServer(args.host, args.port)
    writer = TranscriptWriter(output_dir, on_event=server.hub.publish_transcript)
    archive = AudioArchive(output_dir) if args.save_audio else None
    language = None if args.language == "auto" else args.language
    engine = WhisperEngine(args.model, language)

    server.start()
    print(f"逐字稿將儲存在：{output_dir}", file=sys.stderr, flush=True)
    print(f"即時逐字稿網頁：{server.url}", file=sys.stderr, flush=True)
    print("正在載入語音模型（第一次會自動下載）…", file=sys.stderr, flush=True)
    server.hub.set_status("loading", "正在載入語音模型…")
    try:
        engine.warm_up()
    except BaseException:
        server.hub.set_status("error", "語音模型載入失敗")
        writer.close()
        if archive is not None:
            archive.close()
        server.stop()
        raise
    if not engine.begin_session():
        writer.close()
        if archive is not None:
            archive.close()
        server.stop()
        raise RuntimeError("模型無法進入錄音狀態")

    print("模型已就緒。接下來 macOS 可能要求兩項權限：", file=sys.stderr)
    print("  1. 麥克風", file=sys.stderr)
    print("  2. 螢幕與系統音訊錄製", file=sys.stderr)
    print("錄製中；按 Ctrl+C 停止並完成逐字稿。", file=sys.stderr, flush=True)
    server.hub.set_status("recording", "錄製中")

    worker = TranscriptionWorker(engine, writer)
    segmenters = {
        source: VoiceSegmenter(
            source,
            threshold=args.vad_threshold,
            silence_seconds=args.silence,
            max_segment_seconds=args.max_segment,
        )
        for source in AudioSource
    }

    try:
        process = subprocess.Popen([str(helper)], stdout=subprocess.PIPE)
    except BaseException:
        server.hub.set_status("error", "音訊擷取器啟動失敗")
        engine.end_session_and_unload()
        writer.close()
        if archive is not None:
            archive.close()
        server.stop()
        raise
    worker.start()
    assert process.stdout is not None
    previous_sigint = signal.getsignal(signal.SIGINT)
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, request_stop)
    capture_error: BaseException | None = None
    try:
        while not stopping:
            frame = read_frame(process.stdout)
            if frame is None:
                break
            source, pcm = frame
            if archive is not None:
                archive.write(source, pcm)
            for segment in segmenters[source].feed(pcm):
                worker.submit(segment)
    except BaseException as exc:
        capture_error = exc
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
        for segmenter in segmenters.values():
            segment = segmenter.flush()
            if segment is not None:
                worker.submit(segment)
        if archive is not None:
            archive.close()
        print("正在完成剩餘逐字稿…", file=sys.stderr, flush=True)
        server.hub.set_status("finishing", "正在完成剩餘逐字稿…")
        try:
            worker.stop()
        finally:
            engine.end_session_and_unload()
        server.hub.set_status("stopped", "錄製已結束")
        server.stop()

    if capture_error is not None:
        raise capture_error
    if not stopping and process.returncode not in (0, -signal.SIGTERM):
        raise RuntimeError(f"音訊擷取器異常結束（exit {process.returncode}）")
    print(f"完成：{writer.markdown_path}", file=sys.stderr, flush=True)
    return 0


def prepare(args: argparse.Namespace) -> int:
    if args.input == "local":
        build_capture_helper(force=True)
    language = None if args.language == "auto" else args.language
    print("正在下載並載入語音模型…", file=sys.stderr, flush=True)
    engine = WhisperEngine(args.model, language)
    engine.warm_up()
    engine.unload()
    print("準備完成。執行 `uv run live-transcript` 即可開始。", file=sys.stderr)
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live-transcript",
        description="接收瀏覽器或 Mac 本機音訊，使用 Apple Silicon 即時輸出中英文逐字稿。",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "prepare"),
        default="run",
        help="run（預設）開始逐字稿；prepare 預先下載模型並編譯擷取器",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="MLX Whisper 模型名稱或本機路徑")
    parser.add_argument(
        "--language",
        choices=("auto", "zh", "en"),
        default="auto",
        help="語言；auto 支援會議中英自動辨識（預設）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "transcripts",
        help="逐字稿根目錄（預設：專案 transcripts/）",
    )
    parser.add_argument(
        "--save-audio",
        action="store_true",
        help="local 模式另外保存系統音訊與麥克風 WAV",
    )
    parser.add_argument(
        "--input",
        choices=("remote", "local"),
        default="remote",
        help="音訊來源；remote 由遠端瀏覽器傳送（預設），local 擷取 Mac 本機音訊",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="網頁伺服器監聽位址（預設：僅限本機）",
    )
    parser.add_argument("--port", type=int, default=8083, help="網頁伺服器連接埠（預設：8083）")
    parser.add_argument("--vad-threshold", type=float, default=360.0, help=argparse.SUPPRESS)
    parser.add_argument("--silence", type=float, default=0.8, help=argparse.SUPPRESS)
    parser.add_argument("--max-segment", type=float, default=15.0, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            raise SystemExit(prepare(args))
        if args.input == "local":
            raise SystemExit(run_local_capture(args))
        raise SystemExit(run_remote_server(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
