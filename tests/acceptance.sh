#!/bin/sh
# Canonical repository gate, runnable from any checkout root. The compiled-plan
# close verifier executes this; it preserves the four CI gates documented in
# AGENTS.md (pytest, compileall, ruff check, ruff format).
set -eu
cd "$(dirname "$0")/.."
expected_ruff=$(python3 -c 'import pathlib, tomllib; p = tomllib.loads(pathlib.Path("pyproject.toml").read_text()); print(next(x.removeprefix("ruff==") for x in p["project"]["optional-dependencies"]["dev"] if x.startswith("ruff==")))')
common_venv=
if command -v git >/dev/null 2>&1 && [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" = true ]; then
    common_root=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)
    common_venv="$common_root/.venv/bin"
fi
ruff_bin=
for candidate in "$PWD/.venv/bin/ruff" "${common_venv:+$common_venv/ruff}" "$(command -v ruff || true)"; do
    if [ -x "$candidate" ] && [ "$("$candidate" --version)" = "ruff $expected_ruff" ]; then
        ruff_bin=$candidate
        break
    fi
done
if [ -z "$ruff_bin" ]; then
    printf 'acceptance: requires Ruff %s from the project dev extra; run uv sync --extra dev or install .[dev] in a virtualenv.\n' "$expected_ruff" >&2
    exit 1
fi
pytest_bin=
for candidate in "$PWD/.venv/bin/pytest" "${common_venv:+$common_venv/pytest}" "$(command -v pytest || true)"; do
    if [ -x "$candidate" ]; then
        pytest_bin=$candidate
        break
    fi
done
if [ -z "$pytest_bin" ]; then
    printf 'acceptance: requires pytest from the project dev extra; run uv sync --extra dev or install .[dev] in a virtualenv.\n' >&2
    exit 1
fi
python3 --version
printf 'python: %s\npytest: %s\nruff: %s\n' "$(command -v python3)" "$pytest_bin" "$ruff_bin"
"$ruff_bin" --version
"$pytest_bin" -q
python3 -m compileall -q src tests bin
"$ruff_bin" check .
"$ruff_bin" format --check .
echo "acceptance: OK"
