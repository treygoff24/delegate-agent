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

uv run ruff --version
uv run ruff check src/ tests/ bin/ || err "ruff check"
uv run ruff format --check src/ tests/ bin/ || err "ruff format --check (run: uv run ruff format src/ tests/ bin/)"
uv run pytest -q || err "pytest"

if [ "$fail" -eq 0 ]; then
  printf 'GATE PASS [ruff %s]\n' "$(uv run ruff --version | awk '{print $2}')"
else
  printf 'GATE FAILED [ruff %s]\n' "$(uv run ruff --version | awk '{print $2}')" >&2
  exit 1
fi
