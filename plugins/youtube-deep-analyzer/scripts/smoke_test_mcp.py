from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PLUGIN_ROOT / "vendor"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

from mcp import ClientSession, StdioServerParameters, stdio_client


EXPECTED_TOOLS = {
    "youtube_probe",
    "youtube_runtime_status",
    "youtube_stt_runtime_status",
    "youtube_get_captions",
    "youtube_get_caption_segments",
    "youtube_prepare_transcript_bundle",
    "youtube_verify_caption_ranges",
    "youtube_transcribe_audio_ranges_locally",
    "youtube_transcribe_audio_locally",
    "youtube_prepare_transcript",
    "youtube_prepare_visual_ranges",
    "youtube_prepare_adaptive",
    "youtube_prepare_media",
    "youtube_media_manifest",
    "youtube_get_frame",
    "youtube_get_frame_at",
    "youtube_get_audio_chunk",
    "youtube_delete_job",
}


def _load_server_config() -> dict[str, Any]:
    config_path = PLUGIN_ROOT / ".mcp.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("mcpServers") or config.get("mcp_servers") or config
    server = servers.get("youtube-media")
    if not isinstance(server, dict):
        raise RuntimeError(".mcp.json does not define youtube-media.")
    if server.get("cwd") != ".":
        raise RuntimeError("youtube-media must set cwd to '.' so relative launcher paths resolve from the plugin root.")
    return server


async def _run() -> None:
    server = _load_server_config()
    declared_cwd = (PLUGIN_ROOT / server["cwd"]).resolve()
    if declared_cwd != PLUGIN_ROOT.resolve():
        raise RuntimeError("youtube-media cwd escaped the plugin root.")

    with tempfile.TemporaryDirectory(prefix="youtube-deep-analyzer-smoke-") as unrelated_cwd:
        original_cwd = Path.cwd()
        os.chdir(unrelated_cwd)
        try:
            server_environment = dict(os.environ)
            server_environment.setdefault(
                "YOUTUBE_DEEP_ANALYZER_CACHE", str(Path(unrelated_cwd) / "runtime-cache")
            )
            server_environment.setdefault("YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE", "0")
            parameters = StdioServerParameters(
                command=server["command"],
                args=list(server.get("args", [])),
                cwd=declared_cwd,
                env=server_environment,
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    runtime_call = await session.call_tool("youtube_runtime_status", {})
                    runtime_payload = runtime_call.structured_content or {}
                    stt_call = await session.call_tool("youtube_stt_runtime_status", {})
                    stt_payload = stt_call.structured_content or {}
        finally:
            os.chdir(original_cwd)

    missing = EXPECTED_TOOLS - tools
    if missing:
        raise RuntimeError(f"youtube-media started but tools are missing: {sorted(missing)}")
    print(
        json.dumps(
            {
                "ok": True,
                "server": initialized.server_info.name,
                "version": initialized.server_info.version,
                "tool_count": len(tools),
                "tools": sorted(tools),
                "runtime_status": runtime_payload.get("result"),
                "stt_runtime": stt_payload.get("result"),
                "launched_from_unrelated_cwd": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(_run())
