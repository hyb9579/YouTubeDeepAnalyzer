from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import media_pipeline as pipeline

SENTINEL = "cookie-sentinel-" + "q7Zp" * 6


@contextmanager
def no_wait_gate(_: str):
    yield {"cooldown_seconds": 0.0, "waited_seconds": 0.0}


class CookieOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cookie_file = self.root / "private" / "cookies.txt"
        self.cookie_file.parent.mkdir()
        self.cookie_file.write_text("# test file without real cookies\n", encoding="utf-8")
        self.cookie_file.chmod(0o600)
        patcher = patch.object(pipeline, "_cookie_permission_warned", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, value: str | None) -> patch.dict:
        environment = {key: item for key, item in os.environ.items() if key != pipeline.COOKIES_ENV}
        if value is not None:
            environment[pipeline.COOKIES_ENV] = value
        return patch.dict(os.environ, environment, clear=True)

    def test_unset_keeps_default_options(self) -> None:
        for value in (None, "", "   "):
            with self.env(value):
                options = pipeline._base_ydl_options()
                self.assertNotIn("cookiefile", options)
                self.assertFalse(pipeline.cookies_enabled())
                self.assertFalse(pipeline.runtime_update_status()["cookies_configured"])
                self.assertTrue(options["ignoreconfig"])

    def test_existing_file_sets_cookiefile(self) -> None:
        with self.env(str(self.cookie_file)):
            options = pipeline._base_ydl_options()
            self.assertEqual(str(self.cookie_file), options["cookiefile"])
            self.assertTrue(pipeline.cookies_enabled())
            self.assertTrue(pipeline.runtime_update_status()["cookies_configured"])
            self.assertTrue(options["ignoreconfig"])

    def test_missing_file_raises_without_revealing_path(self) -> None:
        missing = self.root / "secret-location" / "cookies.txt"
        with self.env(str(missing)):
            with self.assertRaises(pipeline.MediaPipelineError) as raised:
                pipeline._base_ydl_options()
            self.assertNotIn("secret-location", str(raised.exception))
            self.assertFalse(pipeline.cookies_enabled())

    def test_directory_is_rejected(self) -> None:
        with self.env(str(self.cookie_file.parent)):
            with self.assertRaises(pipeline.MediaPipelineError):
                pipeline._base_ydl_options()

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_broad_permissions_warn_without_path(self) -> None:
        self.cookie_file.chmod(0o644)
        stderr = io.StringIO()
        with self.env(str(self.cookie_file)), contextlib.redirect_stderr(stderr):
            pipeline._base_ydl_options()
        self.assertIn("broader than 600", stderr.getvalue())
        self.assertNotIn(str(self.cookie_file), stderr.getvalue())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_private_permissions_do_not_warn(self) -> None:
        stderr = io.StringIO()
        with self.env(str(self.cookie_file)), contextlib.redirect_stderr(stderr):
            pipeline._base_ydl_options()
        self.assertEqual("", stderr.getvalue())

    def test_format_diagnostics_drop_cookie_fields(self) -> None:
        info = {
            "requested_downloads": [
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "cookies": f"SID={SENTINEL}",
                    "http_headers": {"Cookie": SENTINEL},
                    "url": f"https://example.invalid/?t={SENTINEL}",
                }
            ]
        }
        self.assertNotIn(SENTINEL, json.dumps(pipeline._selected_format_diagnostics(info)))

    def test_caption_result_and_cache_never_contain_cookie_data(self) -> None:
        seen_options: list[dict] = []

        class FakeYDL:
            def __init__(self, options):
                self.options = options
                seen_options.append(options)

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
                    "automatic_captions": {"ko": [{"url": "https://example.invalid/cap", "http_headers": {"Cookie": SENTINEL}}]},
                    "formats": [{"format_id": "18", "cookies": SENTINEL, "http_headers": {"Cookie": SENTINEL}}],
                    "http_headers": {"Cookie": SENTINEL},
                    "cookies": SENTINEL,
                }
                if download:
                    directory = Path(self.options["outtmpl"]).parent
                    (directory / "captions.ko.vtt").write_text(
                        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8"
                    )
                return info

        cache_dir = self.root / "cache"
        with (
            self.env(str(self.cookie_file)),
            patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_CACHE": str(cache_dir)}),
            patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL),
            patch.object(pipeline, "_video_request_gate", no_wait_gate),
        ):
            result = pipeline.fetch_captions("https://www.youtube.com/watch?v=abcdefghijk", "ko")

        self.assertTrue(seen_options)
        self.assertTrue(all(options.get("cookiefile") == str(self.cookie_file) for options in seen_options))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(SENTINEL, serialized)
        self.assertNotIn(str(self.cookie_file), serialized)
        self.assertTrue(result["transport_diagnostics"]["cookies_used"])
        for path in cache_dir.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(SENTINEL.encode(), content, path.name)
                self.assertNotIn(str(self.cookie_file).encode(), content, path.name)


if __name__ == "__main__":
    unittest.main()
