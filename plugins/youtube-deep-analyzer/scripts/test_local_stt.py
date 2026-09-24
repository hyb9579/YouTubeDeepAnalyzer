from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import local_stt


class LocalSTTTests(unittest.TestCase):
    def test_parse_whisper_cpp_json_keeps_only_timestamp_and_text(self) -> None:
        segments, language = local_stt.parse_whisper_json(
            {
                "result": {"language": "ko"},
                "transcription": [
                    {
                        "timestamps": {"from": "00:00:01.250", "to": "00:00:03.500"},
                        "offsets": {"from": 1250, "to": 3500},
                        "text": "  안녕하세요.  ",
                        "speaker": "ignored",
                        "tokens": [1, 2],
                    }
                ],
            }
        )
        self.assertEqual(language, "ko")
        self.assertEqual(segments, [{"start_seconds": 1.25, "end_seconds": 3.5, "text": "안녕하세요."}])

    def test_runtime_status_explicitly_excludes_acoustic_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(local_stt, "find_whisper_cli", return_value=None):
            status = local_stt.stt_runtime_status(Path(temporary))
        self.assertEqual(status["features"], ["speech_to_text", "timestamped_segments", "automatic_language_detection"])
        self.assertIn("speaker_diarization", status["excluded_features"])
        self.assertEqual(status["model_tokens_during_stt"], 0)

    def test_missing_runtime_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(local_stt, "find_whisper_cli", return_value=None):
            with self.assertRaises(local_stt.LocalSTTError) as raised:
                local_stt.transcribe_audio(
                    Path(temporary) / "source.mp3",
                    Path(temporary),
                    "/usr/bin/false",
                    Path(temporary),
                )
        self.assertEqual(raised.exception.to_dict()["stage"], "local_stt")
        self.assertEqual(raised.exception.error_code, "stt_runtime_unavailable")

    def test_cached_result_skips_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "local_stt"
            output.mkdir()
            cached = {
                "available": True,
                "basis": "local_whisper_cpp_stt",
                "segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "test"}],
            }
            (output / "result-full-auto.json").write_text(json.dumps(cached), encoding="utf-8")
            fake_cli = root / "whisper-cli"
            fake_cli.write_text("", encoding="utf-8")
            fake_cli.chmod(0o755)
            fake_model = root / "model.bin"
            fake_model.write_bytes(b"model")
            with patch.object(local_stt, "find_whisper_cli", return_value=fake_cli), patch.object(
                local_stt, "ensure_model", return_value=fake_model
            ):
                result = local_stt.transcribe_audio(root / "source.mp3", root, "/usr/bin/false", root)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["segments"][0]["text"], "test")

    def test_selective_ranges_reject_full_or_unbounded_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(local_stt.LocalSTTError) as raised:
                local_stt.transcribe_audio_ranges(
                    root / "source.mp3",
                    root,
                    "/usr/bin/false",
                    root,
                    [
                        {
                            "start_seconds": 0,
                            "end_seconds": 120,
                            "caption_text": "unclear term",
                            "question": "Was the term alpha or alpa?",
                        }
                    ],
                )
        self.assertEqual(raised.exception.error_code, "invalid_stt_ranges")

    def test_selective_range_shifts_timestamps_to_video_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_cli = root / "whisper-cli"
            fake_cli.write_text("", encoding="utf-8")
            fake_cli.chmod(0o755)
            fake_model = root / "model.bin"
            fake_model.write_bytes(b"model")

            def fake_run(command, **kwargs):
                if "-oj" in command:
                    prefix = Path(command[command.index("-of") + 1])
                    prefix.with_suffix(".json").write_text(
                        json.dumps(
                            {
                                "result": {"language": "ko"},
                                "transcription": [
                                    {"offsets": {"from": 1000, "to": 2500}, "text": "정확한 용어"}
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    Path(command[-1]).write_bytes(b"wav")
                return type("Completed", (), {"returncode": 0, "stderr": ""})()

            with patch.object(local_stt, "find_whisper_cli", return_value=fake_cli), patch.object(
                local_stt, "ensure_model", return_value=fake_model
            ), patch.object(local_stt.subprocess, "run", side_effect=fake_run):
                result = local_stt.transcribe_audio_ranges(
                    root / "source.mp3",
                    root,
                    "/usr/bin/false",
                    root,
                    [
                        {
                            "start_seconds": 30,
                            "end_seconds": 40,
                            "caption_text": "부정확한 용어",
                            "question": "정확한 용어는 무엇인가?",
                        }
                    ],
                    language="ko",
                )
        self.assertFalse(result["full_audio_transcription_performed"])
        self.assertEqual(result["total_verified_seconds"], 10.0)
        self.assertEqual(result["verifications"][0]["segments"][0]["start_seconds"], 31.0)
        self.assertEqual(result["verifications"][0]["stt_text"], "정확한 용어")


if __name__ == "__main__":
    unittest.main()
