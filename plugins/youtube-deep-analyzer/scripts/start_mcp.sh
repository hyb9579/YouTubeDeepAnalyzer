#!/bin/sh
# Compatibility entry point. New installations invoke launch_mcp.py directly.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" "$SCRIPT_DIR/launch_mcp.py" "$@"
  fi
done
echo "youtube-deep-analyzer requires Python 3.11 or newer." >&2
exit 1
