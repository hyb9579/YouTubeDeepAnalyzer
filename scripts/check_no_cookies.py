#!/usr/bin/env python3
"""Block browser cookie data from entering git history.

The scanner looks for cookie files and for cookie values pasted into any other
file (JSON, code, notes) or commit message. Findings report only a location and
a rule ID; matched values and matching lines are never printed.

Modes:
  --staged              scan every staged blob (pre-commit)
  --message-file PATH   scan a commit message (commit-msg)
  --pre-push            read git pre-push stdin and scan every outgoing commit
  --range A..B          scan every commit in a revision range
  --tree                scan tracked and untracked, non-ignored working-tree files
  --paths FILE...       scan specific files
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator

COOKIES_ENV = "YOUTUBE_DEEP_ANALYZER_COOKIES"
DEFAULT_COOKIE_FILE = Path.home() / ".config" / "youtube-deep-analyzer" / "cookies.txt"
ZERO_SHA = "0" * 40
MAX_BYTES = 50 * 1024 * 1024
MIN_FINGERPRINT_LENGTH = 10

AUTH_COOKIE_NAMES = (
    r"__Secure-[13]PSID(?:TS|CC)?",
    r"__Secure-[13]PAPISID",
    r"__Secure-YEC",
    r"__Secure-ROLLOUT_TOKEN",
    r"__Secure-OSID",
    r"SAPISID",
    r"APISID",
    r"HSID",
    r"SSID",
    r"SIDCC",
    r"SID",
    r"OSID",
    r"LSID",
    r"LOGIN_INFO",
    r"VISITOR_INFO1_LIVE",
    r"VISITOR_PRIVACY_METADATA",
)
_NAMES = "(?:" + "|".join(AUTH_COOKIE_NAMES) + ")"

LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("netscape-cookie-header", re.compile(r"^\s*#\s*(?:Netscape\s+)?HTTP\s+Cookie\s+File", re.IGNORECASE)),
    (
        "netscape-cookie-line",
        re.compile(r"^\S+\t(?:TRUE|FALSE)\t[^\t]*\t(?:TRUE|FALSE)\t-?\d+\t[^\t]+\t"),
    ),
    (
        "auth-cookie-value",
        re.compile(
            r"(?<![A-Za-z0-9_-])" + _NAMES + r"""["']?\s*(?:=|:|\t)\s*["']?[A-Za-z0-9_\-./%+:=~]{12,}"""
        ),
    ),
    ("cookie-export-name", re.compile(r""""name"\s*:\s*["']""" + _NAMES + r"""["']""")),
    (
        "cookie-export-domain",
        re.compile(r""""(?:domain|host)"\s*:\s*["']\.?(?:[a-z0-9-]+\.)*(?:youtube|google)\.com["']""", re.IGNORECASE),
    ),
)

PATH_PATTERNS = (
    "*cookie*.txt",
    "*cookies*.json",
    "*cookie*.json",
    "*.cookies",
    "cookies",
    "cookies.sqlite*",
)


class Finding:
    def __init__(self, location: str, line: int | None, rule: str) -> None:
        self.location = location
        self.line = line
        self.rule = rule

    def __str__(self) -> str:
        suffix = f":{self.line}" if self.line is not None else ""
        return f"{self.location}{suffix}: {self.rule}"


def _git(*args: str, input_text: str | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        input=input_text.encode() if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def load_fingerprints() -> list[str]:
    """Read value fields from the local cookie file into memory only."""
    candidates = []
    configured = os.environ.get(COOKIES_ENV, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(DEFAULT_COOKIE_FILE)
    values: set[str] = set()
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            print("check_no_cookies: warning: a configured cookie file could not be read for fingerprinting.", file=sys.stderr)
            continue
        for raw in text.splitlines():
            if not raw or (raw.startswith("#") and not raw.startswith("#HttpOnly_")):
                continue
            fields = raw.split("\t")
            if len(fields) >= 7 and len(fields[6].strip()) >= MIN_FINGERPRINT_LENGTH:
                values.add(fields[6].strip())
    return sorted(values, key=len, reverse=True)


def path_rule(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in PATH_PATTERNS)


def scan_text(location: str, data: bytes, fingerprints: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return findings
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in LINE_RULES:
            if pattern.search(line):
                findings.append(Finding(location, number, rule))
                break
        else:
            if fingerprints and any(value in line for value in fingerprints):
                findings.append(Finding(location, number, "local-cookie-value"))
    return findings


def scan_blob(location: str, path: str, data: bytes, fingerprints: list[str]) -> list[Finding]:
    findings = [Finding(location, None, "cookie-file-name")] if path_rule(path) else []
    return findings + scan_text(location, data, fingerprints)


def _split_z(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def scan_staged(fingerprints: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _split_z(_git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")):
        findings += scan_blob(f"(staged) {path}", path, _git("show", f":{path}"), fingerprints)
    return findings


def scan_commits(commits: Iterable[str], fingerprints: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for commit in commits:
        short = commit[:12]
        findings += scan_text(f"(commit {short} message)", _git("log", "-1", "--format=%B", commit), fingerprints)
        changed = _split_z(
            _git("diff-tree", "-r", "--root", "--no-commit-id", "--name-only", "--diff-filter=ACMRT", "-z", commit)
        )
        for path in changed:
            findings += scan_blob(f"(commit {short}) {path}", path, _git("show", f"{commit}:{path}"), fingerprints)
    return findings


def _rev_list(*args: str) -> list[str]:
    return [line for line in _git("rev-list", *args).decode().split() if line]


def outgoing_commits(stdin_text: str) -> list[str]:
    commits: list[str] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, _, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue
        if remote_sha == ZERO_SHA:
            commits += _rev_list(local_sha, "--not", "--remotes")
        else:
            try:
                commits += _rev_list(f"{remote_sha}..{local_sha}")
            except RuntimeError:
                # Remote commit is unknown locally (force push or stale fetch): scan everything not on any remote.
                commits += _rev_list(local_sha, "--not", "--remotes")
    return list(dict.fromkeys(commits))


def scan_tree(fingerprints: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _split_z(_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")):
        file = Path(path)
        if not file.is_file():
            continue
        findings += scan_blob(path, path, file.read_bytes(), fingerprints)
    return findings


def scan_paths(paths: Iterable[str], fingerprints: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        findings += scan_blob(path, path, Path(path).read_bytes(), fingerprints)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--message-file")
    mode.add_argument("--pre-push", action="store_true")
    mode.add_argument("--range", dest="revision_range")
    mode.add_argument("--tree", action="store_true")
    mode.add_argument("--paths", nargs="+")
    args = parser.parse_args(argv)

    fingerprints = load_fingerprints()
    try:
        if args.staged:
            findings = scan_staged(fingerprints)
        elif args.message_file:
            findings = scan_text("(commit message)", Path(args.message_file).read_bytes(), fingerprints)
        elif args.pre_push:
            findings = scan_commits(outgoing_commits(sys.stdin.read()), fingerprints)
        elif args.revision_range:
            findings = scan_commits(_rev_list(args.revision_range), fingerprints)
        elif args.tree:
            findings = scan_tree(fingerprints)
        else:
            findings = scan_paths(args.paths, fingerprints)
    except Exception as error:  # Fail closed: an unscanned change must not pass.
        print(f"check_no_cookies: BLOCKED because the scan could not complete ({type(error).__name__}).", file=sys.stderr)
        return 2

    if not findings:
        return 0
    print("check_no_cookies: BLOCKED. Possible browser cookie data found (values are not shown):", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "Remove the data (and rewrite any local commit that contains it) before retrying. "
        "Do not bypass this check with --no-verify.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
