#!/bin/bash
# Shared initialization does not require a personal WeChat account or key.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_PY="${WD_PYTHON:-$(command -v python3)}"
"$TASK_PY" "$TASK_ROOT/setup/bootstrap_runtime.py"
