#!/bin/sh
# Same gate without requiring uv; prefer the checkout's dev environment.
set -eu
cd "$(dirname "$0")/.."
python_bin=python3
if [ -x .venv/bin/python ]; then
    python_bin=.venv/bin/python
elif command -v git >/dev/null 2>&1 && [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" = true ]; then
    common_root=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)
    if [ -x "$common_root/.venv/bin/python" ]; then
        python_bin="$common_root/.venv/bin/python"
    fi
fi
exec "$python_bin" scripts/test.py --gate "$@"
