#!/bin/sh
# Canonical repository gate, runnable from any checkout root. The compiled-plan
# close verifier executes this; it preserves the four CI gates documented in
# AGENTS.md (unittest discovery, compileall, ruff check, ruff format).
set -eu
cd "$(dirname "$0")/.."
expected_ruff=$(python3 -c 'import pathlib, tomllib; p = tomllib.loads(pathlib.Path("pyproject.toml").read_text()); print(next(x.removeprefix("ruff==") for x in p["project"]["optional-dependencies"]["dev"] if x.startswith("ruff==")))')
common_ruff=
if command -v git >/dev/null 2>&1 && [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" = true ]; then
    common_root=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)
    common_ruff="$common_root/.venv/bin/ruff"
fi
ruff_bin=
for candidate in "$PWD/.venv/bin/ruff" "$common_ruff" "$(command -v ruff || true)"; do
    if [ -x "$candidate" ] && [ "$("$candidate" --version)" = "ruff $expected_ruff" ]; then
        ruff_bin=$candidate
        break
    fi
done
if [ -z "$ruff_bin" ]; then
    printf 'acceptance: requires Ruff %s from the project dev extra; run uv sync --extra dev or install .[dev] in a virtualenv.\n' "$expected_ruff" >&2
    exit 1
fi
python3 --version
printf 'python: %s\nruff: %s\n' "$(command -v python3)" "$ruff_bin"
"$ruff_bin" --version
python3 -m unittest discover -s tests -t . 2>&1
python3 -m compileall -q src tests bin
"$ruff_bin" check .
"$ruff_bin" format --check .
echo "acceptance: OK"
