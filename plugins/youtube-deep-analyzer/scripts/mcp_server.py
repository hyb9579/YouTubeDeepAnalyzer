from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BASE_RUNTIME_PATH = os.environ.get("YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME", "").strip()
if BASE_RUNTIME_PATH:
    base_runtime_dir = Path(BASE_RUNTIME_PATH)
    if base_runtime_dir.is_dir():
        sys.path.insert(0, str(base_runtime_dir.resolve()))
RUNTIME_PATH = os.environ.get("YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH", "").strip()
if RUNTIME_PATH:
    runtime_dir = Path(RUNTIME_PATH)
    if runtime_dir.is_dir():
        sys.path.insert(0, str(runtime_dir.resolve()))

try:
    from mcp.server.mcpserver import Audio, Image, MCPServer
    from mcp.types import ToolAnnotations
except ImportError as exc:
    raise SystemExit(
        "youtube-deep-analyzer runtime is missing. Reinstall the plugin or run scripts/bootstrap_runtime.py."
    ) from exc

from media_pipeline import (
    CaptionUnavailableError,
    MediaPipelineError,
    MediaTransportError,
    delete_job,
    fetch_caption_segments,
    fetch_captions,
    get_audio_path,
    get_frame_at_path,
    get_frame_path,
    get_manifest,
    local_stt_runtime_status,
    prepare_adaptive_media,
    prepare_media,
    prepare_transcript_bundle,
    prepare_transcript_media,
    prepare_visual_ranges,
    probe_youtube,
    runtime_update_status,
    transcribe_job_audio_locally,
    transcribe_job_audio_ranges_locally,
    verify_caption_ranges,
)
from local_stt import LocalSTTError


server = MCPServer(
    name="youtube-media",
    title="YouTube Media",
    description=(
        "Use normalized captions as the primary transcript, apply local STT only to materially ambiguous caption ranges, "
        "and inspect only visual ranges whose screen context is actually needed."
    ),
    instructions=(
        "Use only for public videos or content the user is authorized to access. "
        "Never bypass authentication, regional restrictions, paywalls, or DRM."
    ),
    version="1.8.0",
)


def _wrap_error(exc: Exception, default_stage: str = "validation") -> dict[str, Any]:
    if isinstance(exc, LocalSTTError):
        return exc.to_dict()
    if isinstance(exc, MediaTransportError):
        return {"ok": False, **exc.to_dict()}
    if isinstance(exc, CaptionUnavailableError):
        return {
            "ok": False,
            "error": str(exc),
            "stage": "captions",
            "error_code": "captions_unavailable",
            "http_status": None,
            "retryable": False,
        }
    if isinstance(exc, MediaPipelineError):
        return {
            "ok": False,
            "error": str(exc),
            "stage": default_stage,
            "error_code": "media_pipeline_error",
            "http_status": None,
            "retryable": False,
        }
    return {
        "ok": False,
        "error": f"Unexpected media pipeline failure: {exc}",
        "stage": "unexpected",
        "error_code": "unexpected_media_pipeline_error",
        "http_status": None,
        "retryable": False,
    }


