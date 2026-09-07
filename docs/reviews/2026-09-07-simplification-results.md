# Simplification results

The feature branch implements the simplification audit against baseline
`f8602edc4d6671c4adc168535f787dda05646032`. This is a source change, not an
installed-runtime upgrade or a release.

## What changed

Command specs now drive compact help, discovery and shared option restrictions.
Full discovery is opt-in. Droid uses the same `--model` grammar as other engines.
Parsed commands carry one typed payload; tests import the owning modules instead
of relying on CLI and worktree re-exports.

New runs persist one bounded mutable `state.json`, with an immutable manifest
and computed public snapshot output. Finalization recovery targets the affected
run; public reads can observe pending completion without taking mutation locks.
Cancellation retains its outcome while accepting late capture metadata.
Opportunistic retention is throttled and removed from cancellation.

Nested workflows share state and carry thread-local scope through parallel work.
New-agent and native-followup calls share their lifecycle. Journal operations
reuse a single fold. Legacy workflow formats are deliberately rejected rather
than migrated; current result-bound approval and pinned-runtime checks remain.

Worktree inspection and cleanup share safety policy. Cleanup rechecks live
ownership, attachments, dirty content and merge evidence under the registry lock.
Ignored retirement paths are filtered before the display cap, and renames check
both endpoints. Legacy raw records remain ownership evidence, not authority
laundered through the public snapshot view.

## Measurements

These local synthetic measurements compare the baseline with the feature code.
They do not measure installed-runtime behavior or provider latency. CLI values
are medians of five fresh subprocesses after a warm-up, with a temporary HOME
and restricted PATH. Registry values are warm medians on temporary directories.

| Measurement | Baseline | Feature |
| --- | ---: | ---: |
| Help output | 11,849 bytes | 1,548 bytes |
| Default JSON discovery | 130,255 bytes | 9,645 bytes |
| Summary JSON discovery | 103,308 bytes | 11,496 bytes |
| Default discovery elapsed | 154.01 ms | 102.16 ms |
| Help elapsed | 98.10 ms | 96.11 ms |
| Delegate modules imported by CLI | 78 | 64 |
| Empty registry lock, 100 runs | 1.045 ms | 0.040 ms |
| Empty registry lock, 1,000 runs | 8.408 ms | 0.037 ms |
| Empty registry lock, 3,000 runs | 28.695 ms | 0.036 ms |
| Listing 20 of 1,000 terminal runs | 125.29 ms | 68.55 ms |
| Listing record bytes read | 31,022,160 | 608,360 |
| Listing JSON record reads | 2,020 | 40 |
| Serialized listing index | 74,903 bytes | 289,903 bytes |

The listing fixture stores 30 KB of assistant text per terminal run. Record-read
figures exclude loading the index, which is supplied to the measured function;
the table reports its increased size separately. The feature uses a projection
in that existing index only when canonical file identity still matches and no
finalization journal is pending. Both versions return 20 rows and total 1,000.

## Review and verification

The implementation received an independent plan review, three-family integrated
code review, and a fresh fix review. Worktree dirty-path truncation, cancellation
metadata, nested thread scope, unsupported workflow flags, raw ownership evidence
and pin-resolver enforcement received regression coverage after review.

A fresh repo-local live child completed successfully with the requested marker.
Its canonical record was private (mode 0600), terminal and readable without any
snapshot file. This verifies the source launch/capture path, not all providers,
installation, or behavior of an old installed mutator against a new registry.

Final validation: the pinned parallel gate passed **3,046 tests**, with **15
skips** and **2,241 passing subtests**. The command was
`uv run --frozen --extra dev pytest -q -n 8 --dist loadfile` (244.72 seconds).
After the final help-text corrections, the native focused check passed another
276 tests and 1,188 subtests. `python3 -m compileall -q src tests bin`,
`ruff check .`, `ruff format --check .`, and `git diff --check` all passed.

The final three-family scoped re-check found no blocker or major. Its minor
findings were addressed: actual flag spelling is tracked during parsing,
non-launch help is checked against exact allowed globals, positive option
propagation is tested, redundant isolation branches are removed, and record
size tests cover below, at and above the limit. The complete source change is
**301 lines smaller** than the baseline; the test suite is larger. No runtime
dependency was added.

Old workflow replay formats and positional Droid model syntax are intentional
breaking changes. Do not point old installed mutators at new-format run records.
No installed runtime, release channel, or GitHub remote was changed. Existing
worktrees were retained, and pre-existing Beads export changes were not staged.
