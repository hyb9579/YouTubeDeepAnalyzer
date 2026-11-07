from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "youtube-deep-analyzer"


def fail(message: str) -> None:
    raise SystemExit(f"validation failed: {message}")


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"{path.relative_to(ROOT)} is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        fail(f"{path.relative_to(ROOT)} must contain a JSON object")
    return payload


def validate_marketplaces() -> None:
    codex = read_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude = read_json(ROOT / ".claude-plugin" / "marketplace.json")
    if codex.get("name") != "youtube-deep-analyzer" or claude.get("name") != "youtube-deep-analyzer":
        fail("marketplace names must stay synchronized")
    codex_entry = (codex.get("plugins") or [{}])[0]
    claude_entry = (claude.get("plugins") or [{}])[0]
    if codex_entry.get("source", {}).get("path") != "./plugins/youtube-deep-analyzer":
        fail("Codex marketplace source path is incorrect")
    if claude_entry.get("source") != "./plugins/youtube-deep-analyzer":
        fail("Claude marketplace source path is incorrect")
    if not PLUGIN.is_dir():
        fail("marketplace plugin source does not exist")


def validate_plugin() -> None:
    codex = read_json(PLUGIN / ".codex-plugin" / "plugin.json")
    claude = read_json(PLUGIN / ".claude-plugin" / "plugin.json")
    marketplace = read_json(ROOT / ".claude-plugin" / "marketplace.json")
    versions = {codex.get("version"), claude.get("version"), (marketplace.get("plugins") or [{}])[0].get("version")}
    if len(versions) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", str(next(iter(versions)))):
        fail(f"release versions are not synchronized semantic versions: {sorted(str(value) for value in versions)}")
    if codex.get("name") != "youtube-deep-analyzer" or claude.get("name") != "youtube-deep-analyzer":
        fail("plugin names must stay synchronized")
    read_json(PLUGIN / ".mcp.json")
    skill = (PLUGIN / "skills" / "analyze-youtube-video" / "SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\n") or "name: analyze-youtube-video" not in skill[:1000]:
        fail("skill frontmatter is missing or invalid")
    requirements = (PLUGIN / "requirements.lock").read_text(encoding="utf-8").splitlines()
    package_lines = [line for line in requirements if line and not line.startswith("#")]
    if not package_lines or any("==" not in line for line in package_lines):
        fail("every runtime dependency must be exactly pinned")
    required = [
        "scripts/bootstrap_runtime.py",
        "scripts/launch_mcp.py",
        "scripts/local_ocr.py",
        "scripts/start_mcp.sh",
        "scripts/mcp_server.py",
        "scripts/media_pipeline.py",
    ]
    for relative in required:
        if not (PLUGIN / relative).is_file():
            fail(f"required plugin file is missing: {relative}")


def validate_portability() -> None:
    forbidden = ["youtube-deep-analyzer-auto-update"]
    developer_path_patterns = [
        ("Unix user home path", re.compile(r"/(?:Users|home)/[^/\s\"']+/")),
        ("Windows user home path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+\\", re.IGNORECASE)),
    ]
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            fail(f"symbolic links are not allowed in the release: {path.relative_to(ROOT)}")
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.stat().st_size > 20 * 1024 * 1024:
            fail(f"release file exceeds 20 MiB: {path.relative_to(ROOT)}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for marker in forbidden:
            if marker in text:
                fail(f"developer-local marker {marker!r} found in {path.relative_to(ROOT)}")
        for label, pattern in developer_path_patterns:
            if pattern.search(text):
                fail(f"{label} found in {path.relative_to(ROOT)}")
    if (PLUGIN / "vendor").exists():
        fail("vendor directory must not be committed")


def main() -> int:
    validate_marketplaces()
    validate_plugin()
    validate_portability()
    print("distribution validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
