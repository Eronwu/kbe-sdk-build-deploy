#!/usr/bin/env bash
set -euo pipefail
TOOL_DIR="$(cd "$(dirname "$0")" && pwd -P)"
exec python3 "$TOOL_DIR/kbe_deploy.py" "$@"