@server.tool(
    name="youtube_probe",
    title="Inspect YouTube metadata",
    description=(
        "Inspect a public YouTube video or playlist without downloading media. Returns durations, canonical URLs, "
        "playlist positions, and caption availability so adaptive preparation and playlist order can be planned first."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_probe(url: str, detailed_playlist: bool = True, max_items: int = 500) -> dict[str, Any]:
    try:
        return {"ok": True, "result": probe_youtube(url, detailed_playlist, max_items)}
    except Exception as exc:
        return _wrap_error(exc, "metadata")


@server.tool(
    name="youtube_runtime_status",
    title="Inspect the active yt-dlp runtime",
    description=(
        "Report the loaded, bundled, and latest checked stable yt-dlp versions plus automatic-update outcome. "
        "The launcher checks official PyPI at most once per 24 hours, verifies the wheel SHA-256, validates it in "
        "isolation, and falls back to the last verified or bundled runtime when unavailable."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_runtime_status() -> dict[str, Any]:
    try:
        return {"ok": True, "result": runtime_update_status()}
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_stt_runtime_status",
    title="Inspect the local STT runtime",
    description=(
        "Report whether whisper-cli and the verified multilingual local model are ready. The model is downloaded once "
        "on the first transcription, verified by SHA-256, cached locally, and then used without model API tokens."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_stt_runtime_status() -> dict[str, Any]:
    try:
        return {"ok": True, "result": local_stt_runtime_status()}
    except Exception as exc:
        return _wrap_error(exc, "local_stt")


@server.tool(
    name="youtube_get_captions",
    title="Fetch YouTube captions",
    description=(
        "Fetch a complete raw manual or automatic caption file without downloading media. Prefer the normalized "
        "caption-segment tool for analysis; use this compatibility tool when raw caption markup is specifically needed."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_get_captions(url: str, preferred_languages: str = "ko,en") -> dict[str, Any]:
    try:
        return {"ok": True, "result": fetch_captions(url, preferred_languages)}
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_get_caption_segments",
    title="Fetch normalized YouTube caption segments",
    description=(
        "Fetch manual or automatic captions and locally remove rolling-caption duplication. Returns compact, "
        "timestamped text segments for transcript construction."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_get_caption_segments(url: str, preferred_languages: str = "ko,en") -> dict[str, Any]:
    try:
        return {"ok": True, "result": fetch_caption_segments(url, preferred_languages)}
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_prepare_transcript_bundle",
    title="Prepare a caption transcript without audio",
    description=(
        "Default transcript tool. It fetches and normalizes one caption track, but does not download audio, run STT, "
        "or inspect video. Review these captions first. Use selective local STT only when an exact word, term, or "
        "expression remains materially ambiguous after contextual review."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_prepare_transcript_bundle(
    url: str,
    preferred_languages: str = "ko,en",
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": prepare_transcript_bundle(url, preferred_languages),
        }
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_prepare_transcript",
    title="Prepare audio without scanning video",
    description=(
        "Download the public audio source and split it into chronological chunks without analyzing speaker, tone, "
        "pitch, energy, pauses, or emotion. "
        "Use only after caption review identifies a lexical verification need, or when the user explicitly requests audio."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_prepare_transcript(url: str, audio_chunk_seconds: int = 300) -> dict[str, Any]:
    try:
        return {"ok": True, "result": prepare_transcript_media(url, audio_chunk_seconds)}
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_verify_caption_ranges",
    title="Verify only ambiguous caption ranges with local STT",
    description=(
        "After reviewing normalized captions, download public audio and run local STT only on the supplied chronological, "
        "non-overlapping ranges. Each range must include the caption text and a precise lexical verification question. "
        "This tool is optional, does not transcribe the full audio, and is not a prerequisite for visual inspection."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_verify_caption_ranges(
    url: str,
    verification_ranges: list[dict[str, Any]],
    language: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": verify_caption_ranges(url, verification_ranges, language, force),
        }
    except Exception as exc:
        return _wrap_error(exc, "local_stt")


@server.tool(
    name="youtube_transcribe_audio_ranges_locally",
    title="Verify ranges in already prepared audio",
    description=(
        "Run local STT only on caption-verification ranges in an existing audio job. This reusable form avoids another "
        "download and never makes STT a requirement for visual analysis."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_transcribe_audio_ranges_locally(
    job_id: str,
    verification_ranges: list[dict[str, Any]],
    language: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    try:
        return {"ok": True, "result": transcribe_job_audio_ranges_locally(job_id, verification_ranges, language, force)}
    except Exception as exc:
        return _wrap_error(exc, "local_stt")


@server.tool(
    name="youtube_transcribe_audio_locally",
    title="Transcribe entire prepared audio (compatibility)",
    description=(
        "Compatibility tool for an explicit user request for a full local transcript. Do not use it merely to analyze or "
        "summarize a captioned video; prefer caption review and youtube_verify_caption_ranges for unresolved wording."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_transcribe_audio_locally(job_id: str, language: str = "auto", force: bool = False) -> dict[str, Any]:
    try:
        return {"ok": True, "result": transcribe_job_audio_locally(job_id, language, force)}
    except Exception as exc:
        return _wrap_error(exc, "local_stt")


@server.tool(
    name="youtube_prepare_visual_ranges",
    title="Scan caption-selected visual ranges with local OCR",
    description=(
        "After caption review has produced explicit unresolved visual questions, download the video source, "
        "decode only the supplied non-overlapping time ranges, select local perceptual changes, run local OCR, and mark "
        "which frames still need image-model analysis. Passing this tool no ranges is invalid; a broad whole-video pass "
        "requires allow_whole_video_visual_pass=true. This tool never requires prior STT."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_prepare_visual_ranges(
    url: str,
    visual_ranges: list[dict[str, Any]],
    visual_scan_interval_seconds: float = 1.0,
    visual_change_threshold: float = 0.08,
    max_candidates_per_range: int = 24,
    max_height: Literal[360, 480, 720, 1080] = 720,
    ocr_mode: Literal["fast", "accurate"] = "accurate",
    ocr_languages: str = "ko-KR,en-US",
    allow_whole_video_visual_pass: bool = False,
    ocr_engine: Literal["auto", "apple-vision", "tesseract", "none"] | None = None,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": prepare_visual_ranges(
                url=url,
                visual_ranges=visual_ranges,
                visual_scan_interval_seconds=visual_scan_interval_seconds,
                visual_change_threshold=visual_change_threshold,
                max_candidates_per_range=max_candidates_per_range,
                max_height=max_height,
                ocr_mode=ocr_mode,
                ocr_languages=ocr_languages,
                allow_whole_video_visual_pass=allow_whole_video_visual_pass,
                ocr_engine=ocr_engine,
            ),
        }
    except Exception as exc:
        return _wrap_error(exc, "visual_prepare")


@server.tool(
    name="youtube_prepare_adaptive",
    title="Prepare whole-video adaptive candidates (compatibility)",
    description=(
        "Download one permitted public YouTube video, split its audio, scan tiny RGB frames locally, and "
        "extract only perceptually distinct visual candidates using pHash, dHash, aHash, pixel difference, and "
        "grayscale/color histogram change. Compatibility only: new analysis should review captions first and use "
        "youtube_prepare_visual_ranges instead. Never pass a playlist URL."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_prepare_adaptive(
    url: str,
    audio_chunk_seconds: int = 300,
    visual_scan_interval_seconds: float = 2.0,
    visual_change_threshold: float = 0.18,
    max_visual_candidates: int = 96,
    max_height: Literal[360, 480, 720, 1080] = 720,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": prepare_adaptive_media(
                url=url,
                audio_chunk_seconds=audio_chunk_seconds,
                visual_scan_interval_seconds=visual_scan_interval_seconds,
                visual_change_threshold=visual_change_threshold,
                max_visual_candidates=max_visual_candidates,
                max_height=max_height,
            ),
        }
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_prepare_media",
    title="Download and prepare YouTube media (compatibility)",
    description=(
        "Compatibility tool for fixed-interval audiovisual or audio-only preparation. New analysis workflows should "
        "use caption review followed directly by selective STT and/or youtube_prepare_visual_ranges as needed."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_prepare_media(
    url: str,
    analysis_mode: Literal["audiovisual", "audio-only"],
    frame_interval_seconds: int = 30,
    audio_chunk_seconds: int = 300,
    max_height: Literal[360, 480, 720, 1080] = 720,
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "result": prepare_media(
                url,
                analysis_mode,
                frame_interval_seconds,
                audio_chunk_seconds,
                max_height,
            ),
        }
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_media_manifest",
    title="Read prepared media manifest",
    description="Return the ordered frame and audio chunk manifest for a prepared YouTube media job.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def youtube_media_manifest(job_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "result": get_manifest(job_id)}
    except Exception as exc:
        return _wrap_error(exc)


@server.tool(
    name="youtube_get_frame",
    title="Send one unresolved visual frame to the image model",
    description=(
        "Return one actual JPEG image only after the targeted-range manifest marks image_model_recommended=true. "
        "Frames resolved by local OCR or near-duplicate filtering are rejected unless override_reason explains a new unresolved need. "
        "Calling this tool introduces model image-input tokens."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    structured_output=False,
)
def youtube_get_frame(job_id: str, frame_index: int, override_reason: str = "") -> list[Any]:
    try:
        path, item = get_frame_path(job_id, frame_index)
        recommended = item.get("image_model_recommended")
        if recommended is False and not override_reason.strip():
            raise MediaPipelineError(
                "This frame was resolved by local OCR or duplicate filtering. Provide override_reason only when a new "
                "unresolved spatial, chart, equation, demonstration, motion, or before/after question exists."
            )
        return [
            (
                f"job_id={job_id}; frame_index={frame_index}; timestamp_seconds={item['timestamp_seconds']}; "
                f"visual_question={item.get('visual_question')}; local_ocr={json.dumps(item.get('local_ocr'), ensure_ascii=False)}; "
                f"override_reason={override_reason.strip() or 'none'}"
            ),
            Image(path=path),
        ]
    except Exception as exc:
        return [_wrap_error(exc)]


@server.tool(
    name="youtube_get_frame_at",
    title="Read a caption-targeted video frame",
    description=(
        "Extract and return one exact frame at a timestamp from an adaptive or audiovisual job. Use only when the "
        "caption context explicitly or implicitly depends on unseen visual meaning that local OCR and candidates do not resolve. "
        "An unresolved_visual_question is required because this call introduces model image-input tokens."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    structured_output=False,
)
def youtube_get_frame_at(
    job_id: str,
    timestamp_seconds: float,
    max_height: Literal[360, 480, 720, 1080] = 720,
    unresolved_visual_question: str = "",
) -> list[Any]:
    try:
        if not unresolved_visual_question.strip():
            raise MediaPipelineError(
                "unresolved_visual_question is required before sending an exact frame to the image model."
            )
        path, item = get_frame_at_path(job_id, timestamp_seconds, max_height)
        return [
            (
                f"job_id={job_id}; timestamp_seconds={item['timestamp_seconds']}; "
                f"selection_reason={item['selection_reason']}; "
                f"unresolved_visual_question={unresolved_visual_question.strip()}"
            ),
            Image(path=path),
        ]
    except Exception as exc:
        return [_wrap_error(exc)]


@server.tool(
    name="youtube_get_audio_chunk",
    title="Return a requested audio chunk",
    description=(
        "Return one actual MP3 audio chunk and its time range from a prepared YouTube media job when the user requests "
        "the file. Normal caption-based analysis does not require returning this audio attachment to the model."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    structured_output=False,
)
def youtube_get_audio_chunk(job_id: str, chunk_index: int) -> list[Any]:
    try:
        path, item = get_audio_path(job_id, chunk_index)
        return [
            (
                f"job_id={job_id}; chunk_index={chunk_index}; "
                f"start_seconds={item['start_seconds']}; end_seconds={item['end_seconds']}"
            ),
            Audio(path=path),
        ]
    except Exception as exc:
        return [_wrap_error(exc)]


@server.tool(
    name="youtube_delete_job",
    title="Delete downloaded media",
    description=(
        "Delete one prepared media job from the plugin's private cache after analysis and transcript export. "
        "The confirmation value must exactly match the job_id."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
)
def youtube_delete_job(job_id: str, confirmation: str) -> dict[str, Any]:
    try:
        return {"ok": True, "result": delete_job(job_id, confirmation)}
    except Exception as exc:
        return _wrap_error(exc)


if __name__ == "__main__":
    server.run(transport="stdio")
