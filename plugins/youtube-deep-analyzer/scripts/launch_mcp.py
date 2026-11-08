"""Start the MCP server without a POSIX shell (bootstrap syntax supports Python 3.7+)."""
import os
import runpy
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def select_python():
    override = os.environ.get("YOUTUBE_DEEP_ANALYZER_PYTHON", "").strip()
    if not override and sys.version_info >= (3, 11):
        return sys.executable
    candidates = [[override]] if override else [
        [sys.executable], ["/opt/homebrew/bin/python3"], ["/usr/local/bin/python3"],
        ["python3"], ["python"], ["py", "-3"],
    ]
    probe = "import sys; sys.exit(1) if sys.version_info < (3, 11) else print(sys.executable)"
    for candidate in candidates:
        try:
            result = subprocess.run(candidate + ["-c", probe], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=15)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("Python 3.11 or newer is required. Install Python on PATH or set YOUTUBE_DEEP_ANALYZER_PYTHON.")


def _helper(python, script, environment):
    result = subprocess.run([python, str(ROOT / "scripts" / script), "--print-path"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                            errors="replace", env=environment, timeout=1900)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise RuntimeError("{} failed (exit {}).".format(script, result.returncode))
    return result.stdout.strip()


def prepare_environment(python, environment):
    result = dict(environment)
    base = _helper(python, "bootstrap_runtime.py", result)
    if not base:
        raise RuntimeError("Dependency bootstrap returned no runtime path.")
    result["YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME"] = base
    paths = [base]
    if result.get("PYTHONPATH"):
        paths.append(result["PYTHONPATH"])
    result["PYTHONPATH"] = os.pathsep.join(paths)
    overlay = _helper(python, "runtime_update.py", result)
    result.pop("YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH", None)
    if overlay:
        result["YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH"] = overlay
        result["PYTHONPATH"] = os.pathsep.join([overlay, result["PYTHONPATH"]])
    return result


def run_server(environment):
    # Windows execve can close the transport before the replacement is ready.
    # Run the server in this process so the host's stdio handles stay alive.
    os.environ.clear()
    os.environ.update(environment)
    sys.path[:0] = [path for path in environment.get("PYTHONPATH", "").split(os.pathsep) if path]
    server = str(ROOT / "scripts" / "mcp_server.py")
    sys.argv = [server]
    runpy.run_path(server, run_name="__main__")


def main():
    try:
        python = select_python()
        if Path(python).resolve() != Path(sys.executable).resolve():
            command = [python, str(Path(__file__).resolve())]
            if os.name == "nt":
                return subprocess.call(command)
            os.execve(python, command, dict(os.environ))
        environment = prepare_environment(python, os.environ)
        run_server(environment)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("youtube-deep-analyzer startup failed: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
