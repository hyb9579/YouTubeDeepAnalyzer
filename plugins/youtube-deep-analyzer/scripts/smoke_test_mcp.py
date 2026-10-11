from __future__ import annotations

import argparse
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


def _load_server_config(host: str = "codex", plugin_root: Path = PLUGIN_ROOT) -> dict[str, Any]:
    config = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    servers = dict(config["mcpServers"])
    if host == "claude":
        manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        overrides = manifest.get("mcpServers")
        if not isinstance(overrides, dict):
            raise RuntimeError("Claude must declare an inline MCP launch configuration.")
        servers.update(overrides)
    elif host != "codex":
        raise ValueError(f"Unsupported host: {host}")
    server = servers.get("youtube-media")
    if not isinstance(server, dict):
        raise RuntimeError("The plugin does not define youtube-media.")
    return server


def _server_parameters(
    host: str, working_directory: Path, environment: dict[str, str], plugin_root: Path = PLUGIN_ROOT
) -> StdioServerParameters:
    server = _load_server_config(host, plugin_root)
    arguments = list(server.get("args", []))
    server_environment = dict(environment)
    if host == "claude":
        # Model the documented Claude args substitution, without invoking a shell.
        if arguments != ["${CLAUDE_PLUGIN_ROOT}/scripts/start_mcp.sh"] or "cwd" in server:
            raise RuntimeError("Claude must use the installed plugin root without a cwd fallback.")
        arguments = [argument.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for argument in arguments]
        server_environment["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        cwd = working_directory
    else:
        if arguments != ["./scripts/start_mcp.sh"] or server.get("cwd") != ".":
            raise RuntimeError("Codex must launch relative to the plugin root.")
        cwd = plugin_root
    return StdioServerParameters(command=server["command"], args=arguments, cwd=cwd, env=server_environment)


async def _run(host: str = "codex") -> None:
    with tempfile.TemporaryDirectory(prefix="youtube-deep-analyzer-smoke-") as unrelated_cwd:
        server_environment = dict(os.environ)
        server_environment.setdefault(
            "YOUTUBE_DEEP_ANALYZER_CACHE", str(Path(unrelated_cwd) / "runtime-cache")
        )
        server_environment.setdefault("YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE", "0")
        parameters = _server_parameters(host, Path(unrelated_cwd), server_environment)
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                runtime_call = await session.call_tool("youtube_runtime_status", {})
                runtime_payload = runtime_call.structured_content or {}
                stt_call = await session.call_tool("youtube_stt_runtime_status", {})
                stt_payload = stt_call.structured_content or {}

    if not runtime_payload.get("ok") or not stt_payload.get("ok"):
        raise RuntimeError("The MCP server connected but a runtime status tool failed.")

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
                "host": host,
                "server_cwd_is_plugin_root": host == "codex",
                "claude_started_from_unrelated_cwd": host == "claude",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the host-specific MCP launch and tool registration.")
    parser.add_argument("--host", choices=("codex", "claude"), default="codex")
    asyncio.run(_run(parser.parse_args().host))
