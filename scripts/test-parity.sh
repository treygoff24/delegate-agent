#!/usr/bin/env bash
# Receipt that the pytest-xdist accelerator executes exactly the unittest gate:
# compares "Ran N tests (skipped=S)" from unittest discover against
# "P passed, S skipped" from pytest -n. Any drift (a test pytest never
# collects, a skip that only one runner sees) fails here instead of hiding
# behind a green parallel run.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

unit_out=$(python3 -m unittest discover -s tests -t . 2>&1 | tail -3)
unit_ran=$(printf '%s\n' "$unit_out" | sed -n 's/^Ran \([0-9]*\) tests.*/\1/p')
unit_skipped=$(printf '%s\n' "$unit_out" | sed -n 's/.*skipped=\([0-9]*\).*/\1/p')
unit_skipped=${unit_skipped:-0}
printf '%s\n' "$unit_out" | grep -q '^OK' || { printf 'unittest gate is red:\n%s\n' "$unit_out" >&2; exit 1; }

py_out=$(uv run --extra dev pytest -n "${PARITY_WORKERS:-8}" --dist loadfile -q -p no:cacheprovider tests 2>&1 | tail -1)
py_passed=$(printf '%s\n' "$py_out" | sed -n 's/.*[^0-9]\([0-9]*\) passed.*/\1/p')
py_skipped=$(printf '%s\n' "$py_out" | sed -n 's/.*[^0-9]\([0-9]*\) skipped.*/\1/p')
py_skipped=${py_skipped:-0}
printf '%s\n' "$py_out" | grep -Eq '[0-9]+ passed' || { printf 'pytest run is red: %s\n' "$py_out" >&2; exit 1; }
printf '%s\n' "$py_out" | grep -Eq 'failed|error' && { printf 'pytest run is red: %s\n' "$py_out" >&2; exit 1; }

[ -n "$unit_ran" ] && [ -n "$py_passed" ] || { echo "could not parse runner output" >&2; exit 1; }
py_total=$((py_passed + py_skipped))
printf 'unittest: ran=%s skipped=%s | pytest: passed=%s skipped=%s total=%s\n' \
  "$unit_ran" "$unit_skipped" "$py_passed" "$py_skipped" "$py_total"
[ "$unit_ran" -eq "$py_total" ] && [ "$unit_skipped" -eq "$py_skipped" ] \
  || { echo "PARITY DRIFT: runners disagree on executed/skipped counts" >&2; exit 1; }
echo "parity ok"
