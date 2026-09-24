from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


ENGINE_NAME = "whisper.cpp"
MODEL_NAME = "ggml-small-q5_1.bin"
MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_NAME}"
MODEL_SHA256 = "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"
MODEL_MAX_BYTES = 256 * 1024 * 1024
_MODEL_LOCK = threading.Lock()


class LocalSTTError(RuntimeError):
    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "stage": "local_stt",
            "error_code": self.error_code,
            "error": str(self),
            "retryable": self.retryable,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_whisper_cli() -> Path | None:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_WHISPER_CLI", "").strip()
    candidates = [override, shutil.which("whisper-cli"), "/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"]
    for raw in candidates:
        if raw:
            path = Path(raw).expanduser().resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def _model_path(cache_root: Path) -> Path:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_STT_MODEL", "").strip()
    return Path(override).expanduser().resolve() if override else cache_root / "models" / MODEL_NAME


def stt_runtime_status(cache_root: Path) -> dict[str, Any]:
    cli = find_whisper_cli()
    model = _model_path(cache_root)
    present = model.is_file()
    verified = present and _sha256(model) == MODEL_SHA256
    return {
        "engine": ENGINE_NAME,
        "engine_available": cli is not None,
        "engine_path": str(cli) if cli else None,
        "model": MODEL_NAME,
        "model_path": str(model),
        "model_present": present,
        "model_sha256_verified": verified,
        "model_download_required": not verified,
        "model_download_url": MODEL_URL,
        "inference_location": "local",
        "model_tokens_during_stt": 0,
        "features": ["speech_to_text", "timestamped_segments", "automatic_language_detection"],
        "excluded_features": ["speaker_diarization", "speaker_identity", "pitch", "energy", "emotion", "delivery_analysis"],
        "install_hint": None if cli else "Install whisper.cpp so whisper-cli is available, for example: brew install whisper-cpp",
    }


def ensure_model(cache_root: Path) -> Path:
    model = _model_path(cache_root)
    with _MODEL_LOCK:
        if model.is_file() and _sha256(model) == MODEL_SHA256:
            return model
        if model.is_file():
            model.unlink()
        model.parent.mkdir(parents=True, exist_ok=True)
        partial = model.with_suffix(model.suffix + ".part")
        partial.unlink(missing_ok=True)
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "youtube-deep-analyzer/1.7"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                total = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MODEL_MAX_BYTES:
                        raise LocalSTTError(
                            "The local STT model download exceeded the configured size limit.",
                            error_code="stt_model_too_large",
                        )
                    output.write(block)
        except LocalSTTError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise LocalSTTError(
                f"Unable to download the local STT model: {exc}",
                error_code="stt_model_download_failed",
                retryable=True,
            ) from exc
        actual = _sha256(partial)
        if actual != MODEL_SHA256:
            partial.unlink(missing_ok=True)
            raise LocalSTTError(
                f"The local STT model failed SHA-256 verification (expected {MODEL_SHA256}, got {actual}).",
                error_code="stt_model_integrity_failed",
            )
        partial.replace(model)
    return model


def _milliseconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(round(float(value))))
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


