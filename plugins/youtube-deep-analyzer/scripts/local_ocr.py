"""Local OCR backends with explicit selection and per-frame automatic fallback."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ENGINES = {"auto", "apple-vision", "tesseract", "none"}
LANGUAGES = {"ko": "kor", "en": "eng", "ja": "jpn", "fr": "fra", "de": "deu", "es": "spa",
             "it": "ita", "pt": "por", "ru": "rus", "zh-cn": "chi_sim", "zh-hans": "chi_sim",
             "zh-tw": "chi_tra", "zh-hant": "chi_tra"}


class OCRError(RuntimeError):
    pass


def select_engine(engine: str | None = None) -> str:
    selected = (engine if engine is not None else os.environ.get("YOUTUBE_DEEP_ANALYZER_OCR_ENGINE", "auto")).strip().lower()
    if selected not in ENGINES:
        raise OCRError(f"ocr_engine must be one of {sorted(ENGINES)}.")
    return selected


def find_tesseract() -> str | None:
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_TESSERACT", "").strip()
    if override:
        path = Path(override).expanduser()
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which("tesseract")


def tesseract_languages(languages: str) -> list[str]:
    result = []
    for value in re.split(r"[,+]", languages):
        value = value.strip().lower()
        if not value:
            continue
        code = LANGUAGES.get(value) or LANGUAGES.get(value.split("-")[0])
        if code is None and re.fullmatch(r"[a-z]{3}(?:_[a-z]+)?", value):
            code = value
        if code is None:
            raise OCRError(f"Unsupported OCR language {value!r}; use a Tesseract code such as kor or eng.")
        if code not in result:
            result.append(code)
    if not result:
        raise OCRError("At least one OCR language is required.")
    return result


def _run(command: list[str], timeout: float, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    return subprocess.CompletedProcess(result.args, result.returncode,
        result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace"))


def run_apple_vision(paths: list[Path], mode: str, languages: str, cache_dir: Path) -> dict[str, dict[str, Any]]:
    swiftc = shutil.which("swiftc")
    source = Path(__file__).resolve().parent / "apple_vision_ocr.swift"
    if sys.platform != "darwin" or not swiftc or not source.is_file():
        raise OCRError("Apple Vision requires macOS and swiftc.")
    binary = cache_dir / "bin" / "apple-vision-ocr"
    modules = cache_dir / "swift-module-cache"
    binary.parent.mkdir(parents=True, exist_ok=True)
    modules.mkdir(parents=True, exist_ok=True)
    if not binary.is_file() or binary.stat().st_mtime < source.stat().st_mtime:
        result = _run([swiftc, "-module-cache-path", str(modules), str(source), "-o", str(binary)], 180)
        if result.returncode:
            raise OCRError(f"Apple Vision compilation failed: {result.stderr.strip()[-2000:]}")
    result = _run([str(binary), "--mode", mode, "--languages", languages, *map(str, paths)], max(180, len(paths) * 3))
    if result.returncode:
        raise OCRError(f"Apple Vision failed: {result.stderr.strip()[-2000:]}")
    return {str(item["path"]): item for item in json.loads(result.stdout)}


def run_tesseract(paths: list[Path], languages: str) -> dict[str, dict[str, Any]]:
    executable = find_tesseract()
    if not executable:
        raise OCRError("Tesseract not found; install it on PATH or set YOUTUBE_DEEP_ANALYZER_TESSERACT.")
    requested = tesseract_languages(languages)
    available = _run([executable, "--list-langs"], 30)
    if available.returncode:
        raise OCRError(f"Cannot list Tesseract languages: {available.stderr.strip()[-1000:]}")
    installed = set(available.stdout.splitlines())
    missing = set(requested) - installed
    if missing:
        raise OCRError(f"Missing Tesseract language data: {', '.join(sorted(missing))}. Install it or set TESSDATA_PREFIX.")
    outputs = {}
    for path in paths:
        started = time.perf_counter()
        try:
            # stdin avoids native Tesseract builds that cannot open Unicode Windows paths.
            result = _run([executable, "stdin", "stdout", "-l", "+".join(requested), "--psm", "6", "tsv"],
                          120, input_bytes=path.read_bytes())
            if result.returncode:
                raise OCRError(result.stderr.strip()[-2000:])
            words, confidences = [], []
            for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
                if not (row.get("text") or "").strip():
                    continue
                words.append(row["text"].strip())
                try:
                    confidence = float(row.get("conf", "-1"))
                except (ValueError, TypeError):
                    confidence = -1
                if 0 <= confidence <= 100:
                    confidences.append(confidence / 100)
            outputs[str(path)] = {"path": str(path), "text": " ".join(words),
                "average_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
                "observation_count": len(words), "engine": "tesseract",
                "elapsed_ms": (time.perf_counter() - started) * 1000}
        except (OCRError, OSError, subprocess.TimeoutExpired) as exc:
            outputs[str(path)] = {"path": str(path), "text": "", "average_confidence": 0.0,
                                  "engine": "tesseract", "error": str(exc)}
    return outputs


def run_local_ocr(paths: list[Path], mode: str, languages: str, cache_dir: Path,
                  engine: str | None = None) -> tuple[str, dict[str, dict[str, Any]], str | None]:
    selected = select_engine(engine)
    if not paths or selected == "none":
        return "none", {}, None
    candidates = (["apple-vision", "tesseract"] if sys.platform == "darwin" else ["tesseract"]) if selected == "auto" else [selected]
    outputs: dict[str, dict[str, Any]] = {}
    reasons: dict[str, list[str]] = {str(path): [] for path in paths}
    pending = list(paths)
    for candidate in candidates:
        if not pending:
            break
        failure = None
        try:
            batch = run_apple_vision(pending, mode, languages, cache_dir) if candidate == "apple-vision" else run_tesseract(pending, languages)
        except (OCRError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            failure = f"{candidate}: {exc}"
            batch = {}
        remaining = []
        for path in pending:
            key = str(path)
            item = batch.get(key)
            if item is None or item.get("error"):
                if item:
                    outputs[key] = item
                reasons[key].append(failure or f"{candidate}: {(item or {}).get('error', 'no result')}")
                remaining.append(path)
            else:
                if reasons[key]:
                    item = {**item, "fallback_reasons": reasons[key]}
                outputs[key] = item
        pending = remaining
    used = sorted({item.get("engine", "unknown") for item in outputs.values() if not item.get("error")})
    for path in pending:
        key = str(path)
        outputs[key] = {**outputs.get(key, {}), "path": key, "text": "", "average_confidence": 0.0,
                        "error": "; ".join(reasons[key]) or "OCR returned no result."}
    failures = list(dict.fromkeys(reason for values in reasons.values() for reason in values))
    error = None if used else "; ".join(failures) or "No OCR backend returned a result."
    return "+".join(used) if used else "unavailable", outputs, error
