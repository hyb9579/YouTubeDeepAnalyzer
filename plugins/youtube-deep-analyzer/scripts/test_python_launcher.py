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
            with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
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

    def test_failed_bootstrap_is_not_started_as_a_server(self) -> None:
        with patch.object(launcher, "_helper", side_effect=RuntimeError("bootstrap failed")):
            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                launcher.prepare_environment("python", {})


if __name__ == "__main__":
    unittest.main()
