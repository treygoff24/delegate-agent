#!/usr/bin/env bash
# Pinned tooling, shared resource limits, and all four repository checks.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
exec uv run --extra dev python scripts/test.py --gate "$@"
