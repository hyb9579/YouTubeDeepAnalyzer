from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

import imageio_ffmpeg
import yt_dlp

from local_ocr import OCRError, find_tesseract, run_local_ocr, select_engine

from local_stt import (
    LocalSTTError,
    stt_runtime_status as _stt_runtime_status,
    transcribe_audio,
    transcribe_audio_ranges,
)


ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".webm"}
VISUAL_QUESTION_KINDS = {
    "onscreen_text",
    "chart",
    "diagram",
    "spatial",
    "equation",
    "demonstration",
    "motion",
    "before_after",
    "unknown",
}
SEMANTIC_VISUAL_KINDS = VISUAL_QUESTION_KINDS - {"onscreen_text"}
WARNING_TERMS = {
    "warning", "error", "unauthorized", "correction", "danger", "disconnect", "gap", "crack",
    "경고", "오류", "에러", "정정", "주의", "위험", "금지", "분리", "균열", "틈",
}
UNIT_PATTERN = re.compile(
    r"%|(?<![A-Za-z가-힣])(?:ms|s|sec|seconds?|px|kb|mb|gb|tb|ml|l|mg|g|kg|km|cm|mm|hz|khz|mhz|ghz|usd|krw|원|달러|퍼센트)(?![A-Za-z가-힣])",
    flags=re.IGNORECASE,
)


class MediaPipelineError(RuntimeError):
    pass


class CaptionUnavailableError(MediaPipelineError):
    """Raised when metadata is accessible but no requested caption track exists."""


