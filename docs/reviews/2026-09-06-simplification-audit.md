# Delegate simplification audit

Delegate's best cleanup targets are duplicated command policy, repeated filesystem work, and duplicated workflow lifecycle code. Keep the stdlib-only runtime and the safety checks. Do not replace the current implementation with another framework.

Requested by Trey: a read-only assessment using three Sol reviewers at medium reasoning. Reviewed checkout: `f8602edc4d6671c4adc168535f787dda05646032`. I checked the cited mechanisms and independently reproduced the CLI and registry-lock measurements below. No production code was changed.

## 1. Make discovery small and give command policy one owner

The measured clean-config output sizes are:

| Command | Output bytes | Warm median elapsed |
| --- | ---: | ---: |
| `--help` | 11,849 | 96 ms |
| `--version` | 7 | 99 ms |
| `--json describe --summary` | 103,082 | 127 ms |
| `--json describe` | 129,197 | 139 ms |

The summary includes the full command catalog, including usage, arguments, options, and descriptions. It is about 80% of the full output. The code already has a compact overview projection; reuse it instead of maintaining three discovery levels. Preserve existing JSON consumers through an explicit migration, rather than silently removing fields.

There are also two hand-written manuals alongside the command registry: the overview command matrix and the 118-line `agent-help` prose block. Replace the matrix with one launch grammar, one call grammar, command groups, and focused-help pointers. Fold the unique agent warnings into the existing command specs. Keep `agent-help` temporarily as an alias, not a second implementation.

This is functional cleanup, too. I reproduced `--isolation worktree snapshot example` and `--pass-through snapshot example` being accepted and silently cleared. `--json agent-help` exits successfully but emits plain text. Help metadata and parser support checks currently disagree. Use the existing `CommandSpec` support metadata for both; fill its missing restrictions, then delete parallel command allowlists and branch-specific policy checks. Retain command-local meanings of otherwise global flags.

Evidence: `describe_payload.py:79-106,1280-1344,1610-1727`; `command_help.py:41-53,933-963,2359-2498`; `cli_parser.py:58-144,593-611,2192-2238`. All source paths here are under `src/delegate_agent/` unless otherwise stated.

Expected payoff: less code and documentation drift, much smaller agent discovery output, and immediate errors instead of ignored options. The overview/manual blocks alone are roughly 258 lines before their shorter replacement; that is an opportunity size, not a promised net diff.

## 2. Remove full-history maintenance from frequent operations

Three independent mechanisms make cost grow with historical run count:

- Every registry lock scans all run directories, even without pending recovery. Progress persistence acquires it every 10 lines or roughly 0.5 seconds of dirty output.
- Status, cancellation, worktree, and workflow commands run retention first. It walks the full run index. Inspecting one run can therefore trigger maintenance on every run.
- Run listing reads state and manifest for every candidate before selecting the requested rows, then reloads the selected states for log sizes.

Independent synthetic measurements of an empty registry lock, with valid run directories and no recovery records, were 0.91 ms at 100 runs, 8.45 ms at 1,000, and 24.38 ms at 3,000. This is avoidable serialized work, not a measurement of end-to-end model latency.

Start with the least risky deletion: remove automatic retention from the cancellation path and throttle opportunistic retention elsewhere, retaining explicit maintenance. Reuse the existing retention lock and a completion timestamp rather than introducing a maintenance service. Reuse loaded states during listing; load manifests only when selection requires their legacy fallback fields or when enriching selected rows. Preserve exact filtering and ordering.

Then remove all-history recovery from the progress hot path. A candidate design reconciles the affected run on reads/mutations and reserves full recovery for startup or repair. That changes the current guarantee that any later lock recovers every run, so it needs an explicit recovery contract before implementation. Do not simply delete the replay call. Keep crash recovery, cancel precedence, malformed-record quarantine, and pruning awareness.

Evidence: `run_registry.py:394-464,499-513`; `runner.py:63-64,777-838,2325-2332`; `cli.py:1862-1879`; `retention.py:131-158,256-294`; `run_status.py:193-205,317-356`.

## 3. Stop constructing an entire workflow runtime for a nested scope

Nested workflows create another `WorkflowState` over the same journal. Construction creates semaphores and replays the journal; the caller then replaces the new locks and semaphores with the parent's. It also passes through a long list of shared mutable fields. The source comments describe previous fields being lost when this copying missed one.

Keep one shared workflow state. Give nested execution only its script, arguments, namespace, and depth. Delete the repeated replay and the post-construction repairs. Keep scope-specific values isolated during parallel nested execution; mutating the parent's namespace temporarily is not safe.

Evidence: `workflows/runtime.py:689-707,2180-2229`.

This is the clearest small architecture refactor: fewer state owners, fewer chances to omit a field, and one fewer journal traversal per nested construction.

## 4. Give new agents and followups one lifecycle implementation

`agent()` and `followup()` separately implement key construction, cache lookup, child adoption, budget accounting, dry-run behavior, start/finish events, semaphore handling, and structured-result processing. The paths already differ in replay locking and tombstone handling. Similar code does not mean those differences are all wrong, but it means fixes must be checked twice.

