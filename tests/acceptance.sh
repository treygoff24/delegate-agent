#!/bin/sh
# Canonical repository gate, runnable from any checkout root. The compiled-plan
# close verifier executes this; it must stay byte-for-byte the CI contract in
# AGENTS.md (unittest discovery, compileall, ruff check, ruff format).
set -eu
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -t . 2>&1
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
echo "acceptance: OK"
