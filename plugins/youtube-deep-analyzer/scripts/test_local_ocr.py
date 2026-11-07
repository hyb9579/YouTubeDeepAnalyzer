from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import local_ocr as ocr
from media_pipeline import _targeted_job_id


def recognized(path: Path, engine: str = "apple-vision") -> dict:
    return {"path": str(path), "text": "HELLO", "average_confidence": 0.95, "engine": engine}


def write_test_png(path: Path) -> None:
    glyphs = {"H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
              "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
              "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
              "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"]}
    width, height, scale = 380, 130, 10
    pixels = bytearray([255] * width * height)
    for index, letter in enumerate("HELLO"):
        for y, row in enumerate(glyphs[letter]):
            for x, value in enumerate(row):
                if value == "1":
                    for sy in range(scale):
                        offset = (30 + y * scale + sy) * width + 30 + index * 65 + x * scale
                        pixels[offset:offset + scale] = bytes(scale)
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    raw = b"".join(b"\0" + pixels[y * width:(y + 1) * width] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class LocalOCRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path("frame with spaces.png")
        self.cache = Path("unused-cache")

    def test_explicit_engine_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_OCR_ENGINE": "none"}):
            self.assertEqual(ocr.select_engine("tesseract"), "tesseract")
            self.assertEqual(ocr.select_engine(), "none")

    def test_invalid_engine_is_rejected(self) -> None:
        with self.assertRaises(ocr.OCRError):
            ocr.select_engine("remote")

    def test_disabled_ocr_does_not_call_backends(self) -> None:
        with patch.object(ocr, "run_tesseract") as tesseract, patch.object(ocr, "run_apple_vision") as apple:
            self.assertEqual(ocr.run_local_ocr([self.path], "accurate", "en", self.cache, "none"), ("none", {}, None))
            tesseract.assert_not_called()
            apple.assert_not_called()

    def test_language_codes_are_explicit_and_deduplicated(self) -> None:
        self.assertEqual(ocr.tesseract_languages("ko-KR,en-US,kor,chi_sim"), ["kor", "eng", "chi_sim"])
        with self.assertRaises(ocr.OCRError):
            ocr.tesseract_languages("unsupported-language")

    def test_missing_language_data_is_not_silently_ignored(self) -> None:
        listed = subprocess.CompletedProcess([], 0, "List of available languages (1):\neng\n", "")
        with patch.object(ocr, "find_tesseract", return_value="tesseract"), patch.object(ocr, "_run", return_value=listed) as run:
            with self.assertRaisesRegex(ocr.OCRError, "kor"):
                ocr.run_tesseract([self.path], "ko-KR,en-US")
            self.assertEqual(run.call_count, 1)

    def test_windows_binary_override_can_contain_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "Tesseract OCR" / "tesseract.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"test")
            with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_TESSERACT": str(executable)}), patch.object(ocr.os, "access", return_value=True):
                self.assertEqual(ocr.find_tesseract(), str(executable.resolve()))

    def test_tesseract_parses_tsv_and_streams_image_bytes(self) -> None:
        listed = subprocess.CompletedProcess([], 0, "eng\n", "")
        tsv = "level\tconf\ttext\n5\t95\tHELLO\n5\t85\tWORLD\n"
        with patch.object(Path, "read_bytes", return_value=b"image bytes"), patch.object(ocr, "find_tesseract", return_value="tesseract"), patch.object(ocr, "_run", side_effect=[listed, subprocess.CompletedProcess([], 0, tsv, "")]) as run:
            item = ocr.run_tesseract([self.path], "en")[str(self.path)]
        self.assertEqual(item["text"], "HELLO WORLD")
        self.assertAlmostEqual(item["average_confidence"], 0.9)
        self.assertEqual(run.call_args.args[0][1], "stdin")
        self.assertEqual(run.call_args.kwargs["input_bytes"], b"image bytes")

    def test_tesseract_timeout_is_a_frame_error(self) -> None:
        listed = subprocess.CompletedProcess([], 0, "eng\n", "")
        with patch.object(Path, "read_bytes", return_value=b"image bytes"), patch.object(ocr, "find_tesseract", return_value="tesseract"), patch.object(ocr, "_run", side_effect=[listed, subprocess.TimeoutExpired("tesseract", 120)]):
            self.assertIn("error", ocr.run_tesseract([self.path], "en")[str(self.path)])

    def test_apple_failure_falls_back_to_tesseract(self) -> None:
        with patch.object(ocr.sys, "platform", "darwin"), patch.object(ocr, "run_apple_vision", side_effect=ocr.OCRError("compile failed")), patch.object(ocr, "run_tesseract", return_value={str(self.path): recognized(self.path, "tesseract")}):
            engine, values, error = ocr.run_local_ocr([self.path], "accurate", "en", self.cache, "auto")
        self.assertEqual(engine, "tesseract")
        self.assertIsNone(error)
        self.assertIn("compile failed", values[str(self.path)]["fallback_reasons"][0])

    def test_only_failed_apple_frames_are_retried(self) -> None:
        other = Path("other.png")
        apple_results = {str(self.path): recognized(self.path), str(other): {"error": "image load failed"}}
        with patch.object(ocr.sys, "platform", "darwin"), patch.object(ocr, "run_apple_vision", return_value=apple_results), patch.object(ocr, "run_tesseract", return_value={str(other): recognized(other, "tesseract")}) as tesseract:
            engine, values, error = ocr.run_local_ocr([self.path, other], "accurate", "en", self.cache, "auto")
        self.assertEqual(engine, "apple-vision+tesseract")
        self.assertIsNone(error)
        self.assertEqual(tesseract.call_args.args[0], [other])
        self.assertNotIn("fallback_reasons", values[str(self.path)])

    def test_explicit_apple_does_not_switch_engines(self) -> None:
        with patch.object(ocr, "run_apple_vision", side_effect=ocr.OCRError("unavailable")), patch.object(ocr, "run_tesseract") as tesseract:
            engine, _, error = ocr.run_local_ocr([self.path], "accurate", "en", self.cache, "apple-vision")
        self.assertEqual(engine, "unavailable")
        self.assertIn("unavailable", error)
        tesseract.assert_not_called()

    def test_linux_and_windows_auto_use_tesseract(self) -> None:
        for platform in ["linux", "win32"]:
            with self.subTest(platform=platform), patch.object(ocr.sys, "platform", platform), patch.object(ocr, "run_apple_vision") as apple, patch.object(ocr, "run_tesseract", return_value={str(self.path): recognized(self.path, "tesseract")}):
                self.assertEqual(ocr.run_local_ocr([self.path], "accurate", "en", self.cache, "auto")[0], "tesseract")
                apple.assert_not_called()

    def test_visual_job_cache_separates_engine_and_language(self) -> None:
        args = ("https://youtu.be/test", [], 1.0, 0.08, 24, 720, "accurate")
        self.assertNotEqual(_targeted_job_id(*args, "none", "en"), _targeted_job_id(*args, "tesseract", "en"))
        self.assertNotEqual(_targeted_job_id(*args, "tesseract", "en"), _targeted_job_id(*args, "tesseract", "ko"))


class InstalledOCRTests(unittest.TestCase):
    def test_real_tesseract_recognizes_generated_image(self) -> None:
        if not ocr.find_tesseract():
            if os.environ.get("YOUTUBE_DEEP_ANALYZER_REQUIRE_OCR_TESTS") == "1":
                self.fail("CI requires an installed Tesseract engine.")
            self.skipTest("Tesseract is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "한글 image with spaces.png"
            write_test_png(path)
            engine, values, error = ocr.run_local_ocr([path], "accurate", "en-US", Path(temporary), "tesseract")
            self.assertIsNone(error)
            self.assertEqual(engine, "tesseract")
            self.assertIn("HELLO", values[str(path)]["text"].upper())

    @unittest.skipUnless(sys.platform == "darwin", "Apple Vision is macOS-only")
    def test_real_apple_vision_recognizes_generated_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image with spaces.png"
            write_test_png(path)
            engine, values, error = ocr.run_local_ocr([path], "accurate", "en-US", Path(temporary), "apple-vision")
            self.assertIsNone(error)
            self.assertEqual(engine, "apple-vision")
            self.assertIn("HELLO", values[str(path)]["text"].upper())


if __name__ == "__main__":
    unittest.main()
