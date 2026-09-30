# Test-speed results, 2026-09-29

The full-suite median fell from 357.755 seconds to 235.908 seconds (34.06%
less time) at a fixed four-worker, two-core Linux budget. Five interleaved
measured pairs all passed. The paired 95% interval is 3.05-35.31% less time;
the slower 348.869-second candidate sample is included.

The expanded fast gate passed 831 tests and 1,902 subtests, plus compile,
lint, and format checks, in 8.69 seconds. Fast checks are partial coverage;
the full gate remains required before merge.

## Using the gates

```bash
scripts/gate.sh --fast                 # quick tests and all static checks
scripts/gate.sh --fast -- tests/test_config.py  # add affected tests
scripts/gate.sh                        # full regression and static checks
tests/acceptance.sh                    # same gate without requiring uv
```

Both entry points use `scripts/test.py`. It schedules individual tests with
work stealing, uses four workers on Linux and two on macOS, lowers priority,
and locks the common Git directory across linked worktrees. A second run
exits 75. Linux with `testrun` caps the entire test scope at two CPU cores;
macOS has worker bounds and low priority, but no hard CPU quota. Overrides
are explicit; automatic worker counts are refused. Full gates refuse filters
and clear ambient `PYTEST_ADDOPTS`. Fast success is `FAST CHECKS PASS`; full
success is `GATE PASS`.

## Measurement and retained changes

The statistical control already uses a bounded four-worker runner with file
scheduling. This comparison measures the additional scheduling and fixture
improvements; the previously reported 20-minute serial gate is not its control.
Each worktree owns its environment and imports its own source. Versions match:
Python 3.14.7, pytest 9.1.1, xdist 3.8.0, Ruff 0.15.15.

The committed `bench/tests.py` workload, measuring commands, runner, and frozen
collection were locked before tuning and never changed or reapproved. The
collection guard retains all 4,708 original node IDs. Ten interleaved pairs
tuned four existing cancellation and workflow-concurrency cases; five pairs,
with warmups, confirmed the final changes on the full suite.

| Workload/change | Before | After | Less time | Paired 95% interval |
| --- | ---: | ---: | ---: | --- |
| Four-case loop: individual-test scheduling | 11.244 s | 6.256 s | 44.55% | 44.18-44.91% |
| Four-case loop: fixture waits | 6.280 s | 5.427 s | 13.38% | 12.51-14.39% |
| Held-out full suite | 357.755 s | 235.908 s | 34.06% | 3.05-35.31% |

Cancellation fixtures use a short grace period for children they own and reap.
Resume fixtures retain their interrupted initial launch, then resume fake
agents without an artificial ten-second delay. Production timers and the
existing signal, budget, and journal assertions are unchanged.

The known image-scan fixture import error was repaired and proved red/green
before the benchmark started. Two confirmation attempts then exposed fixture
races: recursive deletion while a live supervisor writes its directory, and
an observer that could miss a 0.3-second pending-tool window. Both attempts
were stopped and retained. The deletion fixtures now await a running child
and remove the directory entry atomically; the pending-tool fixtures use
acknowledgments to keep the child quiet until live state is observed. Detached
files are cleaned after producer reaping, and observer threads stop and join.
The identical correctness repairs are in both control and candidate, so no
gain from them is credited to the performance changes. Fresh confirmation
starts after the repairs.

## Evidence and limits

- A planted four-second regression measured 35.01% slower and was removed.
  Unchanged versus unchanged was within noise, with a 0.66% interval half-width
  below the 3% minimum effect.
- Every measured full-suite repetition passed 4,691 tests with 17 skips.
  Subtests rose from 3,858 to 3,861 through added runner checks. JUnit totals
  include those subtests: 8,566 to 8,569. No original collected test was removed.
- Seven deliberate runner/gate breakages triggered their protection checks.
  Disabling missing-state cancellation and pending-tool clearing also made
  the repaired real-process tests fail. Source was restored byte-for-byte.
- Four related live-state cases passed ten repeated selections; the nearby
  family passed 58 tests and 20 subtests. A real second run from another
  worktree was refused while the owner completed.
- The recorded waited-child CPU median changed from 375.984 to 384.492 seconds
  (+2.26%). `RUSAGE_CHILDREN` can exclude reparented supervisors; this is a
  partial counter, not proof of reduced total CPU work. The verified cgroup
  quota covers all descendants and limits the CPU budget independently.
- The repo-local `python3 bin/delegate.py --json describe` check passed.
  Native macOS execution and the other supported Python versions were not run
  locally. CI uses the shared runner with an explicit two-worker count.

The exact final main gate passed all four checks in 234.83 seconds: 4,691
tests passed, 17 skipped, and 3,861 subtests passed. Pytest took 233.15 seconds;
Ruff reported all checks passed and 325 files already formatted.

The measured control is `d65fbb4` (starting code plus shared fixture repairs);
the measured candidate is `6df148d`. The later `c1f6919` change adds only
acceptance-toolchain tests to the fast selection. Raw state, frozen collection,
and logs are retained outside tracked source; this report is the portable summary.

## Remaining opportunities

Real deadline, stall, escalation, and concurrency timing checks were retained.
Ownership checks and process cleanup were preserved. Lazy CLI imports and the
separately tracked OMP flood-mutation timeout remain separate maintainer work;
neither is a cost in these passing workloads. This session did not promote an
installed CLI runtime or publish to GitHub.