Keep separate launch adapters for a new child and a native followup. Share the lifecycle around them. Extract existing repeated behavior; do not introduce a general task engine or a plugin architecture. Keep new-agent fallback chains separate from the requirement that a followup target be resumable.

Evidence: `workflows/runtime.py:2338-2587,2695-3096,3405-3673`.

Preserve budget accounting, replay keys, tombstones, structured null handling, persona identity, deadlines, and retry-worktree cleanup. Verify through behavior-focused replay/failure tests rather than keeping every private method as a compatibility interface.

## 5. Consolidate worktree inspection without weakening deletion safety

Prune, reap, remove, and completion retirement separately evaluate overlapping ownership, attachment, status, dirtiness, and merge conditions. Private parameters named `_merged_check_already_passed` and `_dirty_check_already_passed` pass knowledge between implementations.

Use one inspection result and shared safety predicates, with explicit policy differences for each command. Revalidate changing facts while holding the appropriate locks immediately before removal. Delete duplicate predicates and bypass plumbing, not the second check that prevents a race.

Evidence: `worktree_gc.py:99-180,539-715`; `worktree_remove.py:525-533,547-608`; `worktree_mgmt.py:314-380`.

This is a worthwhile maintenance refactor but a higher-risk implementation batch. Unknown ownership, active attachments, source containment, dirty content, and merge ambiguity must still fail closed. No worktree deletion is authorized by this audit.

## 6. Read each journal and approval file once per operation

Gate parking scans the journal for the maximum sequence, then scans again to find the matching gate. Finding the latest unapproved gate rereads `approval.json` for each gate. Operator event appends perform another sequence scan.

Consolidate the journal fold needed by each operation and load approvals once within its existing synchronization scope. Return the sequence and gate information together. Start without a persistent cache, index, new journal format, or migration of approvals into the journal.

Evidence: `workflows/runtime.py:952-989,1009-1019`; `workflows/commands.py:948-965,1186-1197`; `workflows/registry.py:128-151`.

Keep simulated-event exclusion, truncated-final-line tolerance, gate/result identity, append durability, and lock ordering. This is source-confirmed repeated I/O; end-to-end speedup was not benchmarked.

## 7. Remove test compatibility from the production entry point

An AST check found 45 imports unused inside `cli.py`, many explicitly retained for tests or back-compat. They are not proven dead exports. Tests repeatedly load or reload the CLI to reach parser and request-building functions that have their own modules. Meanwhile, `request_models.py` describes itself as a dependency leaf but imports command implementations, including workflow commands.

Move internal tests to imports from the owning modules. Check external consumers before removing undocumented exports. Keep command data definitions free of execution imports, then import execution code only when dispatch needs it. Use one test-loading approach where possible, while retaining subprocess tests of the actual CLI entry point.

Evidence: `cli.py:5-162`; `request_models.py:1-30`; `tests/delegate_commands_test_base.py:19-20`; `tests/execution_test_base.py:19-26`; `tests/test_delegate_parser.py:28-37`.

An import profile showed 78 Delegate modules loaded for `--help`, with about 78 ms cumulative time in the CLI import in that sample. The measured startup cost is real; the achievable reduction has not been measured. Deleting exports alone will not remove imports still required by dispatch.

## Changes requiring a compatibility decision

Droid's positional model alias has its own roughly 150-line parsing branch, while other engines use `--model`. Standardize the grammar after a deprecation window; retain alias provenance in stored records. Evidence: `cli_parser.py:1298-1448,1930-1948`; `request_build.py:1193-1228,2772-2789`.

Legacy workflow keys, pinless resumes, and old attempt formats are deletion candidates only after inventorying still-resumable workflows and setting a support boundary. They are tested compatibility behavior, not proven dead code. Evidence: `workflows/commands.py:208-240,290-342,443-488`; `workflow_pinning.py:540-557,614-714`.

Collapsing mutable state and snapshot into one record could eventually remove double writes and reconciliation code. It also requires a storage migration and external-reader audit. Defer it until the smaller changes establish whether that complexity is justified.

## Recommended order and verification

Start with compact help/discovery and shared flag policy, retention/read-path cleanup, and nested workflow state. Next consolidate child lifecycle and journal reads. Refactor destructive worktree policy as a separate reviewed batch. Treat CLI/storage compatibility removals as explicit product decisions.

Keep the goal measurable: net production-code reduction, fewer independent policy implementations, bounded discovery output, and lower status/progress cost as run history grows. Do not claim success by deleting tests, historical receipts, providers without usage evidence, or security checks. The source has 57,428 Python lines and the tests 79,567; neither count alone establishes slop. Runtime dependencies are already zero.

Validation during this audit: repo-local `python3 bin/delegate.py --json describe`; isolated help/version/describe probes; direct parser probes; synthetic registry-lock benchmark; and `python3 -m pytest -q tests/test_delegate_parser.py tests/test_delegate_help_cli.py tests/test_command_help.py`, yielding 272 passed and 1,259 subtests passed. CLI timing used six fresh subprocesses per command, discarded the first, and took the median of the remaining five, with a temporary HOME and restricted PATH. Registry timing used temporary directories under `/var/tmp` and six warm samples after one initial sample. These are local synthetic measurements, not installed-runtime or live-concurrency benchmarks. The full acceptance gate was not run because production code was unchanged.
