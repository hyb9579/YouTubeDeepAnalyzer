from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator
from urllib.parse import urlparse


PYPI_METADATA_URL = "https://pypi.org/pypi/yt-dlp/json"
ALLOWED_WHEEL_HOSTS = {"files.pythonhosted.org", "pypi.org"}
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_WHEEL_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
VERSION_PATTERN = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?$")


class RuntimeUpdateError(RuntimeError):
    pass


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def base_runtime_path() -> Path:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_dir():
            return path
    vendor = plugin_root() / "vendor"
    if vendor.is_dir():
        return vendor
    raise RuntimeUpdateError("The base Python runtime is unavailable. Run scripts/bootstrap_runtime.py first.")


def cache_root() -> Path:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_CACHE")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "youtube-deep-analyzer"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "youtube-deep-analyzer"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def runtime_root() -> Path:
    root = cache_root() / "runtime" / "yt-dlp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def bundled_version() -> str:
    requirements = plugin_root() / "requirements.lock"
    match = re.search(r"(?mi)^yt-dlp==([^\s]+)$", requirements.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeUpdateError("requirements.lock does not pin yt-dlp.")
    return match.group(1)


def version_key(value: str) -> tuple[int, int, int, int]:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if not match:
        raise RuntimeUpdateError(f"Refusing non-stable yt-dlp version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def auto_update_enabled() -> bool:
    return os.environ.get("YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def check_interval_seconds() -> float:
    value = os.environ.get("YOUTUBE_DEEP_ANALYZER_UPDATE_INTERVAL_SECONDS")
    if value is None:
        return float(DEFAULT_CHECK_INTERVAL_SECONDS)
    try:
        return min(7 * 24 * 60 * 60, max(300.0, float(value)))
    except ValueError:
        return float(DEFAULT_CHECK_INTERVAL_SECONDS)


def _read_limited(response: Any, maximum: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > maximum:
                raise RuntimeUpdateError(f"Remote response exceeds the {maximum}-byte safety limit.")
        except ValueError:
            pass
    payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise RuntimeUpdateError(f"Remote response exceeds the {maximum}-byte safety limit.")
    return payload


def _open_https(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "youtube-deep-analyzer-runtime-updater/1.0"},
    )
    return urllib.request.urlopen(request, timeout=timeout)


def latest_stable_release() -> dict[str, str]:
    with _open_https(PYPI_METADATA_URL, timeout=5.0) as response:
        payload = json.loads(_read_limited(response, MAX_METADATA_BYTES))
    version = str((payload.get("info") or {}).get("version") or "")
    version_key(version)
    wheels = [
        item
        for item in payload.get("urls") or []
        if item.get("packagetype") == "bdist_wheel"
        and not item.get("yanked")
        and str(item.get("filename") or "").endswith("py3-none-any.whl")
    ]
    if len(wheels) != 1:
        raise RuntimeUpdateError("PyPI did not return exactly one universal yt-dlp wheel.")
    wheel = wheels[0]
    url = str(wheel.get("url") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_WHEEL_HOSTS:
        raise RuntimeUpdateError("PyPI returned an untrusted yt-dlp wheel URL.")
    digest = str(((wheel.get("digests") or {}).get("sha256")) or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeUpdateError("PyPI did not provide a valid SHA-256 wheel digest.")
    return {"version": version, "url": url, "sha256": digest, "filename": str(wheel["filename"])}


def _safe_extract_wheel(payload: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_EXTRACTED_BYTES:
            raise RuntimeUpdateError("The yt-dlp wheel exceeds the extracted-size safety limit.")
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeUpdateError("The yt-dlp wheel contains an unsafe path.")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise RuntimeUpdateError("The yt-dlp wheel contains a symbolic link.")
            target = (destination / Path(*relative.parts)).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise RuntimeUpdateError("The yt-dlp wheel escaped the runtime directory.")
        archive.extractall(destination)


def _validate_runtime(path: Path, expected_version: str) -> None:
    if not (path / "yt_dlp" / "__init__.py").is_file():
        raise RuntimeUpdateError("The candidate runtime does not contain yt_dlp.")
    code = (
        "import re,sys; "
        "sys.path.insert(0, sys.argv[3]); "
        "sys.path.insert(0, sys.argv[4]); "
        "sys.path.insert(0, sys.argv[1]); "
        "from yt_dlp.version import __version__; "
        "from yt_dlp import YoutubeDL; "
        "key=lambda v: tuple(int(x or 0) for x in re.fullmatch(r'(\\d{4})\\.(\\d{1,2})\\.(\\d{1,2})(?:\\.(\\d+))?', v).groups()); "
        "assert key(__version__) == key(sys.argv[2]), (__version__, sys.argv[2]); "
        "YoutubeDL({'quiet': True}); "
        "import media_pipeline; "
        "assert key(media_pipeline._yt_dlp_version()) == key(sys.argv[2]); "
        "media_pipeline._base_ydl_options(); "
        "print(__version__)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(path),
            expected_version,
            str(base_runtime_path()),
            str(plugin_root() / "scripts"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeUpdateError(f"Candidate yt-dlp import validation failed: {result.stderr.strip()[-1000:]}")


def install_release(release: dict[str, str], root: Path) -> Path:
    version = release["version"]
    target = root / version
    if target.is_dir():
        try:
            _validate_runtime(target, version)
            return target
        except RuntimeUpdateError:
            target.replace(root / f".rejected-{version}-{uuid.uuid4().hex}")
    with _open_https(release["url"], timeout=30.0) as response:
        wheel = _read_limited(response, MAX_WHEEL_BYTES)
    actual_digest = hashlib.sha256(wheel).hexdigest()
    if actual_digest != release["sha256"]:
        raise RuntimeUpdateError("The downloaded yt-dlp wheel failed SHA-256 verification.")
    temporary = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
    try:
        _safe_extract_wheel(wheel, temporary)
        _validate_runtime(temporary, version)
        try:
            temporary.replace(target)
        except FileExistsError:
            shutil.rmtree(temporary, ignore_errors=True)
        _validate_runtime(target, version)
        return target
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _state_path(root: Path) -> Path:
    return root / "state.json"


def read_state(root: Path | None = None) -> dict[str, Any]:
    selected_root = root or runtime_root()
    try:
        return json.loads(_state_path(selected_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _usable_cached_runtime(root: Path, state: dict[str, Any], bundled: str) -> tuple[str, Path | None]:
    active_version = str(state.get("active_version") or "")
    active_path_text = str(state.get("active_path") or "")
    if not active_version or not active_path_text:
        return bundled, None
    try:
        if version_key(active_version) < version_key(bundled):
            return bundled, None
    except RuntimeUpdateError:
        return bundled, None
    active_path = Path(active_path_text).resolve()
    if root.resolve() not in active_path.parents or not (active_path / "yt_dlp" / "__init__.py").is_file():
        return bundled, None
    try:
        _validate_runtime(active_path, active_version)
    except RuntimeUpdateError:
        return bundled, None
    return active_version, active_path


def _lock_file(file_object: BinaryIO, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        import msvcrt

        file_object.seek(0, os.SEEK_END)
        if file_object.tell() == 0:
            file_object.write(b"0")
            file_object.flush()
        while True:
            try:
                file_object.seek(0)
                msvcrt.locking(file_object.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeUpdateError("Timed out waiting for the runtime update lock.")
                time.sleep(0.1)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(file_object.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeUpdateError("Timed out waiting for the runtime update lock.")
                time.sleep(0.1)


def _unlock_file(file_object: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        msvcrt.locking(file_object.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)


@contextmanager
def update_lock(root: Path) -> Iterator[None]:
    path = root / "update.lock"
    with path.open("a+b") as file_object:
        _lock_file(file_object)
        try:
            yield
        finally:
            _unlock_file(file_object)


def resolve_runtime(force: bool = False, now: float | None = None) -> dict[str, Any]:
    root = runtime_root()
    bundled = bundled_version()
    current_time = time.time() if now is None else now
    with update_lock(root):
        state = read_state(root)
        active_version, active_path = _usable_cached_runtime(root, state, bundled)
        enabled = auto_update_enabled()
        last_check = float(state.get("last_check_epoch") or 0)
        check_due = force or current_time - last_check >= check_interval_seconds()
        if not enabled or not check_due:
            result = {
                **state,
                "ok": True,
                "auto_update_enabled": enabled,
                "check_performed": False,
                "outcome": (
                    "using-cached-overlay"
                    if active_path
                    else "auto-update-disabled"
                    if not enabled
                    else "check-deferred"
                ),
                "last_update_outcome": state.get("last_update_outcome") or state.get("outcome"),
                "bundled_version": bundled,
                "active_version": active_version,
                "active_path": str(active_path) if active_path else None,
                "using_bundled_fallback": active_path is None,
            }
            _write_state(root, result)
            return result

        previous_version = active_version
        previous_path = active_path
        try:
            release = latest_stable_release()
            latest = release["version"]
            if version_key(latest) > version_key(active_version):
                active_path = install_release(release, root)
                active_version = latest
                outcome = "updated"
            else:
                outcome = "already-current"
            result = {
                "ok": True,
                "auto_update_enabled": True,
                "check_performed": True,
                "outcome": outcome,
                "last_check_epoch": current_time,
                "last_check_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_time)),
                "bundled_version": bundled,
                "latest_stable_version": latest,
                "active_version": active_version,
                "active_path": str(active_path) if active_path else None,
                "previous_active_version": previous_version,
                "using_bundled_fallback": active_path is None,
                "source": PYPI_METADATA_URL,
                "wheel_sha256": release["sha256"],
                "error": None,
            }
        except Exception as exc:
            result = {
                **state,
                "ok": True,
                "auto_update_enabled": True,
                "check_performed": True,
                "outcome": "fallback-after-update-error",
                "last_check_epoch": current_time,
                "last_check_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_time)),
                "bundled_version": bundled,
                "active_version": previous_version,
                "active_path": str(previous_path) if previous_path else None,
                "using_bundled_fallback": previous_path is None,
                "source": PYPI_METADATA_URL,
                "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            }
        _write_state(root, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve a verified yt-dlp runtime overlay.")
    parser.add_argument("--force", action="store_true", help="Ignore the update-check interval.")
    parser.add_argument("--print-path", action="store_true", help="Print only the selected overlay path.")
    parser.add_argument("--status-json", action="store_true", help="Print the complete update status as JSON.")
    args = parser.parse_args()
    try:
        result = resolve_runtime(force=args.force)
    except Exception as exc:
        try:
            fallback_version = bundled_version()
        except Exception:
            fallback_version = "unknown"
        result = {
            "ok": True,
            "auto_update_enabled": auto_update_enabled(),
            "check_performed": False,
            "outcome": "launcher-fallback-after-update-error",
            "bundled_version": fallback_version,
            "active_version": fallback_version,
            "active_path": None,
            "using_bundled_fallback": True,
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }
    if result.get("outcome") == "updated":
        print(
            f"youtube-deep-analyzer: verified yt-dlp {result['active_version']} runtime update installed.",
            file=sys.stderr,
        )
    elif result.get("error"):
        print(
            "youtube-deep-analyzer: yt-dlp update check failed; continuing with the last verified or bundled runtime.",
            file=sys.stderr,
        )
    if args.status_json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.print_path:
        print(result.get("active_path") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
