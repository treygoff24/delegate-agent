# 2026-09-14 — stale cluster fix, supervisor signal relay hardening, 0.31.0 release

Beads: `dlg-m5w` (P1, workflow resume after supervisor death left the prior attempt's children `stale`; replay then failed thunks with "already terminal (stale)"), `dlg-cbz` (P3, pinned continuity passed silently when the harness reported no served model). Evidence: atlas `wf_eb25a11a985f`, 28 resumes with no kill, 10 orphaned `tail -F` watchers (killed by hand at the start of the run).

## What shipped (commits 4e6dd3c..2915e00, released as 0.31.0)

- Resume seals the prior attempt's in-flight children as `cancelled` (`attempt_superseded` with `supervisorLost`), `delegate cancel` seals a `dead_pid` stale row, `workflow kill` reconstructs its `cancelled` report from the whole group, and the supervisor handles `SIGTERM`/`SIGHUP` through a wakeup-pipe relay thread that journals `supervisor_signalled` and requests cooperative cancellation.
- Relay hardening (Astra rounds 5–12): drain generations, `signalDrainIncomplete` on the terminal status and in `wait`/`watch`, a fence taken only when no delivery is in flight, a structural release guard on the workflow lock handle so every exit from the lock context passes through the relay, atomic admission, the dedicated supervisor process ending itself while it still holds the flock, transactional relay arming with the read end owned by whichever of the reader and the rollback claims it first.
- `models` marks a missing binary; a pinned run whose harness reported no served model carries `pinned_continuity_unverified`.

## Rulings (Claude, announced to Trey in-session)

- Astra rounds 9–12 reviewed relay *arming* against asynchronous exceptions landing on single bytecodes. Trey asked whether this was overengineering (2026-09-14); the answer was yes for rounds 9–12, and the loop was ended after round 12 with no further review. The residue (an asynchronous exception on the bytecode after `os.pipe()` or between `set_wakeup_fd` taking effect and the store of its return value; rollback's own interruptibility) is stated in the `start()` docstring and accepted: it is bounded to a process already unwinding and lock ownership never depended on arming.
- Round 12's one finding (test nondeterminism) was fixed as a test-only commit (`2915e00`) without another review.
- Deferred, nonblocking, from Astra: the gate-preservation branch's aggregate `cancelled` list; summarized multi-child refusal details; the drain-before-publish ordering has no binding test (documented).

## Verification record

- Gates 9–12 green on 4fd0bc9, 60c8f5e, 3e70739, 27174af (3333–3338 passed, 15 skipped, ~2560 subtests, ruff 0.15.15). Gate 7 on e7a1acc failed one test because a subprocess imported `runtime.py` mid-mutation; rule adopted: never edit the checkout while a gate runs. Gate 8 was stopped as moot.
- Every new assertion red-proofed by mutation (M1–M34 across the rounds); the tree was restored and re-run green after each.
- Commit-body corrections: `e7a1acc` says "269/269" (actual 230 tests + 39 subtests); `4fd0bc9` says "274 tests" (actual 271 tests + 48 subtests). Neither can be amended.
- Live dogfood on the installed runtime at 27174af (`~/.delegate/releases/27174af5269284df-1428ff3827b708eb`, `promotionMatchesRuntime: true`): `models` marks `opencode`/`kimi` missing; a pinned `codex call` carries the warning; SIGKILL of a whole workflow tree then `--resume` wrote `attempt_superseded` (`supervisorLost: true`), sealed the orphan as `cancelled`, relaunched, no `thunk_failed`; SIGTERM at the supervisor produced `failed`/`signal:SIGTERM`, `supervisor_signalled`, the child cancelled, no `signalDrainIncomplete`, supervisor exited; `workflow kill` clean; `delegate cancel` sealed all 11 real atlas `dead_pid` rows in `~/Code/atlas/.worktrees/plan-atlas-v1` (0 stale remain).
- Not verified live: the `signalDrainIncomplete` path itself (needs a relay stuck inside a journal write), macOS.

## Release checks (publishing checklist, run on 13e4e77)

- compileall, `git diff --check`, TruffleHog verified-only on history and on the archived tree (0 verified, 0 unverified), the Lob-key grep, the private-path scan, sdist+wheel build with `twine check`, and the wheel smoke (`--version` 0.31.0, `describe`, seven `dry-run` engines) all pass.
- gitleaks 8.30.1 on the raw Forgejo history reports 207 findings: 196 in `docs/receipts/**` paths that git-filter-repo strips from every published commit, and 11 in test fixtures that ship — `tests/test_snapshot_redaction.py` builds dummy PEM blocks by concatenation (`MIIEpAIBAAKCAQEA`, `ABCDEFSECRETBODY`) to test redaction, and older revisions of `tests/test_workflow_commands.py` carry a sha256 agent-key hex named `key_plain`. Justified as fixtures; none is a credential. gitleaks 8.16.0 (dpkg) flags a different fixture set (4 test files), same class.
- Gate 13 on the release tree (`13e4e77`) failed 7 tests + 2 teardown errors, all timing-shaped ("timed out waiting for N child runs", "Directory not empty" teardown, a `timeout` wrapper firing, and `test_terminal_none_agent_result_replays_without_respawn`, whose own comment says a loaded box blows its window). The run took 12:32 against the usual 8:35 on a box at load average 16–18 from other sessions; all seven pass on the idle re-run in 29 s. The release tree's gate is re-run as gate 14 once the load subsides; the runtime code it covers is `27174af` (gate 12 green) plus a test-only commit and the version constant.
