"""Run both host launch checks with the Python environment prepared by CI."""
from pathlib import Path
import os
import sysconfig
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
script = root / "plugins" / "youtube-deep-analyzer" / "scripts" / "smoke_test_mcp.py"
environment = dict(os.environ)
environment["YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME"] = sysconfig.get_path("purelib")
environment["YOUTUBE_DEEP_ANALYZER_AUTO_UPDATE"] = "0"
for host in ("codex", "claude"):
    subprocess.run([sys.executable, str(script), "--host", host], env=environment, check=True)