class MediaTransportError(MediaPipelineError):
    """A reproducible YouTube transport failure with stable machine-readable fields."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        error_code: str,
        video_id: str,
        correlation_id: str,
        http_status: int | None = None,
        retryable: bool = False,
        attempt: int = 1,
        max_attempts: int = 1,
        waits_seconds: list[float] | None = None,
        retry_after_seconds: float | None = None,
        selected_languages: list[str] | None = None,
        language_attempts: list[dict[str, Any]] | None = None,
        format_request: str | None = None,
        selected_format: dict[str, Any] | None = None,
        player_client: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details = {
            "stage": stage,
            "error_code": error_code,
            "http_status": http_status,
            "retryable": retryable,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "max_retries": max(0, max_attempts - 1),
            "waits_seconds": waits_seconds or [],
            "retry_after_seconds": retry_after_seconds,
            "selected_languages": selected_languages or [],
            "language_attempts": language_attempts or [],
            "video_id": video_id,
            "yt_dlp_version": _yt_dlp_version(),
            "request_correlation_id": correlation_id,
            "format_request": format_request,
            "selected_format": selected_format,
            "player_client": player_client,
            "js_runtime": _js_runtime_diagnostic(),
            "runtime_update": runtime_update_status(),
            "cause_type": cause_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), **self.details}


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_CAPTION_CACHE_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_REQUEST_SPACING_SECONDS = 1.25
_RATE_LIMIT_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
_MEDIA_403_RETRY_SECONDS = 5.0


class _YDLLogger:
    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            return

    def warning(self, message: str) -> None:
        print(f"yt-dlp warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"yt-dlp error: {message}", file=sys.stderr)


def _yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__

        return str(__version__)
    except Exception:
        return str(getattr(yt_dlp, "__version__", "unknown"))


def _js_runtime_diagnostic() -> dict[str, Any]:
    node_path = shutil.which("node")
    return {"name": "node", "available": bool(node_path), "path": node_path}


def _correlation_id() -> str:
    return uuid.uuid4().hex


def _video_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        identifier = parsed.path.strip("/").split("/")[0]
    else:
        query = parse_qs(parsed.query)
        identifier = (query.get("v") or query.get("list") or [""])[0]
    return identifier or hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _lock_file(file_object: Any, timeout_seconds: float = 180.0) -> None:
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
                    raise MediaPipelineError("Timed out waiting for the per-video request gate.")
                time.sleep(0.1)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(file_object.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MediaPipelineError("Timed out waiting for the per-video request gate.")
                time.sleep(0.1)


def _unlock_file(file_object: Any) -> None:
    if os.name == "nt":
        import msvcrt

        file_object.seek(0)
        msvcrt.locking(file_object.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)


def _request_spacing_seconds() -> float:
    value = os.environ.get("YOUTUBE_DEEP_ANALYZER_REQUEST_SPACING_SECONDS")
    if value is None:
        return _DEFAULT_REQUEST_SPACING_SECONDS
    try:
        return min(30.0, max(0.0, float(value)))
    except ValueError:
        return _DEFAULT_REQUEST_SPACING_SECONDS


@contextmanager
def _video_request_gate(video_key: str) -> Iterator[dict[str, Any]]:
    """Serialize a video's YouTube requests across threads and MCP processes."""
    safe_key = hashlib.sha256(video_key.encode("utf-8")).hexdigest()[:24]
    gate_dir = cache_root() / "request-gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    lock_path = gate_dir / f"{safe_key}.lock"
    state_path = gate_dir / f"{safe_key}.json"
    local_lock = _thread_lock(safe_key)
    with local_lock:
        with lock_path.open("a+b") as lock_file:
            _lock_file(lock_file)
            gate_state: dict[str, Any] = {"cooldown_seconds": 0.0, "waited_seconds": 0.0}
            try:
                state: dict[str, Any] = {}
                if state_path.is_file():
                    try:
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        state = {}
                now = time.time()
                ready_at = max(
                    float(state.get("last_request_epoch") or 0) + _request_spacing_seconds(),
                    float(state.get("blocked_until_epoch") or 0),
                )
                wait_seconds = max(0.0, ready_at - now)
                if wait_seconds:
                    time.sleep(wait_seconds)
                gate_state["waited_seconds"] = round(wait_seconds, 3)
                yield gate_state
            finally:
                try:
                    finished = time.time()
                    cooldown = max(0.0, float(gate_state.get("cooldown_seconds") or 0))
                    state = {
                        "last_request_epoch": finished,
                        "blocked_until_epoch": finished + cooldown,
                        "updated_by_pid": os.getpid(),
                    }
                    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
                    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
                    temporary.replace(state_path)
                finally:
                    _unlock_file(lock_file)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        exc_info = getattr(current, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            pending.append(exc_info[1])
        for name in ("cause", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)


def _retry_after_seconds(value: Any, now: float | None = None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return min(300.0, max(0.0, float(text)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            reference = time.time() if now is None else now
            return min(300.0, max(0.0, parsed.timestamp() - reference))
        except (TypeError, ValueError, OverflowError):
            return None


def _transport_details(exc: BaseException) -> tuple[int | None, float | None, str]:
    for current in _exception_chain(exc):
        status = getattr(current, "status", None) or getattr(current, "code", None)
        if isinstance(status, int):
            headers = getattr(current, "headers", None)
            if headers is None:
                response = getattr(current, "response", None)
                headers = getattr(response, "headers", None)
            retry_after = None
            if headers is not None:
                try:
                    retry_after = _retry_after_seconds(
                        headers.get("Retry-After") or headers.get("retry-after")
                    )
                except Exception:
                    retry_after = None
            return status, retry_after, type(current).__name__
    message = str(exc)
    match = re.search(r"HTTP Error\s+(\d{3})", message, flags=re.IGNORECASE)
    return (int(match.group(1)) if match else None), None, type(exc).__name__


def _fallback_wait(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return round(retry_after, 3)
    index = min(max(0, attempt - 1), len(_RATE_LIMIT_BACKOFF_SECONDS) - 1)
    base = _RATE_LIMIT_BACKOFF_SECONDS[index]
    return round(base * random.uniform(0.8, 1.2), 3)


def _error_code(stage: str, status: int | None) -> str:
    if status is not None:
        return f"youtube_http_{status}"
    return f"youtube_{stage}_failed"


def _run_youtube_operation(
    operation: Callable[[], Any],
    *,
    stage: str,
    source_url: str,
    correlation_id: str,
    selected_languages: list[str] | None = None,
    language_attempts: list[dict[str, Any]] | None = None,
    format_request: str | None = None,
    retry_403_once: bool = False,
    diagnostic_context: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    max_attempts = 4
    waits: list[float] = []
    video_id = _video_key(source_url)
    last_retry_after: float | None = None
    for attempt in range(1, max_attempts + 1):
        with _video_request_gate(video_id) as gate:
            try:
                result = operation()
                return result, {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "waits_seconds": waits,
                    "request_gate_waited_seconds": gate["waited_seconds"],
                }
            except Exception as exc:
                status, retry_after, cause_type = _transport_details(exc)
                retry_429 = status == 429 and attempt < max_attempts
                retry_403 = status == 403 and retry_403_once and attempt == 1
                if language_attempts:
                    language_attempts[-1].update(
                        {
                            "status": "retrying" if retry_429 or retry_403 else "failed",
                            "http_status": status,
                            "request_attempt": attempt,
                        }
                    )
                if retry_429:
                    wait = _fallback_wait(attempt, retry_after)
                elif retry_403:
                    wait = _MEDIA_403_RETRY_SECONDS
                else:
                    recommended_retry_after = retry_after or last_retry_after
                    if status == 429:
                        recommended_retry_after = _fallback_wait(attempt, retry_after)
                        gate["cooldown_seconds"] = recommended_retry_after
                    raise MediaTransportError(
                        f"YouTube {stage} request failed: {exc}",
                        stage=stage,
                        error_code=_error_code(stage, status),
                        http_status=status,
                        retryable=status == 429,
                        attempt=attempt,
                        max_attempts=max_attempts if status == 429 else (2 if retry_403_once and status == 403 else attempt),
                        waits_seconds=waits,
                        retry_after_seconds=recommended_retry_after,
                        selected_languages=selected_languages,
                        language_attempts=language_attempts,
                        video_id=video_id,
                        correlation_id=correlation_id,
                        format_request=format_request,
                        selected_format=(diagnostic_context or {}).get("selected_format"),
                        player_client=(diagnostic_context or {}).get("player_client"),
                        cause_type=cause_type,
                    ) from exc
                waits.append(wait)
                last_retry_after = retry_after
                gate["cooldown_seconds"] = wait
    raise AssertionError("unreachable")


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


def runtime_update_status() -> dict[str, Any]:
    path = cache_root() / "runtime" / "yt-dlp" / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        state = {}
    return {
        "loaded_yt_dlp_version": _yt_dlp_version(),
        "runtime_source": "verified-cache-overlay"
        if os.environ.get("YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH")
        else "bundled",
        "auto_update_enabled": os.environ.get("YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        "check_performed": state.get("check_performed"),
        "outcome": state.get("outcome") or "no-state",
        "last_update_outcome": state.get("last_update_outcome"),
        "bundled_version": state.get("bundled_version"),
        "latest_stable_version": state.get("latest_stable_version"),
        "active_version": state.get("active_version") or _yt_dlp_version(),
        "using_bundled_fallback": state.get("using_bundled_fallback", True),
        "last_check_utc": state.get("last_check_utc"),
        "source": state.get("source"),
        "error": state.get("error"),
    }


def validate_youtube_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise MediaPipelineError("Only http(s) YouTube URLs are accepted.")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise MediaPipelineError("The youtu.be URL does not contain a video ID.")
    if host != "youtu.be" and parsed.path == "/watch" and not parse_qs(parsed.query).get("v"):
        raise MediaPipelineError("The YouTube watch URL does not contain a video ID.")
    return value


def _base_ydl_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "logger": _YDLLogger(),
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "ignoreconfig": True,
        "geo_bypass": False,
        "nocheckcertificate": False,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
    }
    node_path = shutil.which("node")
    if node_path:
        options["js_runtimes"] = {"node": {"path": node_path}}
    return options


def _caption_summary(info: dict[str, Any]) -> dict[str, Any]:
    manual = sorted((info.get("subtitles") or {}).keys())
    automatic = sorted((info.get("automatic_captions") or {}).keys())
    return {
        "available": bool(manual or automatic),
        "manual_languages": manual,
        "automatic_languages": automatic,
    }


def _compact_video_info(info: dict[str, Any], position: int | None = None) -> dict[str, Any]:
    webpage_url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    video_id = info.get("id")
    if video_id and (not webpage_url or not str(webpage_url).startswith("http")):
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "position": position,
        "id": video_id,
        "title": info.get("title"),
        "url": webpage_url,
        "duration_seconds": info.get("duration"),
        "channel": info.get("channel") or info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "availability": info.get("availability") or "unknown",
        "captions": _caption_summary(info),
    }


def probe_youtube(
    url: str,
    detailed_playlist: bool = True,
    max_items: int = 500,
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id_value or _correlation_id()
    if max_items < 1 or max_items > 5000:
        raise MediaPipelineError("max_items must be between 1 and 5000.")

    options = _base_ydl_options()
    options.update(
        {
            "skip_download": True,
            "ignoreerrors": True,
            "extract_flat": False if detailed_playlist else "in_playlist",
            "playlistend": max_items,
        }
    )
    def extract() -> Any:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(source_url, download=False)

    info, request_diagnostics = _run_youtube_operation(
        extract,
        stage="metadata",
        source_url=source_url,
        correlation_id=correlation_id,
    )
    if not info:
        raise MediaTransportError(
            "YouTube returned no accessible metadata for this URL.",
            stage="metadata",
            error_code="youtube_metadata_empty",
            video_id=_video_key(source_url),
            correlation_id=correlation_id,
            attempt=request_diagnostics["attempt"],
            max_attempts=request_diagnostics["max_attempts"],
            waits_seconds=request_diagnostics["waits_seconds"],
        )

    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries") is not None:
        entries: list[dict[str, Any]] = []
        unavailable = 0
        for position, item in enumerate(info.get("entries") or [], start=1):
            if not item:
                unavailable += 1
                entries.append(
                    {
                        "position": position,
                        "id": None,
                        "title": "Unavailable video",
                        "url": None,
                        "duration_seconds": None,
                        "channel": None,
                        "upload_date": None,
                        "availability": "unavailable",
                        "captions": {"available": False, "manual_languages": [], "automatic_languages": []},
                    }
                )
                continue
            entries.append(_compact_video_info(item, position))

        known_duration = sum(float(x["duration_seconds"]) for x in entries if x.get("duration_seconds"))
        return {
            "kind": "playlist",
            "source_url": source_url,
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "item_count": len(entries),
            "unavailable_count": unavailable,
            "known_total_duration_seconds": known_duration,
            "entries": entries,
            "transport_diagnostics": {
                **request_diagnostics,
                "request_correlation_id": correlation_id,
                "yt_dlp_version": _yt_dlp_version(),
                "runtime_update": runtime_update_status(),
            },
        }

    return {
        "kind": "video",
        "source_url": source_url,
        "video": _compact_video_info(info, 1),
        "transport_diagnostics": {
            **request_diagnostics,
            "request_correlation_id": correlation_id,
            "yt_dlp_version": _yt_dlp_version(),
            "runtime_update": runtime_update_status(),
        },
    }


def _caption_candidates(
    requested: list[str], manual: set[str], automatic: set[str]
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    available = manual | automatic
    for requested_language in requested:
        expanded = [requested_language]
        if not requested_language.lower().endswith("-orig"):
            expanded = [f"{requested_language}-orig", requested_language]
        for language in expanded:
            if language not in available:
                continue
            basis = "manual captions" if language in manual else "automatic captions"
            candidate = (language, basis)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _caption_cache_path(source_url: str, languages: list[str]) -> Path:
    digest = hashlib.sha256(f"{source_url}\n{','.join(languages)}".encode("utf-8")).hexdigest()[:24]
    return cache_root() / "caption-results" / f"{digest}.json"


def _read_caption_cache(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > _CAPTION_CACHE_TTL_SECONDS:
            return None
        result = json.loads(path.read_text(encoding="utf-8"))
        result["cache_hit"] = True
        return result
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _write_caption_cache(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def fetch_captions(
    url: str,
    preferred_languages: str = "ko,en",
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id_value or _correlation_id()
    languages = [item.strip() for item in preferred_languages.split(",") if item.strip()]
    if not languages or len(languages) > 10:
        raise MediaPipelineError("Provide between 1 and 10 comma-separated caption language codes.")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", item) for item in languages):
        raise MediaPipelineError("Caption language codes contain unsupported characters.")

    cache_path = _caption_cache_path(source_url, languages)
    cached = _read_caption_cache(cache_path)
    if cached is not None:
        diagnostics = cached.setdefault("transport_diagnostics", {})
        diagnostics["cache_source_correlation_id"] = diagnostics.get("request_correlation_id")
        diagnostics["request_correlation_id"] = correlation_id
        diagnostics["network_request_performed"] = False
        return cached

    metadata_options = _base_ydl_options()
    metadata_options.update({"noplaylist": True, "skip_download": True})

    def extract_metadata() -> Any:
        with yt_dlp.YoutubeDL(metadata_options) as ydl:
            return ydl.extract_info(source_url, download=False)

    info, metadata_diagnostics = _run_youtube_operation(
        extract_metadata,
        stage="captions",
        source_url=source_url,
        correlation_id=correlation_id,
        selected_languages=languages,
    )
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise MediaPipelineError("Fetch captions for one canonical video URL at a time, not a playlist URL.")

    manual = set((info.get("subtitles") or {}).keys())
    automatic = set((info.get("automatic_captions") or {}).keys())
    candidates = _caption_candidates(languages, manual, automatic)
    if not candidates:
        raise CaptionUnavailableError(
            "No exact caption track was available in the requested languages; translated tracks were not generated."
        )

    language_attempts: list[dict[str, Any]] = []
    caption_root = cache_root() / "captions" / correlation_id
    last_unavailable: Exception | None = None
    try:
        for language, basis in candidates:
            attempt_record: dict[str, Any] = {
                "language": language,
                "basis": basis,
                "status": "started",
                "http_status": None,
            }
            language_attempts.append(attempt_record)
            caption_dir = caption_root / hashlib.sha256(language.encode("utf-8")).hexdigest()[:12]
            shutil.rmtree(caption_dir, ignore_errors=True)
            caption_dir.mkdir(parents=True, exist_ok=True)
            options = _base_ydl_options()
            options.update(
                {
                    "noplaylist": True,
                    "skip_download": True,
                    "writesubtitles": basis == "manual captions",
                    "writeautomaticsub": basis == "automatic captions",
                    "subtitleslangs": [language],
                    "subtitlesformat": "vtt/best",
                    "outtmpl": str(caption_dir / "captions.%(ext)s"),
                }
            )

            def download_track() -> Any:
                with yt_dlp.YoutubeDL(options) as ydl:
                    return ydl.extract_info(source_url, download=True)

            try:
                downloaded_info, track_diagnostics = _run_youtube_operation(
                    download_track,
                    stage="captions",
                    source_url=source_url,
                    correlation_id=correlation_id,
                    selected_languages=[candidate[0] for candidate in candidates],
                    language_attempts=language_attempts,
                )
                files = [
                    path
                    for path in sorted(caption_dir.glob("captions.*"))
                    if path.suffix.lower() in {".vtt", ".srt", ".ass", ".ttml"}
                ]
                if not files:
                    attempt_record["status"] = "not-written"
                    last_unavailable = CaptionUnavailableError(
                        f"YouTube exposed {language!r}, but yt-dlp wrote no caption file for it."
                    )
                    continue
                selected = files[0]
                if selected.stat().st_size > 5 * 1024 * 1024:
                    raise MediaPipelineError("The selected caption file exceeds the 5 MiB safety limit.")
                attempt_record.update(
                    {
                        "status": "succeeded",
                        "request_attempt": track_diagnostics["attempt"],
                        "http_status": None,
                    }
                )
                result = {
                    "source_url": source_url,
                    "video": _compact_video_info(downloaded_info or info, 1),
                    "language": language,
                    "basis": basis,
                    "format": selected.suffix.lower().lstrip("."),
                    "content": selected.read_text(encoding="utf-8", errors="replace"),
                    "cache_hit": False,
                    "transport_diagnostics": {
                        "request_correlation_id": correlation_id,
                        "yt_dlp_version": _yt_dlp_version(),
                        "runtime_update": runtime_update_status(),
                        "requested_languages": languages,
                        "selected_languages": [candidate[0] for candidate in candidates],
                        "language_attempts": language_attempts,
                        "metadata_request": metadata_diagnostics,
                        "caption_request": track_diagnostics,
                        "translated_tracks_generated": False,
                        "request_policy": "one-exact-track-at-a-time",
                    },
                }
                _write_caption_cache(cache_path, result)
                return result
            except MediaTransportError:
                raise
            except CaptionUnavailableError as exc:
                attempt_record["status"] = "unavailable"
                last_unavailable = exc
        raise last_unavailable or CaptionUnavailableError(
            "No captions were available in the requested languages."
        )
    finally:
        shutil.rmtree(caption_root, ignore_errors=True)


def _caption_timestamp_seconds(value: str) -> float:
    parts = value.strip().replace(",", ".").split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    raise ValueError(f"Unsupported caption timestamp: {value}")


def _format_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _new_caption_suffix(previous: str, current: str) -> str:
    if not previous:
        return current
    if current == previous or previous.endswith(current):
        return ""
    if current.startswith(previous):
        return current[len(previous) :].strip()
    previous_words = previous.split()
    current_words = current.split()
    max_overlap = min(len(previous_words), len(current_words))
    for overlap in range(max_overlap, 0, -1):
        if previous_words[-overlap:] == current_words[:overlap]:
            return " ".join(current_words[overlap:]).strip()
    return current


def normalize_caption_segments(content: str, source_format: str) -> list[dict[str, Any]]:
    if source_format.lower() not in {"vtt", "srt"}:
        raise MediaPipelineError(f"Caption normalization does not support {source_format!r}.")
    timing_pattern = re.compile(
        r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
        r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n"
        r"(?P<body>.*?)(?=\n\s*\n|\Z)",
        re.DOTALL,
    )
    segments: list[dict[str, Any]] = []
    previous_text = ""
    for match in timing_pattern.finditer(content.replace("\r\n", "\n")):
        body = re.sub(r"<[^>]+>", "", match.group("body"))
        body = re.sub(r"&nbsp;", " ", body)
        text = re.sub(r"\s+", " ", body).strip()
        if not text:
            continue
        suffix = _new_caption_suffix(previous_text, text)
        previous_text = text
        if not suffix:
            continue
        start = _caption_timestamp_seconds(match.group("start"))
        end = _caption_timestamp_seconds(match.group("end"))
        if segments and start <= segments[-1]["end_seconds"] + 0.35 and len(segments[-1]["text"]) < 420:
            segments[-1]["end_seconds"] = max(segments[-1]["end_seconds"], end)
            segments[-1]["end"] = _format_timestamp(segments[-1]["end_seconds"])
            segments[-1]["text"] = f"{segments[-1]['text']} {suffix}".strip()
        else:
            segments.append(
                {
                    "index": len(segments),
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "start": _format_timestamp(start),
                    "end": _format_timestamp(end),
                    "text": suffix,
                }
            )
    if not segments:
        raise MediaPipelineError("The caption file contained no normalizable timestamped cues.")
    return segments


def fetch_caption_segments(
    url: str,
    preferred_languages: str = "ko,en",
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    caption = fetch_captions(url, preferred_languages, _correlation_id_value)
    segments = normalize_caption_segments(caption["content"], caption["format"])
    return {
        "source_url": caption["source_url"],
        "video": caption["video"],
        "language": caption["language"],
        "basis": caption["basis"],
        "source_format": caption["format"],
        "format": "normalized-caption-segments",
        "segment_count": len(segments),
        "segments": segments,
        "cache_hit": caption.get("cache_hit", False),
        "transport_diagnostics": caption.get("transport_diagnostics"),
    }


def _job_id(
    url: str,
    mode: str,
    frame_interval: int,
    audio_chunk_seconds: int,
    max_height: int,
    visual_scan_interval_seconds: float,
    visual_change_threshold: float,
    max_visual_candidates: int,
) -> str:
    digest = hashlib.sha256(
        (
            f"{url}\n{mode}\n{frame_interval}\n{audio_chunk_seconds}\n{max_height}\n"
            f"{visual_scan_interval_seconds:.3f}\n{visual_change_threshold:.4f}\n{max_visual_candidates}"
        ).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def _run_ffmpeg(arguments: list[str], timeout_seconds: int = 7200) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *arguments]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()[-4000:]
        raise MediaPipelineError(f"FFmpeg failed: {detail or 'unknown error'}")


def _grid_average(frame: bytes, width: int, height: int) -> list[int]:
    values: list[int] = []
    source_width = 32
    source_height = 32
    for gy in range(height):
        y0 = gy * source_height // height
        y1 = max(y0 + 1, (gy + 1) * source_height // height)
        for gx in range(width):
            x0 = gx * source_width // width
            x1 = max(x0 + 1, (gx + 1) * source_width // width)
            total = 0
            count = 0
            for y in range(y0, y1):
                row = y * source_width
                for x in range(x0, x1):
                    total += frame[row + x]
                    count += 1
            values.append(total // count)
    return values


def _bits_to_int(bits: list[bool]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


_DCT_COS = [[math.cos(math.pi * (2 * x + 1) * u / 32.0) for x in range(16)] for u in range(8)]


def _visual_signature(frame: bytes) -> dict[str, Any]:
    grayscale = bytes(
        (77 * frame[index] + 150 * frame[index + 1] + 29 * frame[index + 2]) >> 8
        for index in range(0, len(frame), 3)
    )
    eight = _grid_average(grayscale, 8, 8)
    average = sum(eight) / len(eight)
    ahash = _bits_to_int([value >= average for value in eight])

    nine_by_eight = _grid_average(grayscale, 9, 8)
    dhash_bits: list[bool] = []
    for row in range(8):
        offset = row * 9
        for column in range(8):
            dhash_bits.append(nine_by_eight[offset + column] < nine_by_eight[offset + column + 1])
    dhash = _bits_to_int(dhash_bits)

    sixteen = _grid_average(grayscale, 16, 16)
    coefficients: list[float] = []
    for v in range(8):
        cos_y = _DCT_COS[v]
        for u in range(8):
            cos_x = _DCT_COS[u]
            coefficient = 0.0
            for y in range(16):
                row = y * 16
                weighted_row = 0.0
                for x in range(16):
                    weighted_row += sixteen[row + x] * cos_x[x]
                coefficient += weighted_row * cos_y[y]
            coefficients.append(coefficient)
    median_source = sorted(coefficients[1:])
    median = median_source[len(median_source) // 2]
    phash = _bits_to_int([coefficient >= median for coefficient in coefficients])

    histogram = [0] * 16
    for value in grayscale:
        histogram[min(15, value // 16)] += 1
    color_histogram = [0] * 24
    for index, value in enumerate(frame):
        channel = index % 3
        color_histogram[channel * 8 + min(7, value // 32)] += 1
    return {
        "ahash": ahash,
        "dhash": dhash,
        "phash": phash,
        "pixels": grayscale,
        "rgb_pixels": frame,
        "histogram": histogram,
        "color_histogram": color_histogram,
    }


def _aligned_gray_mad(current: bytes, previous: bytes, radius: int = 1) -> float:
    best = 1.0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            total = 0
            count = 0
            for y in range(max(0, dy), min(32, 32 + dy)):
                for x in range(max(0, dx), min(32, 32 + dx)):
                    other_x = x - dx
                    other_y = y - dy
                    total += abs(current[y * 32 + x] - previous[other_y * 32 + other_x])
                    count += 1
            if count:
                best = min(best, total / (count * 255.0))
    return best


def _edge_map(pixels: bytes) -> set[int]:
    edges: set[int] = set()
    for y in range(1, 31):
        for x in range(1, 31):
            index = y * 32 + x
            gx = abs(pixels[index + 1] - pixels[index - 1])
            gy = abs(pixels[index + 32] - pixels[index - 32])
            if gx + gy >= 70:
                edges.add(index)
    return edges


def _edge_change(current: bytes, previous: bytes) -> float:
    current_edges = _edge_map(current)
    previous_edges = _edge_map(previous)
    union = current_edges | previous_edges
    return len(current_edges ^ previous_edges) / len(union) if union else 0.0


def _changed_area(current: bytes, previous: bytes, cutoff: int = 24) -> float:
    return sum(abs(a - b) >= cutoff for a, b in zip(current, previous)) / len(current)


def _signature_distance(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, float]:
    phash = (current["phash"] ^ previous["phash"]).bit_count() / 64.0
    dhash = (current["dhash"] ^ previous["dhash"]).bit_count() / 64.0
    ahash = (current["ahash"] ^ previous["ahash"]).bit_count() / 64.0
    pixels_a = current["pixels"]
    pixels_b = previous["pixels"]
    pixel_mad = sum(abs(a - b) for a, b in zip(pixels_a, pixels_b)) / (len(pixels_a) * 255.0)
    histogram_l1 = sum(
        abs(a - b) for a, b in zip(current["histogram"], previous["histogram"])
    ) / (2.0 * len(pixels_a))
    rgb_a = current["rgb_pixels"]
    rgb_b = previous["rgb_pixels"]
    color_mad = sum(abs(a - b) for a, b in zip(rgb_a, rgb_b)) / (len(rgb_a) * 255.0)
    color_histogram_l1 = sum(
        abs(a - b) for a, b in zip(current["color_histogram"], previous["color_histogram"])
    ) / (2.0 * len(pixels_a))
    aligned_mad = _aligned_gray_mad(pixels_a, pixels_b)
    edge_change = _edge_change(pixels_a, pixels_b)
    changed_area = _changed_area(pixels_a, pixels_b)
    score = (
        0.35 * phash
        + 0.20 * dhash
        + 0.05 * ahash
        + 0.10 * pixel_mad
        + 0.05 * histogram_l1
        + 0.20 * color_mad
        + 0.05 * color_histogram_l1
    )
    return {
        "phash_distance": round(phash, 4),
        "dhash_distance": round(dhash, 4),
        "ahash_distance": round(ahash, 4),
        "pixel_mad": round(pixel_mad, 4),
        "histogram_l1": round(histogram_l1, 4),
        "color_mad": round(color_mad, 4),
        "color_histogram_l1": round(color_histogram_l1, 4),
        "aligned_mad": round(aligned_mad, 4),
        "edge_change": round(edge_change, 4),
        "changed_area": round(changed_area, 4),
        "change_score": round(score, 4),
    }


def _scan_visual_changes(
    source_file: Path,
    scan_interval_seconds: float,
    change_threshold: float,
    max_candidates: int,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start_seconds > 0:
        command.extend(["-ss", f"{start_seconds:.3f}"])
    command.extend([
        "-i",
        str(source_file),
    ])
    if end_seconds is not None:
        command.extend(["-t", f"{max(0.0, end_seconds - start_seconds):.3f}"])
    command.extend([
        "-vf",
        (
            f"fps=1/{scan_interval_seconds},"
            "scale=32:32:force_original_aspect_ratio=decrease,"
            "pad=32:32:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24"
        ),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise MediaPipelineError("FFmpeg visual scan could not open its output pipes.")

    frame_size = 32 * 32 * 3
    scanned: list[dict[str, Any]] = []
    previous_signature: dict[str, Any] | None = None
    last_selected_signature: dict[str, Any] | None = None
    sample_index = 0
    try:
        while True:
            frame = process.stdout.read(frame_size)
            if not frame:
                break
            if len(frame) != frame_size:
                raise MediaPipelineError("FFmpeg visual scan returned a truncated frame.")
            signature = _visual_signature(frame)
            if previous_signature is None:
                metrics = {
                    "phash_distance": 1.0,
                    "dhash_distance": 1.0,
                    "ahash_distance": 1.0,
                    "pixel_mad": 1.0,
                    "histogram_l1": 1.0,
                    "color_mad": 1.0,
                    "color_histogram_l1": 1.0,
                    "aligned_mad": 1.0,
                    "edge_change": 1.0,
                    "changed_area": 1.0,
                    "change_score": 1.0,
                }
                cumulative_score = 1.0
            else:
                metrics = _signature_distance(signature, previous_signature)
                cumulative_score = (
                    _signature_distance(signature, last_selected_signature)["change_score"]
                    if last_selected_signature is not None
                    else metrics["change_score"]
                )
            should_select = (
                sample_index == 0
                or metrics["change_score"] >= change_threshold
                or cumulative_score >= min(1.0, change_threshold * 1.35)
                or metrics["phash_distance"] >= 0.03125
                or metrics["aligned_mad"] >= 0.006
                or metrics["edge_change"] >= 0.04
                or metrics["changed_area"] >= 0.01
            )
            if should_select:
                scanned.append(
                    {
                        "sample_index": sample_index,
                        "timestamp_seconds": round(start_seconds + sample_index * scan_interval_seconds, 3),
                        **metrics,
                        "cumulative_change_score": round(cumulative_score, 4),
                        "ahash": f"{signature['ahash']:016x}",
                        "dhash": f"{signature['dhash']:016x}",
                        "phash": f"{signature['phash']:016x}",
                        "selection_reason": "first" if sample_index == 0 else "visual-change",
                    }
                )
                last_selected_signature = signature
            previous_signature = signature
            sample_index += 1
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait(timeout=7200)
    if return_code != 0:
        raise MediaPipelineError(f"FFmpeg visual scan failed: {stderr.strip()[-4000:] or 'unknown error'}")

    if sample_index and (not scanned or scanned[-1]["sample_index"] != sample_index - 1):
        scanned.append(
            {
                "sample_index": sample_index - 1,
                "timestamp_seconds": round(start_seconds + (sample_index - 1) * scan_interval_seconds, 3),
                "phash_distance": 0.0,
                "dhash_distance": 0.0,
                "ahash_distance": 0.0,
                "pixel_mad": 0.0,
                "histogram_l1": 0.0,
                "color_mad": 0.0,
                "color_histogram_l1": 0.0,
                "aligned_mad": 0.0,
                "edge_change": 0.0,
                "changed_area": 0.0,
                "change_score": 0.0,
                "cumulative_change_score": 0.0,
                "ahash": None,
                "dhash": None,
                "phash": None,
                "selection_reason": "last",
            }
        )

    if len(scanned) > max_candidates:
        first = scanned[0]
        last = scanned[-1]
        middle = scanned[1:-1]
        slots = max_candidates - 2
        chosen: list[dict[str, Any]] = []
        if slots > 0 and middle:
            bucket_size = len(middle) / slots
            for bucket in range(slots):
                start = int(bucket * bucket_size)
                end = max(start + 1, int((bucket + 1) * bucket_size))
                item = max(
                    middle[start:min(end, len(middle))],
                    key=lambda candidate: (
                        candidate["change_score"],
                        candidate["cumulative_change_score"],
                    ),
                )
                item = dict(item)
                item["selection_reason"] = "coverage-peak"
                chosen.append(item)
        scanned = [first, *chosen, last]

    return scanned, sample_index


def _extract_selected_frames(
    source_file: Path,
    frames_dir: Path,
    candidates: list[dict[str, Any]],
    scan_interval_seconds: float,
    max_height: int,
    start_seconds: float = 0.0,
    filename_prefix: str = "frame",
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    sample_indices = [int(item["sample_index"]) for item in candidates]
    select_expression = "+".join(f"eq(n\\,{index})" for index in sample_indices)
    frame_pattern = frames_dir / f"{filename_prefix}_%06d.jpg"
    arguments = ["-y"]
    if start_seconds > 0:
        arguments.extend(["-ss", f"{start_seconds:.3f}"])
    arguments.extend(
        [
            "-i", str(source_file),
            "-vf",
            (
                f"fps=1/{scan_interval_seconds},select={select_expression},"
                f"scale=-2:{max_height}:force_original_aspect_ratio=decrease"
            ),
            "-fps_mode",
            "vfr",
            "-q:v",
            "3",
            str(frame_pattern),
        ]
    )
    _run_ffmpeg(arguments)
    paths = sorted(frames_dir.glob(f"{filename_prefix}_*.jpg"))
    if len(paths) != len(candidates):
        raise MediaPipelineError(
            f"Adaptive frame extraction produced {len(paths)} frames for {len(candidates)} candidates."
        )
    frames: list[dict[str, Any]] = []
    for index, (path, candidate) in enumerate(zip(paths, candidates)):
        frames.append(
            {
                "index": index,
                "timestamp_seconds": candidate["timestamp_seconds"],
                "file": str(path.relative_to(frames_dir.parent)),
                "bytes": path.stat().st_size,
                "selection_reason": candidate["selection_reason"],
                "change_score": candidate["change_score"],
                "cumulative_change_score": candidate["cumulative_change_score"],
                "phash_distance": candidate["phash_distance"],
                "dhash_distance": candidate["dhash_distance"],
                "ahash_distance": candidate["ahash_distance"],
                "pixel_mad": candidate["pixel_mad"],
                "histogram_l1": candidate["histogram_l1"],
                "color_mad": candidate["color_mad"],
                "color_histogram_l1": candidate["color_histogram_l1"],
                "aligned_mad": candidate.get("aligned_mad"),
                "edge_change": candidate.get("edge_change"),
                "changed_area": candidate.get("changed_area"),
                "phash": candidate["phash"],
                "dhash": candidate["dhash"],
                "ahash": candidate["ahash"],
            }
        )
    return frames


def _find_source_file(job_dir: Path, mode: str) -> Path:
    excluded = {"manifest.json"}
    candidates = [
        p
        for p in job_dir.iterdir()
        if p.is_file()
        and p.name not in excluded
        and not p.name.endswith((".part", ".ytdl", ".json"))
        and p.suffix.lower() in (VIDEO_EXTENSIONS | AUDIO_EXTENSIONS)
    ]
    if not candidates:
        raise MediaPipelineError("The download completed but no media file was found.")
    if mode == "audiovisual":
        video_candidates = [p for p in candidates if p.suffix.lower() in VIDEO_EXTENSIONS]
        if video_candidates:
            candidates = video_candidates
    return max(candidates, key=lambda p: p.stat().st_size)


def _safe_manifest(job_id: str) -> tuple[Path, dict[str, Any]]:
    if not job_id or any(c not in "0123456789abcdef" for c in job_id) or len(job_id) != 20:
        raise MediaPipelineError("Invalid job_id.")
    job_dir = (cache_root() / "jobs" / job_id).resolve()
    jobs_root = (cache_root() / "jobs").resolve()
    if jobs_root not in job_dir.parents:
        raise MediaPipelineError("Invalid job path.")
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.is_file():
        raise MediaPipelineError(f"Unknown or incomplete job_id: {job_id}")
    return job_dir, json.loads(manifest_path.read_text(encoding="utf-8"))


def _selected_format_diagnostics(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {"formats": [], "player_clients": []}
    requested = info.get("requested_downloads") or [info]
    formats: list[dict[str, Any]] = []
    clients: list[str] = []
    for item in requested:
        if not isinstance(item, dict):
            continue
        client = item.get("__yt_dlp_client") or item.get("player_client")
        if client and str(client) not in clients:
            clients.append(str(client))
        formats.append(
            {
                "format_id": item.get("format_id"),
                "ext": item.get("ext"),
                "protocol": item.get("protocol"),
                "audio_codec": item.get("acodec"),
                "video_codec": item.get("vcodec"),
                "player_client": client,
            }
        )
    return {"formats": formats, "player_clients": clients}


def prepare_media(
    url: str,
    analysis_mode: str,
    frame_interval_seconds: int = 30,
    audio_chunk_seconds: int = 300,
    max_height: int = 720,
    visual_scan_interval_seconds: float = 2.0,
    visual_change_threshold: float = 0.18,
    max_visual_candidates: int = 96,
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    total_started = time.perf_counter()
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id_value or _correlation_id()
    if analysis_mode not in {"adaptive", "audiovisual", "audio-only", "audio-source-only"}:
        raise MediaPipelineError(
            "analysis_mode must be 'adaptive', 'audiovisual', 'audio-only', or 'audio-source-only'."
        )
    if not 5 <= frame_interval_seconds <= 300:
        raise MediaPipelineError("frame_interval_seconds must be between 5 and 300.")
    if not 30 <= audio_chunk_seconds <= 600:
        raise MediaPipelineError("audio_chunk_seconds must be between 30 and 600.")
    if max_height not in {360, 480, 720, 1080}:
        raise MediaPipelineError("max_height must be one of 360, 480, 720, or 1080.")
    if not 0.5 <= visual_scan_interval_seconds <= 15:
        raise MediaPipelineError("visual_scan_interval_seconds must be between 0.5 and 15.")
    if not 0.05 <= visual_change_threshold <= 0.9:
        raise MediaPipelineError("visual_change_threshold must be between 0.05 and 0.9.")
    if not 4 <= max_visual_candidates <= 500:
        raise MediaPipelineError("max_visual_candidates must be between 4 and 500.")

    job_id = _job_id(
        source_url,
        analysis_mode,
        frame_interval_seconds,
        audio_chunk_seconds,
        max_height,
        visual_scan_interval_seconds,
        visual_change_threshold,
        max_visual_candidates,
    )
    job_dir = cache_root() / "jobs" / job_id
    manifest_path = job_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cache_hit"] = True
        return manifest

    if job_dir.exists():
        shutil.rmtree(job_dir)
    (job_dir / "frames").mkdir(parents=True, exist_ok=True)
    (job_dir / "audio").mkdir(parents=True, exist_ok=True)

    options = _base_ydl_options()
    options.update(
        {
            "noplaylist": True,
            "outtmpl": str(job_dir / "source.%(ext)s"),
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
            "continuedl": False,
            "overwrites": True,
            "max_filesize": 4 * 1024 * 1024 * 1024,
            "merge_output_format": "mp4",
            "format": (
                f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
                if analysis_mode in {"adaptive", "audiovisual"}
                else "bestaudio/best"
            ),
        }
    )
    diagnostic_context: dict[str, Any] = {}

    def capture_download_selection(event: dict[str, Any]) -> None:
        details = _selected_format_diagnostics(event.get("info_dict"))
        diagnostic_context["selected_format"] = details
        clients = details.get("player_clients") or []
        diagnostic_context["player_client"] = ",".join(clients) if clients else None

    options["progress_hooks"] = [capture_download_selection]
    format_request = str(options["format"])
    download_started = time.perf_counter()

    def download_media() -> Any:
        for path in job_dir.glob("source.*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(source_url, download=True)

    try:
        info, request_diagnostics = _run_youtube_operation(
            download_media,
            stage="media_download",
            source_url=source_url,
            correlation_id=correlation_id,
            format_request=format_request,
            retry_403_once=True,
            diagnostic_context=diagnostic_context,
        )
    except MediaTransportError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    download_elapsed = time.perf_counter() - download_started

    if not info or info.get("_type") in {"playlist", "multi_video"}:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise MediaPipelineError("Prepare one canonical video URL at a time, not a playlist URL.")

    source_file = _find_source_file(job_dir, analysis_mode)
    duration = float(info.get("duration") or 0)

    audio_pattern = job_dir / "audio" / "chunk_%04d.mp3"
    audio_error = None
    audio_started = time.perf_counter()
    if analysis_mode != "audio-source-only":
        try:
            _run_ffmpeg(
                [
                    "-y",
                    "-i",
                    str(source_file),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    "64k",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(audio_chunk_seconds),
                    "-reset_timestamps",
                    "1",
                    str(audio_pattern),
                ]
            )
        except MediaPipelineError as exc:
            audio_error = str(exc)
            if analysis_mode == "audio-only":
                shutil.rmtree(job_dir, ignore_errors=True)
                raise MediaTransportError(
                    "No usable audio track was found in the downloaded media.",
                    stage="audio_decode",
                    error_code="audio_decode_failed",
                    video_id=_video_key(source_url),
                    correlation_id=correlation_id,
                    cause_type=type(exc).__name__,
                ) from exc
    audio_elapsed = time.perf_counter() - audio_started

    visual_samples_scanned = 0
    visual_started = time.perf_counter()
    if analysis_mode == "adaptive":
        candidates, visual_samples_scanned = _scan_visual_changes(
            source_file,
            visual_scan_interval_seconds,
            visual_change_threshold,
            max_visual_candidates,
        )
        frames = _extract_selected_frames(
            source_file,
            job_dir / "frames",
            candidates,
            visual_scan_interval_seconds,
            max_height,
        )
    elif analysis_mode == "audiovisual":
        frame_pattern = job_dir / "frames" / "frame_%06d.jpg"
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(source_file),
                "-vf",
                f"fps=1/{frame_interval_seconds},scale=-2:{max_height}:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(frame_pattern),
            ]
        )
        frames = []
        for index, path in enumerate(sorted((job_dir / "frames").glob("frame_*.jpg"))):
            frames.append(
                {
                    "index": index,
                    "timestamp_seconds": index * frame_interval_seconds,
                    "file": str(path.relative_to(job_dir)),
                    "bytes": path.stat().st_size,
                    "selection_reason": "legacy-fixed-interval",
                }
            )
    else:
        frames = []
    visual_elapsed = time.perf_counter() - visual_started

    audio_chunks = []
    for index, path in enumerate(sorted((job_dir / "audio").glob("chunk_*.mp3"))):
        start = index * audio_chunk_seconds
        end = min(duration, start + audio_chunk_seconds) if duration else start + audio_chunk_seconds
        audio_chunks.append(
            {
                "index": index,
                "start_seconds": start,
                "end_seconds": end,
                "file": str(path.relative_to(job_dir)),
                "bytes": path.stat().st_size,
            }
        )

    if analysis_mode in {"adaptive", "audiovisual"} and not frames:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise MediaPipelineError("No video frames could be extracted from the downloaded media.")

    manifest = {
        "job_id": job_id,
        "cache_hit": False,
        "source_url": source_url,
        "video": _compact_video_info(info, 1),
        "analysis_mode": analysis_mode,
        "source_file": source_file.name,
        "source_bytes": source_file.stat().st_size,
        "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(),
        "transport_diagnostics": {
            **request_diagnostics,
            "request_correlation_id": correlation_id,
            "yt_dlp_version": _yt_dlp_version(),
            "runtime_update": runtime_update_status(),
            "format_request": format_request,
            "selected_format": _selected_format_diagnostics(info),
            "request_policy": "per-video-cross-process-single-flight",
        },
        "frame_interval_seconds": frame_interval_seconds if analysis_mode == "audiovisual" else None,
        "visual_selection_strategy": (
            "adaptive-perceptual-change" if analysis_mode == "adaptive" else "legacy-fixed-interval"
            if analysis_mode == "audiovisual"
            else None
        ),
        "visual_scan_interval_seconds": visual_scan_interval_seconds if analysis_mode == "adaptive" else None,
        "visual_change_threshold": visual_change_threshold if analysis_mode == "adaptive" else None,
        "max_visual_candidates": max_visual_candidates if analysis_mode == "adaptive" else None,
        "visual_samples_scanned": visual_samples_scanned if analysis_mode == "adaptive" else None,
        "audio_chunk_seconds": audio_chunk_seconds,
        "frame_count": len(frames),
        "audio_chunk_count": len(audio_chunks),
        "audio_error": audio_error if not audio_chunks else None,
        "resource_accounting": {
            "model_tokens_during_prepare": 0,
            "download_elapsed_seconds": round(download_elapsed, 4),
            "audio_decode_elapsed_seconds": round(audio_elapsed, 4),
            "visual_decode_elapsed_seconds": round(visual_elapsed, 4),
            "total_prepare_elapsed_seconds": round(time.perf_counter() - total_started, 4),
            "local_hotspots": [
                "network-download",
                *(["ffmpeg-audio-decode"] if analysis_mode != "audio-source-only" else []),
                *(["ffmpeg-video-decode", "perceptual-change-scan"] if analysis_mode != "audio-only" else []),
            ],
            "model_cost_begins_when": (
                "audio chunks, captions, or manifest text are supplied to the model; preparation itself is local"
            ),
        },
        "frames": frames,
        "audio_chunks": audio_chunks,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def prepare_adaptive_media(
    url: str,
    audio_chunk_seconds: int = 300,
    visual_scan_interval_seconds: float = 2.0,
    visual_change_threshold: float = 0.18,
    max_visual_candidates: int = 96,
    max_height: int = 720,
) -> dict[str, Any]:
    return prepare_media(
        url=url,
        analysis_mode="adaptive",
        frame_interval_seconds=30,
        audio_chunk_seconds=audio_chunk_seconds,
        max_height=max_height,
        visual_scan_interval_seconds=visual_scan_interval_seconds,
        visual_change_threshold=visual_change_threshold,
        max_visual_candidates=max_visual_candidates,
    )


def prepare_transcript_media(
    url: str,
    audio_chunk_seconds: int = 300,
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    """Download audio only so transcript construction never triggers a video scan."""
    return prepare_media(
        url=url,
        analysis_mode="audio-only",
        frame_interval_seconds=30,
        audio_chunk_seconds=audio_chunk_seconds,
        max_height=360,
        _correlation_id_value=_correlation_id_value,
    )


def prepare_stt_source_media(
    url: str,
    _correlation_id_value: str | None = None,
) -> dict[str, Any]:
    """Download the audio source without decoding the entire track into chunks."""
    return prepare_media(
        url=url,
        analysis_mode="audio-source-only",
        frame_interval_seconds=30,
        audio_chunk_seconds=300,
        max_height=360,
        _correlation_id_value=_correlation_id_value,
    )


def prepare_transcript_bundle(
    url: str,
    preferred_languages: str = "ko,en",
) -> dict[str, Any]:
    """Fetch and normalize captions without downloading audio or running STT."""
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id()
    captions: dict[str, Any]
    try:
        captions = {
            "available": True,
            "result": fetch_caption_segments(source_url, preferred_languages, correlation_id),
        }
    except CaptionUnavailableError as exc:
        captions = {"available": False, "reason": str(exc), "result": None}
    caption_result = captions.get("result") or {}
    caption_segments = list(caption_result.get("segments") or [])
    transcript = {
        "available": bool(caption_segments),
        "basis": "normalized_youtube_captions" if caption_segments else None,
        "language": caption_result.get("language"),
        "segment_count": len(caption_segments),
        "segments": caption_segments,
        "audio_transcription_performed": False,
        "stt_required": False,
        "stt_policy": "Use selective local STT only for a bounded caption phrase that remains materially ambiguous.",
    }
    return {
        "source_url": source_url,
        "request_correlation_id": correlation_id,
        "execution_order": ["captions"],
        "concurrent_same_video_requests": False,
        "captions": captions,
        "media": None,
        "transcript": transcript,
        "caption_usage": "primary transcript source",
        "visual_review_may_proceed_without_stt": True,
        "transport_policy": {
            "request_gate": "cross-process per-video single-flight",
            "minimum_request_spacing_seconds": _request_spacing_seconds(),
            "caption_fallback": "one exact available track at a time; no generated translations",
            "http_429": "Retry-After or jittered 5/15/45 second backoff",
            "http_403": "one fresh media re-extraction after 5 seconds",
            "authentication_bypass": False,
        },
    }


def local_stt_runtime_status() -> dict[str, Any]:
    return _stt_runtime_status(cache_root())


def transcribe_job_audio_locally(job_id: str, language: str = "auto", force: bool = False) -> dict[str, Any]:
    job_dir, manifest = _safe_manifest(job_id)
    source_file = (job_dir / str(manifest.get("source_file") or "")).resolve()
    if job_dir not in source_file.parents or not source_file.is_file():
        raise LocalSTTError("The prepared audio source is missing or unsafe.", error_code="stt_source_missing")
    result = transcribe_audio(
        source_file=source_file,
        job_dir=job_dir,
        ffmpeg_path=str(manifest.get("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()),
        cache_root=cache_root(),
        language=language,
        force=force,
    )
    manifest["local_stt"] = {
        "available": result.get("available"),
        "basis": result.get("basis"),
        "engine": result.get("engine"),
        "model": result.get("model"),
        "language": result.get("language"),
        "segment_count": result.get("segment_count"),
        "analysis_scope": result.get("analysis_scope"),
        "resource_accounting": result.get("resource_accounting"),
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def transcribe_job_audio_ranges_locally(
    job_id: str,
    verification_ranges: list[dict[str, Any]],
    language: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    job_dir, manifest = _safe_manifest(job_id)
    source_file = (job_dir / str(manifest.get("source_file") or "")).resolve()
    if job_dir not in source_file.parents or not source_file.is_file():
        raise LocalSTTError("The prepared audio source is missing or unsafe.", error_code="stt_source_missing")
    duration = float((manifest.get("video") or {}).get("duration_seconds") or 0)
    for item in verification_ranges:
        if isinstance(item, dict) and duration and float(item.get("end_seconds", 0)) > duration:
            raise LocalSTTError(
                f"A caption-verification range exceeds the video duration of {duration:.3f} seconds.",
                error_code="invalid_stt_ranges",
            )
    result = transcribe_audio_ranges(
        source_file=source_file,
        job_dir=job_dir,
        ffmpeg_path=str(manifest.get("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()),
        cache_root=cache_root(),
        ranges=verification_ranges,
        language=language,
        force=force,
    )
    manifest["selective_local_stt"] = {
        "basis": result.get("basis"),
        "analysis_scope": result.get("analysis_scope"),
        "range_count": result.get("range_count"),
        "total_verified_seconds": result.get("total_verified_seconds"),
        "full_audio_transcription_performed": False,
        "model_tokens_during_stt": 0,
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def verify_caption_ranges(
    url: str,
    verification_ranges: list[dict[str, Any]],
    language: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    """Download audio only after caption review and STT only the explicitly requested ranges."""
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id()
    media = prepare_stt_source_media(source_url, correlation_id)
    verification = transcribe_job_audio_ranges_locally(media["job_id"], verification_ranges, language, force)
    return {
        "source_url": source_url,
        "request_correlation_id": correlation_id,
        "execution_order": ["media_download", "selected_range_audio_decode", "selected_range_stt"],
        "caption_review_required_before_call": True,
        "visual_review_dependency": "none",
        "media": media,
        "verification": verification,
    }


def _targeted_job_id(
    url: str,
    ranges: list[dict[str, Any]],
    scan_interval_seconds: float,
    change_threshold: float,
    max_candidates_per_range: int,
    max_height: int,
    ocr_mode: str,
    ocr_engine: str = "auto",
    ocr_languages: str = "ko-KR,en-US",
) -> str:
    payload = json.dumps(ranges, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(
        (
            f"{url}\ntranscript-targeted-visual\n{payload}\n{scan_interval_seconds:.3f}\n"
            f"{change_threshold:.4f}\n{max_candidates_per_range}\n{max_height}\n{ocr_mode}"
            f"\nocr-v2\n{ocr_engine}\n{ocr_languages}\n{sys.platform}\n{find_tesseract()}"
        ).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def _normalize_visual_ranges(
    ranges: list[dict[str, Any]],
    duration_seconds: float,
    allow_whole_video_visual_pass: bool,
) -> list[dict[str, Any]]:
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= 32:
        raise MediaPipelineError("visual_ranges must contain between 1 and 32 ranges.")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise MediaPipelineError(f"visual_ranges[{index}] must be an object.")
        try:
            start = float(item.get("start_seconds"))
            end = float(item.get("end_seconds"))
        except (TypeError, ValueError) as exc:
            raise MediaPipelineError(f"visual_ranges[{index}] needs numeric start_seconds and end_seconds.") from exc
        if start < 0 or end <= start:
            raise MediaPipelineError(f"visual_ranges[{index}] must satisfy 0 <= start_seconds < end_seconds.")
        if duration_seconds:
            if start >= duration_seconds:
                raise MediaPipelineError(f"visual_ranges[{index}] starts after the video ends.")
            end = min(end, duration_seconds)
        question = re.sub(r"\s+", " ", str(item.get("question") or "")).strip()
        if not question or len(question) > 1000:
            raise MediaPipelineError(f"visual_ranges[{index}] needs a concise unresolved question.")
        question_kind = str(item.get("question_kind") or "unknown").strip().lower()
        if question_kind not in VISUAL_QUESTION_KINDS:
            raise MediaPipelineError(
                f"visual_ranges[{index}].question_kind must be one of {sorted(VISUAL_QUESTION_KINDS)}."
            )
        transcript_context = re.sub(r"\s+", " ", str(item.get("transcript_context") or "")).strip()
        known_text = re.sub(r"\s+", " ", str(item.get("known_text") or "")).strip()
        if len(transcript_context) > 20_000 or len(known_text) > 20_000:
            raise MediaPipelineError("Transcript context and known text are limited to 20,000 characters per range.")
        normalized.append(
            {
                "range_index": index,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "question": question,
                "question_kind": question_kind,
                "transcript_context": transcript_context,
                "known_text": known_text,
                "priority": max(1, min(5, int(item.get("priority") or 3))),
            }
        )
    normalized.sort(key=lambda value: (value["start_seconds"], value["end_seconds"]))
    for previous, current in zip(normalized, normalized[1:]):
        if current["start_seconds"] < previous["end_seconds"]:
            raise MediaPipelineError(
                "Visual ranges must not overlap. Merge overlapping questions into one range to avoid duplicate decoding."
            )
    total_seconds = sum(item["end_seconds"] - item["start_seconds"] for item in normalized)
    normal_limit = max(180.0, duration_seconds * 0.35) if duration_seconds else 900.0
    if not allow_whole_video_visual_pass and total_seconds > normal_limit:
        raise MediaPipelineError(
            f"Requested visual ranges total {total_seconds:.1f}s, above the targeted-pass limit "
            f"of {normal_limit:.1f}s. Narrow the transcript-derived ranges or explicitly allow a whole-video visual pass."
        )
    return normalized


def _ocr_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[^\W_]+(?:[.%:+_-][^\W_]+)*", text, flags=re.UNICODE)
        if token
    }


def _number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _unit_tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in UNIT_PATTERN.finditer(text)}


def _run_local_ocr(
    paths: list[Path], ocr_mode: str, languages: str, engine: str | None = None
) -> tuple[str, dict[str, dict[str, Any]], str | None]:
    return run_local_ocr(paths, ocr_mode, languages, cache_root(), engine)


def _semantic_ocr_decision(
    frame: dict[str, Any],
    visual_range: dict[str, Any],
    ocr: dict[str, Any] | None,
    ocr_error: str | None,
    minimum_ocr_confidence: float = 0.75,
) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str((ocr or {}).get("text") or "")).strip()
    confidence = float((ocr or {}).get("average_confidence") or 0.0)
    known = f"{visual_range.get('transcript_context', '')} {visual_range.get('known_text', '')}".strip()
    ocr_words = _ocr_tokens(text)
    known_words = _ocr_tokens(known)
    novel_words = sorted(ocr_words - known_words)
    novelty_ratio = len(novel_words) / len(ocr_words) if ocr_words else 0.0
    new_numbers = sorted(_number_tokens(text) - _number_tokens(known))
    new_units = sorted(_unit_tokens(text) - _unit_tokens(known))
    warning_terms = sorted(ocr_words & WARNING_TERMS)
    near_duplicate = (
        frame.get("selection_reason") != "first"
        and float(frame.get("phash_distance") or 0) <= 0.03125
        and float(frame.get("aligned_mad") or 0) <= 0.006
        and float(frame.get("edge_change") or 0) <= 0.04
        and novelty_ratio <= 0.03
    )
    kind = visual_range["question_kind"]
    reasons: list[str] = []
    resolved_by_local_ocr = False
    if near_duplicate:
        reasons.append("near-duplicate-after-local-ocr")
        image_model_recommended = False
    elif ocr_error:
        reasons.append("local-ocr-unavailable")
        image_model_recommended = True
    elif kind == "onscreen_text":
        if text and confidence >= minimum_ocr_confidence:
            resolved_by_local_ocr = True
            image_model_recommended = False
            reasons.append("plain-text-question-resolved-by-local-ocr")
        else:
            image_model_recommended = True
            reasons.append("plain-text-question-not-reliably-resolved")
    else:
        image_model_recommended = True
        reasons.append(f"requires-{kind}-semantics-beyond-ocr")
    if new_numbers:
        reasons.append("new-number-in-local-ocr")
    if new_units:
        reasons.append("new-unit-in-local-ocr")
    if warning_terms:
        reasons.append("warning-or-correction-in-local-ocr")
    if novelty_ratio > 0.03:
        reasons.append("new-onscreen-text")
    return {
        "text": text,
        "average_confidence": round(confidence, 4),
        "observation_count": int((ocr or {}).get("observation_count") or (ocr or {}).get("observations") or 0),
        "elapsed_ms": (ocr or {}).get("elapsed_ms"),
        "ocr_error": ocr_error or (ocr or {}).get("error"),
        "novel_token_ratio": round(novelty_ratio, 4),
        "novel_tokens": novel_words[:100],
        "new_numbers": new_numbers,
        "new_units": new_units,
        "warning_terms": warning_terms,
        "resolved_by_local_ocr": resolved_by_local_ocr,
        "near_duplicate": near_duplicate,
        "image_model_recommended": image_model_recommended,
        "decision_reasons": reasons,
    }


def prepare_visual_ranges(
    url: str,
    visual_ranges: list[dict[str, Any]],
    visual_scan_interval_seconds: float = 1.0,
    visual_change_threshold: float = 0.08,
    max_candidates_per_range: int = 24,
    max_height: int = 720,
    ocr_mode: str = "accurate",
    ocr_languages: str = "ko-KR,en-US",
    allow_whole_video_visual_pass: bool = False,
    ocr_engine: str | None = None,
) -> dict[str, Any]:
    """Download video only after transcript review, then decode only requested ranges."""
    total_started = time.perf_counter()
    source_url = validate_youtube_url(url)
    correlation_id = _correlation_id()
    if not 0.25 <= visual_scan_interval_seconds <= 15:
        raise MediaPipelineError("visual_scan_interval_seconds must be between 0.25 and 15.")
    if not 0.03 <= visual_change_threshold <= 0.9:
        raise MediaPipelineError("visual_change_threshold must be between 0.03 and 0.9.")
    if not 2 <= max_candidates_per_range <= 96:
        raise MediaPipelineError("max_candidates_per_range must be between 2 and 96.")
    if max_height not in {360, 480, 720, 1080}:
        raise MediaPipelineError("max_height must be one of 360, 480, 720, or 1080.")
    if ocr_mode not in {"fast", "accurate"}:
        raise MediaPipelineError("ocr_mode must be 'fast' or 'accurate'.")
    try:
        selected_ocr_engine = select_engine(ocr_engine)
    except OCRError as exc:
        raise MediaPipelineError(str(exc)) from exc

    probe_started = time.perf_counter()
    probe = probe_youtube(source_url, detailed_playlist=True, max_items=1, _correlation_id_value=correlation_id)
    probe_elapsed = time.perf_counter() - probe_started
    if probe.get("kind") != "video":
        raise MediaPipelineError("Prepare one canonical video URL at a time, not a playlist URL.")
    duration = float((probe.get("video") or {}).get("duration_seconds") or 0)
    ranges = _normalize_visual_ranges(visual_ranges, duration, allow_whole_video_visual_pass)
    job_id = _targeted_job_id(
        source_url, ranges, visual_scan_interval_seconds, visual_change_threshold,
        max_candidates_per_range, max_height, ocr_mode, selected_ocr_engine, ocr_languages,
    )
    job_dir = cache_root() / "jobs" / job_id
    manifest_path = job_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cache_hit"] = True
        return manifest

    if job_dir.exists():
        shutil.rmtree(job_dir)
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    options = _base_ydl_options()
    options.update(
        {
            "noplaylist": True,
            "outtmpl": str(job_dir / "source.%(ext)s"),
            "continuedl": False,
            "overwrites": True,
            "max_filesize": 4 * 1024 * 1024 * 1024,
            "format": f"bestvideo[height<={max_height}]/best[height<={max_height}]",
        }
    )
    diagnostic_context: dict[str, Any] = {}

    def capture_visual_download_selection(event: dict[str, Any]) -> None:
        details = _selected_format_diagnostics(event.get("info_dict"))
        diagnostic_context["selected_format"] = details
        clients = details.get("player_clients") or []
        diagnostic_context["player_client"] = ",".join(clients) if clients else None

    options["progress_hooks"] = [capture_visual_download_selection]
    format_request = str(options["format"])
    download_started = time.perf_counter()

    def download_visual_media() -> Any:
        for path in job_dir.glob("source.*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(source_url, download=True)

    try:
        info, request_diagnostics = _run_youtube_operation(
            download_visual_media,
            stage="media_download",
            source_url=source_url,
            correlation_id=correlation_id,
            format_request=format_request,
            retry_403_once=True,
            diagnostic_context=diagnostic_context,
        )
    except MediaTransportError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    download_elapsed = time.perf_counter() - download_started
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise MediaPipelineError("Prepare one canonical video URL at a time, not a playlist URL.")
    source_file = _find_source_file(job_dir, "audiovisual")

    frames: list[dict[str, Any]] = []
    scanned_samples = 0
    range_metrics: list[dict[str, Any]] = []
    visual_started = time.perf_counter()
    for range_position, visual_range in enumerate(ranges):
        range_started = time.perf_counter()
        candidates, sample_count = _scan_visual_changes(
            source_file,
            visual_scan_interval_seconds,
            visual_change_threshold,
            max_candidates_per_range,
            start_seconds=visual_range["start_seconds"],
            end_seconds=visual_range["end_seconds"],
        )
        scanned_samples += sample_count
        extracted = _extract_selected_frames(
            source_file,
            frames_dir,
            candidates,
            visual_scan_interval_seconds,
            max_height,
            start_seconds=visual_range["start_seconds"],
            filename_prefix=f"range_{range_position:03d}",
        )
        for frame in extracted:
            frame["index"] = len(frames)
            frame["range_index"] = visual_range["range_index"]
            frame["visual_question"] = visual_range["question"]
            frame["question_kind"] = visual_range["question_kind"]
            frames.append(frame)
        range_metrics.append(
            {
                "range_index": visual_range["range_index"],
                "duration_seconds": round(visual_range["end_seconds"] - visual_range["start_seconds"], 3),
                "samples_scanned": sample_count,
                "candidates_extracted": len(extracted),
                "local_visual_elapsed_seconds": round(time.perf_counter() - range_started, 4),
            }
        )
    visual_elapsed = time.perf_counter() - visual_started

    frame_paths = [(job_dir / frame["file"]).resolve() for frame in frames]
    ocr_started = time.perf_counter()
    ocr_engine, ocr_results, ocr_error = _run_local_ocr(frame_paths, ocr_mode, ocr_languages, selected_ocr_engine)
    ocr_elapsed = time.perf_counter() - ocr_started
    ranges_by_index = {item["range_index"]: item for item in ranges}
    for frame, path in zip(frames, frame_paths):
        decision = _semantic_ocr_decision(
            frame,
            ranges_by_index[frame["range_index"]],
            ocr_results.get(str(path)),
            ocr_error,
        )
        frame["local_ocr"] = decision
        frame["image_model_recommended"] = decision["image_model_recommended"]
        frame["decision_reasons"] = decision["decision_reasons"]

    manifest = {
        "job_id": job_id,
        "cache_hit": False,
        "source_url": source_url,
        "video": _compact_video_info(info, 1),
        "analysis_mode": "transcript-targeted-visual",
        "source_file": source_file.name,
        "source_bytes": source_file.stat().st_size,
        "ffmpeg": imageio_ffmpeg.get_ffmpeg_exe(),
        "transport_diagnostics": {
            **request_diagnostics,
            "request_correlation_id": correlation_id,
            "yt_dlp_version": _yt_dlp_version(),
            "runtime_update": runtime_update_status(),
            "format_request": format_request,
            "selected_format": _selected_format_diagnostics(info),
            "request_policy": "per-video-cross-process-single-flight",
        },
        "visual_selection_strategy": "transcript-ranges-then-local-ocr",
        "visual_scan_interval_seconds": visual_scan_interval_seconds,
        "visual_change_threshold": visual_change_threshold,
        "max_candidates_per_range": max_candidates_per_range,
        "visual_ranges": ranges,
        "targeted_seconds": round(sum(item["end_seconds"] - item["start_seconds"] for item in ranges), 3),
        "visual_samples_scanned": scanned_samples,
        "frame_count": len(frames),
        "image_model_recommended_count": sum(bool(frame["image_model_recommended"]) for frame in frames),
        "local_ocr_resolved_count": sum(bool(frame["local_ocr"]["resolved_by_local_ocr"]) for frame in frames),
        "local_ocr": {
            "engine": ocr_engine,
            "requested_engine": selected_ocr_engine,
            "fallbacks": {str(path): value["fallback_reasons"] for path, value in ocr_results.items() if value.get("fallback_reasons")},
            "mode": ocr_mode,
            "languages": ocr_languages,
            "elapsed_seconds": round(ocr_elapsed, 4),
            "error": ocr_error,
            "model_tokens": 0,
        },
        "resource_accounting": {
            "model_tokens_during_prepare": 0,
            "metadata_probe_elapsed_seconds": round(probe_elapsed, 4),
            "video_download_elapsed_seconds": round(download_elapsed, 4),
            "range_decode_hash_extract_elapsed_seconds": round(visual_elapsed, 4),
            "local_ocr_elapsed_seconds": round(ocr_elapsed, 4),
            "total_prepare_elapsed_seconds": round(time.perf_counter() - total_started, 4),
            "range_metrics": range_metrics,
            "local_hotspots": [
                "network-video-download",
                "ffmpeg-target-range-decode",
                "jpeg-candidate-extraction",
                f"{ocr_engine}-ocr",
            ],
            "image_token_boundary": (
                "zero image tokens until youtube_get_frame or youtube_get_frame_at returns an image to the model"
            ),
            "text_token_inputs": (
                "the manifest, local OCR, transcript context, and decision metadata consume ordinary model text context when read"
            ),
        },
        "frames": frames,
        "audio_chunks": [],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def get_manifest(job_id: str) -> dict[str, Any]:
    _, manifest = _safe_manifest(job_id)
    return manifest


def get_frame_path(job_id: str, frame_index: int) -> tuple[Path, dict[str, Any]]:
    job_dir, manifest = _safe_manifest(job_id)
    frames = manifest.get("frames") or []
    if frame_index < 0 or frame_index >= len(frames):
        raise MediaPipelineError(f"frame_index must be between 0 and {max(len(frames) - 1, 0)}.")
    item = frames[frame_index]
    path = (job_dir / item["file"]).resolve()
    if job_dir not in path.parents or not path.is_file():
        raise MediaPipelineError("The requested frame file is missing or unsafe.")
    return path, item


def get_frame_at_path(job_id: str, timestamp_seconds: float, max_height: int = 720) -> tuple[Path, dict[str, Any]]:
    job_dir, manifest = _safe_manifest(job_id)
    if manifest.get("analysis_mode") not in {"adaptive", "audiovisual", "transcript-targeted-visual"}:
        raise MediaPipelineError("The prepared job does not contain a video stream.")
    if max_height not in {360, 480, 720, 1080}:
        raise MediaPipelineError("max_height must be one of 360, 480, 720, or 1080.")
    duration = float((manifest.get("video") or {}).get("duration_seconds") or 0)
    if timestamp_seconds < 0 or (duration and timestamp_seconds > duration):
        raise MediaPipelineError(f"timestamp_seconds must be between 0 and {duration or 'the video duration'}.")
    source_file = (job_dir / str(manifest.get("source_file") or "")).resolve()
    if job_dir not in source_file.parents or not source_file.is_file():
        raise MediaPipelineError("The prepared source video is missing or unsafe.")

    target_dir = job_dir / "target_frames"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp_milliseconds = int(round(timestamp_seconds * 1000))
    path = target_dir / f"frame_{timestamp_milliseconds:012d}_{max_height}.jpg"
    if not path.is_file():
        _run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{timestamp_seconds:.3f}",
                "-i",
                str(source_file),
                "-frames:v",
                "1",
                "-vf",
                f"scale=-2:{max_height}:force_original_aspect_ratio=decrease",
                "-q:v",
                "2",
                str(path),
            ]
        )
    if not path.is_file():
        raise MediaPipelineError("No frame was available at the requested timestamp.")
    return path, {
        "timestamp_seconds": round(timestamp_seconds, 3),
        "max_height": max_height,
        "file": str(path.relative_to(job_dir)),
        "bytes": path.stat().st_size,
        "selection_reason": "transcript-targeted",
    }


def get_audio_path(job_id: str, chunk_index: int) -> tuple[Path, dict[str, Any]]:
    job_dir, manifest = _safe_manifest(job_id)
    chunks = manifest.get("audio_chunks") or []
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise MediaPipelineError(f"chunk_index must be between 0 and {max(len(chunks) - 1, 0)}.")
    item = chunks[chunk_index]
    path = (job_dir / item["file"]).resolve()
    if job_dir not in path.parents or not path.is_file():
        raise MediaPipelineError("The requested audio chunk is missing or unsafe.")
    return path, item




def delete_job(job_id: str, confirmation: str) -> dict[str, Any]:
    if confirmation != job_id:
        raise MediaPipelineError("confirmation must exactly match job_id.")
    job_dir, manifest = _safe_manifest(job_id)
    source_bytes = int(manifest.get("source_bytes") or 0)
    total_bytes = sum(p.stat().st_size for p in job_dir.rglob("*") if p.is_file())
    shutil.rmtree(job_dir)
    return {"deleted": True, "job_id": job_id, "freed_bytes": max(total_bytes, source_bytes)}
