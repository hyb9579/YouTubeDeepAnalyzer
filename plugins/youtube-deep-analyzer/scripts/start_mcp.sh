#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    BASE_RUNTIME_PATH=$("$candidate" "$SCRIPT_DIR/bootstrap_runtime.py" --print-path)
    export YOUTUBE_DEEP_ANALYZER_BASE_RUNTIME="$BASE_RUNTIME_PATH"
    export PYTHONPATH="$BASE_RUNTIME_PATH${PYTHONPATH:+:$PYTHONPATH}"
    RUNTIME_PATH=$("$candidate" "$SCRIPT_DIR/runtime_update.py" --print-path)
    if [ -n "$RUNTIME_PATH" ]; then
      export YOUTUBE_DEEP_ANALYZER_RUNTIME_PATH="$RUNTIME_PATH"
      export PYTHONPATH="$RUNTIME_PATH:$PYTHONPATH"
    fi
    exec "$candidate" "$SCRIPT_DIR/mcp_server.py"
  fi
done

echo "youtube-deep-analyzer requires Python 3.10 or newer." >&2
exit 1
