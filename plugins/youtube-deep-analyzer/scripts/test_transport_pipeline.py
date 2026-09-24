from __future__ import annotations

import os
import multiprocessing
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import media_pipeline as pipeline


class FakeHttpError(RuntimeError):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP Error {status}: test")
        self.code = status
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


@contextmanager
def no_wait_gate(_: str):
    yield {"cooldown_seconds": 0.0, "waited_seconds": 0.0}


def process_gate_worker(cache_dir: str, queue) -> None:
    os.environ["YOUTUBE_DEEP_ANALYZER_CACHE"] = cache_dir
    os.environ["YOUTUBE_DEEP_ANALYZER_REQUEST_SPACING_SECONDS"] = "0"
    with pipeline._video_request_gate("shared-video"):
        queue.put(("enter", os.getpid(), time.time()))
        time.sleep(0.08)
        queue.put(("exit", os.getpid(), time.time()))


class TransportPipelineTests(unittest.TestCase):
    def test_caption_bundle_does_not_download_or_transcribe_audio(self) -> None:
        captions = {
            "language": "ko",
            "segments": [{"start_seconds": 0.0, "end_seconds": 2.0, "text": "자막입니다."}],
        }
        with patch.object(pipeline, "fetch_caption_segments", return_value=captions), patch.object(
            pipeline, "prepare_transcript_media"
        ) as prepare_audio, patch.object(pipeline, "transcribe_job_audio_locally") as transcribe:
            result = pipeline.prepare_transcript_bundle("https://www.youtube.com/watch?v=abcdefghijk")
        prepare_audio.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(result["execution_order"], ["captions"])
        self.assertEqual(result["transcript"]["basis"], "normalized_youtube_captions")
        self.assertFalse(result["transcript"]["audio_transcription_performed"])
        self.assertTrue(result["visual_review_may_proceed_without_stt"])

    def test_selective_stt_source_does_not_decode_full_audio_chunks(self) -> None:
        with patch.object(pipeline, "prepare_media", return_value={"analysis_mode": "audio-source-only"}) as prepare:
            result = pipeline.prepare_stt_source_media("https://www.youtube.com/watch?v=abcdefghijk", "test-id")
        self.assertEqual(result["analysis_mode"], "audio-source-only")
        self.assertEqual(prepare.call_args.kwargs["analysis_mode"], "audio-source-only")

    def test_caption_order_prefers_original_then_exact_then_english(self) -> None:
        result = pipeline._caption_candidates(
            ["ko", "en"],
            set(),
            {"ko-orig", "ko", "en"},
        )
        self.assertEqual([item[0] for item in result], ["ko-orig", "ko", "en"])

    def test_429_honors_retry_after(self) -> None:
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FakeHttpError(429, "7")
            return "ok"

        with patch.object(pipeline, "_video_request_gate", no_wait_gate):
            result, diagnostics = pipeline._run_youtube_operation(
                operation,
                stage="captions",
                source_url="https://www.youtube.com/watch?v=abcdefghijk",
                correlation_id="test-429",
            )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(diagnostics["waits_seconds"], [7.0])

    def test_429_uses_three_backoffs_then_returns_structured_error(self) -> None:
        with (
            patch.object(pipeline, "_video_request_gate", no_wait_gate),
            patch.object(pipeline.random, "uniform", return_value=1.0),
        ):
            with self.assertRaises(pipeline.MediaTransportError) as raised:
                pipeline._run_youtube_operation(
                    lambda: (_ for _ in ()).throw(FakeHttpError(429)),
                    stage="captions",
                    source_url="https://www.youtube.com/watch?v=abcdefghijk",
                    correlation_id="test-backoff",
                    selected_languages=["ko-orig", "ko", "en"],
                )
        details = raised.exception.to_dict()
        self.assertEqual(details["http_status"], 429)
        self.assertEqual(details["attempt"], 4)
        self.assertEqual(details["max_attempts"], 4)
        self.assertEqual(details["max_retries"], 3)
        self.assertEqual(details["waits_seconds"], [5.0, 15.0, 45.0])
        self.assertTrue(details["retryable"])

    def test_public_media_403_is_reextracted_once(self) -> None:
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FakeHttpError(403)
            return "audio"

        with patch.object(pipeline, "_video_request_gate", no_wait_gate):
            result, diagnostics = pipeline._run_youtube_operation(
                operation,
                stage="media_download",
                source_url="https://www.youtube.com/watch?v=abcdefghijk",
                correlation_id="test-403",
                format_request="bestaudio/best",
                retry_403_once=True,
            )
        self.assertEqual(result, "audio")
        self.assertEqual(calls, 2)
        self.assertEqual(diagnostics["waits_seconds"], [5.0])

    def test_repeated_403_reports_two_attempts(self) -> None:
        with patch.object(pipeline, "_video_request_gate", no_wait_gate):
            with self.assertRaises(pipeline.MediaTransportError) as raised:
                pipeline._run_youtube_operation(
                    lambda: (_ for _ in ()).throw(FakeHttpError(403)),
                    stage="media_download",
                    source_url="https://www.youtube.com/watch?v=abcdefghijk",
                    correlation_id="test-403-fail",
                    format_request="bestaudio/best",
                    retry_403_once=True,
                )
        details = raised.exception.to_dict()
        self.assertEqual(details["attempt"], 2)
        self.assertEqual(details["max_attempts"], 2)
        self.assertEqual(details["waits_seconds"], [5.0])
        self.assertFalse(details["retryable"])

    def test_fetch_captions_stops_after_first_exact_track_success(self) -> None:
        requested_tracks: list[list[str]] = []

        class FakeYDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def extract_info(self, _url, download):
                info = {
                    "id": "abcdefghijk",
                    "title": "test",
                    "duration": 60,
                    "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "subtitles": {},
                    "automatic_captions": {"ko-orig": [{}], "ko": [{}], "en": [{}]},
                }
                if download:
                    language = list(self.options["subtitleslangs"])
                    requested_tracks.append(language)
                    directory = Path(self.options["outtmpl"]).parent
                    (directory / f"captions.{language[0]}.vtt").write_text(
                        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n",
                        encoding="utf-8",
                    )
                return info

        with tempfile.TemporaryDirectory() as cache_dir:
            with (
                patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_CACHE": cache_dir}),
                patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL),
                patch.object(pipeline, "_video_request_gate", no_wait_gate),
            ):
                result = pipeline.fetch_captions(
                    "https://www.youtube.com/watch?v=abcdefghijk", "ko,en"
                )
        self.assertEqual(result["language"], "ko-orig")
        self.assertEqual(requested_tracks, [["ko-orig"]])
        self.assertEqual(
            result["transport_diagnostics"]["request_policy"],
            "one-exact-track-at-a-time",
        )

    def test_request_gate_serializes_threads(self) -> None:
        active = 0
        maximum = 0
        counter_lock = threading.Lock()

        def guarded() -> None:
            nonlocal active, maximum
            with pipeline._video_request_gate("abcdefghijk"):
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                with counter_lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(
                os.environ,
                {
                    "YOUTUBE_DEEP_ANALYZER_CACHE": cache_dir,
                    "YOUTUBE_DEEP_ANALYZER_REQUEST_SPACING_SECONDS": "0",
                },
            ):
                with ThreadPoolExecutor(max_workers=6) as executor:
                    list(executor.map(lambda _: guarded(), range(12)))
        self.assertEqual(maximum, 1)

    def test_request_gate_serializes_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as cache_dir:
            queue = context.Queue()
            workers = [context.Process(target=process_gate_worker, args=(cache_dir, queue)) for _ in range(2)]
            for worker in workers:
                worker.start()
            events = [queue.get(timeout=10) for _ in range(4)]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
        ordered = sorted(events, key=lambda item: item[2])
        self.assertEqual([item[0] for item in ordered], ["enter", "exit", "enter", "exit"])


if __name__ == "__main__":
    unittest.main()
