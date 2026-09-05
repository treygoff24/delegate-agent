#!/usr/bin/env bash
# Receipt that the pytest-xdist accelerator and the unittest gate agree on
# executed and skipped counts (not a proof of identical collection): compares "Ran N tests (skipped=S)" from unittest discover against
# "P passed, S skipped" from pytest -n. Any drift (a test pytest never
# collects, a skip that only one runner sees) fails here instead of hiding
# behind a green parallel run.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

log_dir=$(mktemp -d "${TMPDIR:-/var/tmp}/delegate-parity.XXXXXX")
unit_log="$log_dir/unittest.log"
py_log="$log_dir/pytest.log"
printf 'parity logs: %s\n' "$log_dir"

fail() {
  printf '%s (complete log: %s)\n' "$1" "$2" >&2
  # Tail is presentation only. Parsing always consumes the complete saved log.
  tail -40 "$2" >&2
  exit "${3:-1}"
}

if python3 -m unittest discover -s tests -t . >"$unit_log" 2>&1; then
  :
else
  code=$?
  fail "unittest gate failed with exit $code" "$unit_log" "$code"
fi
# Buffered stdout can appear after Ran/OK. Associate the final status with its
# preceding count, not with a fixed number of trailing lines.
unit_counts=$(awk '
  { gsub(/\033\[[0-9;]*m/, "") }
  /^Ran [0-9]+ tests? in / { ran=$2; status="" }
  /^OK([[:space:]]|$)/ { if (ran != "") status=$0 }
  /^FAILED([[:space:]]|$)/ { status="" }
  END {
    if (ran == "" || status == "") exit 1
    skipped=0
    if (match(status, /skipped=[0-9]+/)) skipped=substr(status, RSTART+8, RLENGTH-8)
    printf "%s %s\n", ran, skipped
  }
' "$unit_log") || fail "could not parse a successful unittest summary" "$unit_log"
read -r unit_ran unit_skipped <<< "$unit_counts"

if uv run --extra dev pytest -n "${PARITY_WORKERS:-8}" --dist loadfile -q -p no:cacheprovider tests >"$py_log" 2>&1; then
  :
else
  code=$?
  fail "pytest run failed with exit $code" "$py_log" "$code"
fi
# Ignore later diagnostics and subtest counts; omitted skip/pass tokens mean
# zero, not a grep failure under pipefail (including an all-skipped suite).
py_counts=$(awk '
  { gsub(/\033\[[0-9;]*m/, "") }
  /[0-9]+ (passed|skipped|failed|errors?)/ && / in [0-9.]+s/ { summary=$0 }
  END {
    if (summary == "" || summary ~ /[0-9]+ (failed|errors?)/) exit 1
    passed=0; skipped=0; found=0
    if (match(summary, /[0-9]+ passed/)) {
      split(substr(summary, RSTART, RLENGTH), token, " "); passed=token[1]; found=1
    }
    if (match(summary, /[0-9]+ skipped/)) {
      split(substr(summary, RSTART, RLENGTH), token, " "); skipped=token[1]; found=1
    }
    if (!found) exit 1
    printf "%s %s\n", passed, skipped
  }
' "$py_log") || fail "could not parse a successful pytest summary" "$py_log"
read -r py_passed py_skipped <<< "$py_counts"
py_total=$((py_passed + py_skipped))
printf 'unittest: ran=%s skipped=%s | pytest: passed=%s skipped=%s total=%s\n' \
  "$unit_ran" "$unit_skipped" "$py_passed" "$py_skipped" "$py_total"
[ "$unit_ran" -eq "$py_total" ] && [ "$unit_skipped" -eq "$py_skipped" ] \
  || { echo "PARITY DRIFT: runners disagree on executed/skipped counts" >&2; exit 1; }
echo "parity ok"
