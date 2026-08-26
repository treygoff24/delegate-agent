#!/usr/bin/env bash
# Canonical gate for delegate-agent. Run before calling anything done.
#
# Everything runs through `uv run`, never PATH, because PATH tooling is whatever
# the machine had for breakfast: this box carries ruff 0.16.4 on PATH while
# pyproject pins 0.15.15, and the two disagree about formatting.
#
# `ruff format --check` is here because its absence already cost us. On
# 2026-08-25 a change passed `ruff check` clean and landed unformatted, because
# lint and format are different questions and only one of them was being asked.
# A gate that asks one question reads like a gate that asked all of them.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

fail=0
err() { printf 'GATE FAIL: %s\n' "$*" >&2; fail=1; }

# --extra dev matters: plain `uv run` in a fresh worktree has no dev extras, so
# uv quietly resolves LATEST ruff/pytest from PyPI and the gate runs unpinned
# tooling while looking pinned (observed 2026-08-26: 0.16.4 in a worktree,
# 0.15.15 in the primary checkout, same commit).
run() { uv run --extra dev "$@"; }

pinned_ruff=$(grep -oE 'ruff==[0-9][0-9.]*' pyproject.toml | head -1 | cut -d= -f3)
actual_ruff=$(run ruff --version | awk '{print $2}')
printf 'ruff %s (pinned %s)\n' "$actual_ruff" "$pinned_ruff"
[ "$actual_ruff" = "$pinned_ruff" ] || err "ruff $actual_ruff does not match pyproject pin $pinned_ruff"

run ruff check src/ tests/ bin/ || err "ruff check"
run ruff format --check src/ tests/ bin/ || err "ruff format --check (run: uv run --extra dev ruff format src/ tests/ bin/)"
run pytest -q || err "pytest"

if [ "$fail" -eq 0 ]; then
  printf 'GATE PASS [ruff %s]\n' "$actual_ruff"
else
  printf 'GATE FAILED [ruff %s]\n' "$actual_ruff" >&2
  exit 1
fi
