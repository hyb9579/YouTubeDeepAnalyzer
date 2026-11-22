"""Tests for check_no_cookies.

Fake cookie data is assembled at runtime so this source file never contains
text that the scanner itself would flag.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_no_cookies as scanner  # noqa: E402

AUTH_NAME = "SAP" + "ISID"
FAKE_VALUE = "Zx9Qw8" * 4 + "fakeOnly"
TAB = "\t"


def netscape_line(name: str = AUTH_NAME, value: str = FAKE_VALUE) -> str:
    return TAB.join([".youtube.com", "TRUE", "/", "TRUE", "1999999999", name, value])


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


class ScannerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first so it runs last, after the working directory is restored.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Never read the real local cookie file from tests.
        patcher = patch.object(scanner, "DEFAULT_COOKIE_FILE", self.root / "missing" / "cookies.txt")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {scanner.COOKIES_ENV: ""})
        env.start()
        self.addCleanup(env.stop)

    def rules(self, text: str, fingerprints: list[str] | None = None, path: str = "notes.md") -> list[str]:
        return [f.rule for f in scanner.scan_blob(path, path, text.encode(), fingerprints or [])]

    def run_main(self, *argv: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = scanner.main(list(argv))
        return code, stderr.getvalue()


class RuleTests(ScannerTestCase):
    def test_detects_netscape_header(self) -> None:
        self.assertIn("netscape-cookie-header", self.rules("# Netscape HTTP " + "Cookie File\n"))

    def test_detects_netscape_line_under_any_file_name(self) -> None:
        self.assertIn("netscape-cookie-line", self.rules(netscape_line() + "\n", path="data/report.csv"))

    def test_detects_value_pasted_into_code(self) -> None:
        source = f'headers = {{"x": "{AUTH_NAME}={FAKE_VALUE}"}}\n'
        self.assertIn("auth-cookie-value", self.rules(source, path="client.py"))

    def test_detects_cookie_header(self) -> None:
        self.assertIn("auth-cookie-value", self.rules("Cookie: PREF=x; " + "SID" + "=" + FAKE_VALUE + "\n"))

    def test_detects_browser_json_export(self) -> None:
        export = '[{"domain": ".youtube' + '.com", "na' + f'me": "{AUTH_NAME}", "value": "redacted"}}]'
        rules = self.rules(export, path="export.data")
        self.assertTrue({"cookie-export-domain", "cookie-export-name"} & set(rules))

    def test_detects_json_value_on_its_own_line(self) -> None:
        export = "{\n" + '  "na' + f'me": "{AUTH_NAME}",\n' + '  "value": "abc"\n}\n'
        self.assertIn("cookie-export-name", self.rules(export, path="x.json5"))

    def test_detects_local_cookie_value_without_any_other_marker(self) -> None:
        self.assertEqual(
            ["local-cookie-value"],
            self.rules(f"token: {FAKE_VALUE[::-1]}{FAKE_VALUE}\n", fingerprints=[FAKE_VALUE]),
        )

    def test_detects_cookie_file_names(self) -> None:
        for name in ("cookies.txt", "yt-cookies.txt", "all-cookies.txt", "browser-cookies.json", "x.cookies"):
            self.assertIn("cookie-file-name", self.rules("harmless\n", path=f"dir/{name}"), name)

    def test_clean_text_has_no_findings(self) -> None:
        text = (
            "Set YOUTUBE_DEEP_ANALYZER_COOKIES to a local Netscape cookie file outside the repository.\n"
            "The SID of a process is unrelated.\n"
            '{"name": "youtube-media", "domain": "example.com"}\n'
        )
        self.assertEqual([], self.rules(text, path="README.md"))

    def test_binary_content_is_skipped_but_name_rule_applies(self) -> None:
        data = b"\0" + netscape_line().encode()
        self.assertEqual([], scanner.scan_text("blob.bin", data, [FAKE_VALUE]))
        self.assertEqual(["cookie-file-name"], [f.rule for f in scanner.scan_blob("c", "cookies.txt", data, [])])


class FingerprintTests(ScannerTestCase):
    def test_loads_only_long_value_fields_from_configured_file(self) -> None:
        cookie_file = self.root / "cookies.txt"
        cookie_file.write_text(
            "# Netscape HTTP " + "Cookie File\n"
            + netscape_line() + "\n"
            + netscape_line("PREF", "short") + "\n"
            + "#HttpOnly_" + netscape_line("HSID", FAKE_VALUE + "2") + "\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {scanner.COOKIES_ENV: str(cookie_file)}):
            values = scanner.load_fingerprints()
        self.assertEqual(sorted([FAKE_VALUE, FAKE_VALUE + "2"]), sorted(values))

    def test_findings_never_print_values(self) -> None:
        cookie_file = self.root / "cookies.txt"
        cookie_file.write_text(netscape_line() + "\n", encoding="utf-8")
        target = self.root / "pasted.json"
        target.write_text(f'{{"token": "{FAKE_VALUE}"}}\n' + netscape_line() + "\n", encoding="utf-8")
        with patch.dict(os.environ, {scanner.COOKIES_ENV: str(cookie_file)}):
            code, output = self.run_main("--paths", str(target))
        self.assertEqual(1, code)
        self.assertIn("local-cookie-value", output)
        self.assertNotIn(FAKE_VALUE, output)
        self.assertNotIn(AUTH_NAME, output)


class GitModeTests(ScannerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "init")
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def test_staged_scan_blocks_pasted_cookie(self) -> None:
        (self.repo / "settings.json").write_text(f'{{"auth": "{AUTH_NAME}={FAKE_VALUE}"}}\n', encoding="utf-8")
        git(self.repo, "add", "settings.json")
        code, output = self.run_main("--staged")
        self.assertEqual(1, code)
        self.assertIn("settings.json:1: auth-cookie-value", output)
        self.assertNotIn(FAKE_VALUE, output)

    def test_staged_scan_passes_clean_change(self) -> None:
        (self.repo / "notes.md").write_text("nothing secret\n", encoding="utf-8")
        git(self.repo, "add", "notes.md")
        self.assertEqual(0, self.run_main("--staged")[0])

    def test_commit_message_scan(self) -> None:
        message = self.root / "MSG"
        message.write_text(f"fix\n\n{netscape_line()}\n", encoding="utf-8")
        self.assertEqual(1, self.run_main("--message-file", str(message))[0])

    def test_pre_push_catches_cookie_added_then_removed(self) -> None:
        (self.repo / "dump.txt").write_text(netscape_line() + "\n", encoding="utf-8")
        git(self.repo, "add", "dump.txt")
        git(self.repo, "commit", "-q", "-m", "add")
        (self.repo / "dump.txt").write_text("gone\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "remove")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        stdin = f"refs/heads/main {head} refs/heads/main {scanner.ZERO_SHA}\n"
        with patch.object(sys, "stdin", io.StringIO(stdin)):
            code, output = self.run_main("--pre-push")
        self.assertEqual(1, code)
        self.assertIn("dump.txt:1: netscape-cookie-line", output)

    def test_pre_push_ignores_branch_deletion(self) -> None:
        stdin = f"(delete) {scanner.ZERO_SHA} refs/heads/old {'1' * 40}\n"
        with patch.object(sys, "stdin", io.StringIO(stdin)):
            self.assertEqual(0, self.run_main("--pre-push")[0])

    def test_scan_failure_blocks(self) -> None:
        stdin = f"refs/heads/main {'f' * 40} refs/heads/main {scanner.ZERO_SHA}\n"
        with patch.object(sys, "stdin", io.StringIO(stdin)):
            self.assertEqual(2, self.run_main("--pre-push")[0])


class RepositoryTests(unittest.TestCase):
    def test_repository_tree_is_clean(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(repo / "scripts" / "check_no_cookies.py"), "--tree"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