def parse_whisper_json(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    raw_segments = payload.get("transcription") or payload.get("segments") or []
    segments: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        offsets = raw.get("offsets") or {}
        timestamps = raw.get("timestamps") or {}
        start_ms = _milliseconds(offsets.get("from")) if offsets else _milliseconds(timestamps.get("from"))
        end_ms = _milliseconds(offsets.get("to")) if offsets else _milliseconds(timestamps.get("to"))
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        if not text:
            continue
        segments.append(
            {
                "start_seconds": round(start_ms / 1000, 3),
                "end_seconds": round(max(start_ms, end_ms) / 1000, 3),
                "text": text,
            }
        )
    language = payload.get("result", {}).get("language") if isinstance(payload.get("result"), dict) else None
    language = language or payload.get("language")
    return segments, str(language) if language else None


def transcribe_audio(
    source_file: Path,
    job_dir: Path,
    ffmpeg_path: str,
    cache_root: Path,
    language: str = "auto",
    force: bool = False,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    language = language.strip().lower() or "auto"
    if not re.fullmatch(r"auto|[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language):
        raise LocalSTTError("language must be 'auto' or a short language code such as ko or en.", error_code="invalid_stt_language")
    if start_seconds < 0 or (end_seconds is not None and end_seconds <= start_seconds):
        raise LocalSTTError(
            "The STT range must have start_seconds >= 0 and end_seconds > start_seconds.",
            error_code="invalid_stt_range",
        )
    cli = find_whisper_cli()
    if cli is None:
        raise LocalSTTError(
            "whisper-cli is not installed or executable. Install whisper.cpp, then restart the plugin task.",
            error_code="stt_runtime_unavailable",
        )
    model = ensure_model(cache_root)
    output_dir = job_dir / "local_stt"
    output_dir.mkdir(parents=True, exist_ok=True)
    range_key = (
        "full"
        if end_seconds is None and start_seconds == 0
        else f"{int(round(start_seconds * 1000)):012d}-{int(round((end_seconds or 0) * 1000)):012d}"
    )
    result_path = output_dir / f"result-{range_key}-{language}.json"
    if result_path.is_file() and not force:
        cached = json.loads(result_path.read_text(encoding="utf-8"))
        cached["cache_hit"] = True
        return cached

    wav_path = output_dir / f"input-{range_key}-16khz-mono.wav"
    convert_started = time.perf_counter()
    ffmpeg_command = [ffmpeg_path, "-y"]
    if start_seconds > 0:
        ffmpeg_command.extend(["-ss", f"{start_seconds:.3f}"])
    ffmpeg_command.extend(["-i", str(source_file)])
    if end_seconds is not None:
        ffmpeg_command.extend(["-t", f"{end_seconds - start_seconds:.3f}"])
    ffmpeg_command.extend(["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)])
    converted = subprocess.run(
        ffmpeg_command,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if converted.returncode != 0 or not wav_path.is_file():
        raise LocalSTTError(
            f"Unable to create the local STT WAV input: {converted.stderr[-1000:]}",
            error_code="stt_audio_decode_failed",
        )
    decode_elapsed = time.perf_counter() - convert_started

    output_prefix = output_dir / f"whisper-{range_key}-{language}"
    whisper_started = time.perf_counter()
    whisper_command = [
        str(cli), "-m", str(model), "-f", str(wav_path), "-l", language, "-oj", "-of", str(output_prefix), "-np"
    ]
    completed = subprocess.run(
        whisper_command,
        capture_output=True,
        text=True,
        timeout=24 * 60 * 60,
    )
    whisper_json = output_prefix.with_suffix(".json")
    backend = "metal_or_default"
    if completed.returncode != 0 and not whisper_json.is_file() and any(
        marker in completed.stderr.lower() for marker in ("ggml_metal", "metal", "gpu")
    ):
        completed = subprocess.run(
            [*whisper_command, "--no-gpu"],
            capture_output=True,
            text=True,
            timeout=24 * 60 * 60,
        )
        backend = "cpu_fallback"
    whisper_elapsed = time.perf_counter() - whisper_started
    if completed.returncode != 0 or not whisper_json.is_file():
        raise LocalSTTError(
            f"Local whisper.cpp transcription failed: {completed.stderr[-1500:]}",
            error_code="stt_inference_failed",
            retryable=False,
        )
    payload = json.loads(whisper_json.read_text(encoding="utf-8"))
    segments, detected_language = parse_whisper_json(payload)
    if start_seconds:
        segments = [
            {
                **item,
                "start_seconds": round(item["start_seconds"] + start_seconds, 3),
                "end_seconds": round(item["end_seconds"] + start_seconds, 3),
            }
            for item in segments
        ]
    bounded = end_seconds is not None or start_seconds > 0
    result = {
        "available": bool(segments),
        "basis": "local_whisper_cpp_stt",
        "engine": ENGINE_NAME,
        "model": MODEL_NAME,
        "inference_backend": backend,
        "requested_language": language,
        "language": detected_language or (None if language == "auto" else language),
        "segment_count": len(segments),
        "segments": segments,
        "text": " ".join(item["text"] for item in segments),
        "audio_transcription_performed": True,
        "analysis_scope": "caption_verification_range_only" if bounded else "speech_to_text_only",
        "verification_range": {
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(end_seconds, 3) if end_seconds is not None else None,
        } if bounded else None,
        "cache_hit": False,
        "resource_accounting": {
            "model_tokens_during_stt": 0,
            "audio_decode_elapsed_seconds": round(decode_elapsed, 4),
            "local_stt_elapsed_seconds": round(whisper_elapsed, 4),
            "local_hotspots": ["ffmpeg-pcm-decode", "whisper.cpp-local-inference"],
            "network_after_model_cached": False,
        },
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def transcribe_audio_ranges(
    source_file: Path,
    job_dir: Path,
    ffmpeg_path: str,
    cache_root: Path,
    ranges: list[dict[str, Any]],
    language: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    if not ranges or len(ranges) > 20:
        raise LocalSTTError("Provide between 1 and 20 caption-verification ranges.", error_code="invalid_stt_ranges")
    normalized: list[dict[str, Any]] = []
    total_seconds = 0.0
    previous_end = -1.0
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise LocalSTTError("Each STT range must be an object.", error_code="invalid_stt_ranges")
        start = float(item.get("start_seconds", -1))
        end = float(item.get("end_seconds", -1))
        duration = end - start
        if start < 0 or duration <= 0 or duration > 90:
            raise LocalSTTError(
                "Each caption-verification range must be longer than 0 and no longer than 90 seconds.",
                error_code="invalid_stt_ranges",
            )
        if start < previous_end:
            raise LocalSTTError("Caption-verification ranges must be chronological and non-overlapping.", error_code="invalid_stt_ranges")
        caption_text = re.sub(r"\s+", " ", str(item.get("caption_text") or "")).strip()
        question = re.sub(r"\s+", " ", str(item.get("question") or "")).strip()
        if not question:
            raise LocalSTTError("Each STT range requires a precise caption-verification question.", error_code="invalid_stt_ranges")
        if len(caption_text) > 4000 or len(question) > 1000:
            raise LocalSTTError("Caption text or verification question is too long.", error_code="invalid_stt_ranges")
        normalized.append(
            {
                "index": index,
                "start_seconds": start,
                "end_seconds": end,
                "caption_text": caption_text,
                "question": question,
            }
        )
        previous_end = end
        total_seconds += duration
    if total_seconds > 600:
        raise LocalSTTError("Caption-verification ranges may cover at most 600 seconds in total.", error_code="invalid_stt_ranges")

    started = time.perf_counter()
    verifications: list[dict[str, Any]] = []
    for item in normalized:
        result = transcribe_audio(
            source_file,
            job_dir,
            ffmpeg_path,
            cache_root,
            language,
            force,
            item["start_seconds"],
            item["end_seconds"],
        )
        verifications.append(
            {
                **item,
                "stt_text": result.get("text", ""),
                "segments": result.get("segments", []),
                "detected_language": result.get("language"),
                "available": result.get("available", False),
                "cache_hit": result.get("cache_hit", False),
                "resource_accounting": result.get("resource_accounting"),
            }
        )
    return {
        "basis": "normalized_youtube_captions_with_selective_local_stt_verification",
        "analysis_scope": "caption_verification_ranges_only",
        "range_count": len(verifications),
        "total_verified_seconds": round(total_seconds, 3),
        "verifications": verifications,
        "full_audio_transcription_performed": False,
        "model_tokens_during_stt": 0,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
