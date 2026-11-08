from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import launch_mcp as launcher


class PythonLauncherTests(unittest.TestCase):
    def test_current_supported_python_needs_no_probe(self) -> None:
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": ""}), patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.select_python(), sys.executable)
            run.assert_not_called()

    def test_python_311_is_accepted_without_a_probe(self) -> None:
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": ""}), patch.object(launcher.sys, "version_info", (3, 11)), patch.object(launcher.subprocess, "run") as run:
            self.assertEqual(launcher.select_python(), sys.executable)
            run.assert_not_called()

    def test_python_310_is_rejected_when_no_supported_candidate_exists(self) -> None:
        unsupported = subprocess.CompletedProcess([], 1, "", "")
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": ""}), patch.object(launcher.sys, "version_info", (3, 10)), patch.object(launcher.subprocess, "run", return_value=unsupported) as run:
            with self.assertRaisesRegex(RuntimeError, "Python 3.11"):
                launcher.select_python()
            self.assertTrue(run.called)
            self.assertIn("(3, 11)", run.call_args.args[0][-1])

    def test_direct_bootstrap_rejects_python_310_before_installation(self) -> None:
        import bootstrap_runtime
        with patch.object(bootstrap_runtime.sys, "version_info", (3, 10)), patch.object(bootstrap_runtime, "cache_root") as cache:
            with self.assertRaisesRegex(bootstrap_runtime.BootstrapError, "Python 3.11"):
                bootstrap_runtime.resolve_runtime()
            cache.assert_not_called()

    def test_old_default_python_selects_supported_interpreter(self) -> None:
        failures = subprocess.CompletedProcess([], 1, "", "")
        success = subprocess.CompletedProcess([], 0, "/supported/python\n", "")
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": ""}), patch.object(launcher.sys, "version_info", (3, 7)), patch.object(launcher.subprocess, "run", side_effect=[failures, success]):
            self.assertEqual(launcher.select_python(), "/supported/python")

    def test_explicit_python_path_with_spaces_is_one_argument(self) -> None:
        result = subprocess.CompletedProcess([], 0, "C:/Python Tools/python.exe\n", "")
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": "C:/Python Tools/python.exe"}), patch.object(launcher.subprocess, "run", return_value=result) as run:
            self.assertEqual(launcher.select_python(), "C:/Python Tools/python.exe")
            self.assertEqual(run.call_args.args[0][0], "C:/Python Tools/python.exe")

    def test_unavailable_interpreter_reports_requirement(self) -> None:
        with patch.dict(os.environ, {"YOUTUBE_DEEP_ANALYZER_PYTHON": "missing"}), patch.object(launcher.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "Python 3.11"):
                launcher.select_python()

    def test_environment_uses_platform_path_separator_and_overlay_precedence(self) -> None:
        with patch.object(launcher, "_helper", side_effect=["base runtime", "overlay runtime"]):
            result = launcher.prepare_environment("python", {"PYTHONPATH": "existing"})
        self.assertEqual(result["PYTHONPATH"], os.pathsep.join(["overlay runtime", "base runtime", "existing"]))
        self.assertEqual(result["YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH"], "overlay runtime")

    def test_stale_overlay_environment_is_cleared(self) -> None:
        with patch.object(launcher, "_helper", side_effect=["base", ""]):
            result = launcher.prepare_environment("python", {"YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH": "stale"})
        self.assertNotIn("YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH", result)
        self.assertEqual(result["PYTHONPATH"], "base")

    def test_server_runs_in_process_with_runtime_paths_and_original_stdio(self) -> None:
        original_stdin, original_stdout = sys.stdin, sys.stdout
        with patch.dict(os.environ, clear=True), patch.object(launcher.sys, "path", ["existing"]), patch.object(launcher.sys, "argv", []), patch.object(launcher.runpy, "run_path") as run:
            launcher.run_server({"PYTHONPATH": os.pathsep.join(["overlay", "base"]), "MARKER": "value"})
            self.assertEqual(launcher.sys.path[:2], ["overlay", "base"])
            self.assertEqual(os.environ["MARKER"], "value")
            self.assertIs(sys.stdin, original_stdin)
            self.assertIs(sys.stdout, original_stdout)
            run.assert_called_once_with(str(launcher.ROOT / "scripts/mcp_server.py"), run_name="__main__")

    def test_failed_bootstrap_is_not_started_as_a_server(self) -> None:
        with patch.object(launcher, "_helper", side_effect=RuntimeError("bootstrap failed")):
            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                launcher.prepare_environment("python", {})


if __name__ == "__main__":
    unittest.main()
