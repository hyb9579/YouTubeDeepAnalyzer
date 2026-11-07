from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smoke_test_mcp import PLUGIN_ROOT, _load_server_config, _server_parameters


class MCPLaunchTests(unittest.TestCase):
    def test_codex_uses_plugin_root_without_claude_variables(self) -> None:
        with tempfile.TemporaryDirectory() as working_directory:
            parameters = _server_parameters("codex", Path(working_directory), {})
        self.assertEqual(parameters.args, ["./scripts/launch_mcp.py"])
        self.assertEqual(Path(parameters.cwd), PLUGIN_ROOT)

    def test_claude_overrides_shared_config_and_uses_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as working_directory:
            parameters = _server_parameters("claude", Path(working_directory), {})
            self.assertEqual(Path(parameters.cwd), Path(working_directory))
        self.assertEqual(parameters.command, "python3")
        self.assertEqual(len(parameters.args), 1)
        self.assertEqual(Path(parameters.args[0]), PLUGIN_ROOT / "scripts/launch_mcp.py")
        self.assertEqual(parameters.env["CLAUDE_PLUGIN_ROOT"], str(PLUGIN_ROOT))

    def test_claude_rejects_old_shell_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary)
            (plugin / ".mcp.json").write_text((PLUGIN_ROOT / ".mcp.json").read_text())
            (plugin / ".claude-plugin").mkdir()
            manifest = {"mcpServers": {"youtube-media": {
                "command": "sh", "args": ["-c", "exec sh ${CLAUDE_PLUGIN_ROOT:-.}/scripts/launch_mcp.py"],
                "cwd": ".",
            }}}
            (plugin / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
            with self.assertRaises(RuntimeError):
                _server_parameters("claude", plugin, {}, plugin)

    def test_installed_root_with_spaces_is_one_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "installed plugin"
            plugin.mkdir()
            (plugin / ".mcp.json").write_text((PLUGIN_ROOT / ".mcp.json").read_text())
            (plugin / ".claude-plugin").mkdir()
            (plugin / ".claude-plugin/plugin.json").write_text(
                (PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text()
            )
            parameters = _server_parameters("claude", Path(temporary), {}, plugin)
            self.assertEqual(len(parameters.args), 1)
            self.assertEqual(Path(parameters.args[0]), plugin / "scripts/launch_mcp.py")
            self.assertNotIn("cwd", _load_server_config("claude", plugin))


if __name__ == "__main__":
    unittest.main()
