from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class BootstrapError(RuntimeError):
    pass


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def requirements_path() -> Path:
    return plugin_root() / "requirements.lock"


def lock_digest() -> str:
    return hashlib.sha256(requirements_path().read_bytes()).hexdigest()


def cache_root() -> Path:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_DEPENDENCY_CACHE", "").strip()
    claude_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if override:
        root = Path(override).expanduser()
    elif claude_data:
        root = Path(claude_data).expanduser() / "python-runtime"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "youtube-deep-analyzer" / "runtime" / "python"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "youtube-deep-analyzer" / "runtime" / "python"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _validate(path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import mcp, yt_dlp, imageio_ffmpeg; "
                "from mcp.server.mcpserver import MCPServer; print('ok')"
            ),
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError(f"Python runtime validation failed: {result.stderr.strip()[-1500:]}")


def _lock_file(handle: BinaryIO, timeout_seconds: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise BootstrapError("Timed out waiting for the dependency installation lock.")
                time.sleep(0.2)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BootstrapError("Timed out waiting for the dependency installation lock.")
                time.sleep(0.2)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def install_lock(root: Path) -> Iterator[None]:
    with (root / "install.lock").open("a+b") as handle:
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def resolve_runtime() -> Path:
    if sys.version_info < (3, 11):
        raise BootstrapError("Python 3.11 or newer is required by the pinned runtime dependencies.")
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME", "").strip()
    if override:
        selected = Path(override).expanduser().resolve()
        _validate(selected)
        return selected

    root = cache_root()
    digest = lock_digest()
    target = root / f"py{sys.version_info.major}{sys.version_info.minor}-{digest[:16]}"
    with install_lock(root):
        if target.is_dir():
            try:
                _validate(target)
                return target
            except BootstrapError:
                target.replace(root / f".rejected-{target.name}-{int(time.time())}")

        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=root))
        try:
            command = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-compile",
                "--target",
                str(temporary),
                "--requirement",
                str(requirements_path()),
            ]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800)
            if result.returncode != 0:
                raise BootstrapError(
                    "Unable to install the pinned Python runtime from PyPI. "
                    + (result.stderr.strip() or result.stdout.strip())[-2000:]
                )
            _validate(temporary)
            (temporary / "youtube-deep-analyzer-runtime.json").write_text(
                json.dumps(
                    {
                        "requirements_sha256": digest,
                        "python": sys.version,
                        "created_at_epoch": time.time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            try:
                temporary.replace(target)
            except FileExistsError:
                shutil.rmtree(temporary, ignore_errors=True)
            _validate(target)
            return target
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or locate the pinned YouTube Deep Analyzer Python runtime.")
    parser.add_argument("--print-path", action="store_true")
    parser.add_argument("--status-json", action="store_true")
    args = parser.parse_args()
    try:
        path = resolve_runtime()
    except Exception as exc:
        print(f"youtube-deep-analyzer dependency bootstrap failed: {exc}", file=sys.stderr)
        return 1
    if args.status_json:
        print(json.dumps({"ok": True, "path": str(path), "requirements_sha256": lock_digest()}))
    else:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
