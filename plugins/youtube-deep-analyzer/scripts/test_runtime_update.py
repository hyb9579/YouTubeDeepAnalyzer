from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import runtime_update


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self.payload if amount < 0 else self.payload[:amount]


class RuntimeUpdateTests(unittest.TestCase):
    def test_version_comparison_normalizes_zero_padding(self) -> None:
        self.assertEqual(runtime_update.version_key("2026.8.19"), runtime_update.version_key("2026.08.19"))
        self.assertGreater(runtime_update.version_key("2026.9.1"), runtime_update.version_key("2026.08.19"))

    def test_prerelease_is_rejected(self) -> None:
        with self.assertRaises(runtime_update.RuntimeUpdateError):
            runtime_update.version_key("2026.9.1.dev0")

    def test_wheel_path_traversal_is_rejected(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("../escape.py", "bad")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(runtime_update.RuntimeUpdateError):
                runtime_update._safe_extract_wheel(payload.getvalue(), Path(directory))

    def test_wheel_sha256_mismatch_falls_before_extraction(self) -> None:
        payload = b"not-a-wheel"
        release = {
            "version": "2026.9.1",
            "url": "https://files.pythonhosted.org/test.whl",
            "sha256": "0" * 64,
            "filename": "yt_dlp-test-py3-none-any.whl",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runtime_update, "_open_https", return_value=FakeResponse(payload)):
                with self.assertRaisesRegex(runtime_update.RuntimeUpdateError, "SHA-256"):
                    runtime_update.install_release(release, Path(directory))

    def test_new_stable_release_becomes_active_overlay(self) -> None:
        release = {
            "version": "2026.9.1",
            "url": "https://files.pythonhosted.org/test.whl",
            "sha256": hashlib.sha256(b"test").hexdigest(),
            "filename": "yt_dlp-test-py3-none-any.whl",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def fake_install(_release, selected_root: Path) -> Path:
                target = selected_root / _release["version"]
                (target / "yt_dlp").mkdir(parents=True)
                (target / "yt_dlp" / "__init__.py").write_text("", encoding="utf-8")
                return target

            with (
                patch.object(runtime_update, "runtime_root", return_value=root),
                patch.object(runtime_update, "bundled_version", return_value="2026.8.19"),
                patch.object(runtime_update, "latest_stable_release", return_value=release),
                patch.object(runtime_update, "install_release", side_effect=fake_install),
                patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE": "1"}),
            ):
                result = runtime_update.resolve_runtime(force=True, now=1_800_000_000)
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(result["active_version"], "2026.9.1")
        self.assertFalse(result["using_bundled_fallback"])

    def test_offline_check_falls_back_without_blocking_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(runtime_update, "runtime_root", return_value=Path(directory)),
                patch.object(runtime_update, "bundled_version", return_value="2026.8.19"),
                patch.object(runtime_update, "latest_stable_release", side_effect=OSError("offline")),
                patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE": "1"}),
            ):
                result = runtime_update.resolve_runtime(force=True, now=1_800_000_000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "fallback-after-update-error")
        self.assertTrue(result["using_bundled_fallback"])
        self.assertIn("offline", result["error"])

    def test_failed_candidate_keeps_last_verified_overlay(self) -> None:
        release = {
            "version": "2026.9.1",
            "url": "https://files.pythonhosted.org/test.whl",
            "sha256": hashlib.sha256(b"test").hexdigest(),
            "filename": "yt_dlp-test-py3-none-any.whl",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "2026.8.19"
            with (
                patch.object(runtime_update, "runtime_root", return_value=root),
                patch.object(runtime_update, "bundled_version", return_value="2026.7.4"),
                patch.object(
                    runtime_update,
                    "_usable_cached_runtime",
                    return_value=("2026.8.19", previous),
                ),
                patch.object(runtime_update, "latest_stable_release", return_value=release),
                patch.object(
                    runtime_update,
                    "install_release",
                    side_effect=runtime_update.RuntimeUpdateError("incompatible"),
                ),
                patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE": "1"}),
            ):
                result = runtime_update.resolve_runtime(force=True, now=1_800_000_000)
        self.assertEqual(result["outcome"], "fallback-after-update-error")
        self.assertEqual(result["active_version"], "2026.8.19")
        self.assertEqual(result["active_path"], str(previous))
        self.assertFalse(result["using_bundled_fallback"])


if __name__ == "__main__":
    unittest.main()
