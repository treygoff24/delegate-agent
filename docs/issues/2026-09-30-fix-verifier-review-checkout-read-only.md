# Fix verifier cannot run tests: its review checkout is read-only (2026-09-30)

Found during the Atlas Build 1 slice 1 run (workflow `wf_b75bd9cba4c1`, spine `~/Code/atlas/.worktrees/plan-atlas-build1-s1`, delegate 0.31.0, compiled engine `5134e210…` from writing-plans). Papercut `pc2_860817724a5ebc30`.

## Resolution (2026-09-29)

Trey approved narrowing the default work write guard to credentials, profile
state, and the installed Delegate runtime. `isolation.writeGuard.codeRoot`
now defaults to `null`, so the guard leaves sibling checkouts writable unless
the operator explicitly protects a code root or adds a checkout to `add`.
The protect-list prompt no longer tells the child to work only in its initial
checkout. No workflow writable-path API or writing-plans change is needed for
this default-policy failure.

The archived failed run used an enforced **bubblewrap** write guard with
Codex's own sandbox bypassed, rather than the Codex native sandbox described
below. Its manifest protected `~/Code` and reopened the spine but omitted the
review checkout. A read-only mount probe reproduced that distinction. The
native Codex sandbox, when enabled separately, still enforces its own writable
roots; this change does not remove those restrictions.

Existing workflows pinned to the earlier Delegate runtime retain the earlier
behavior until their runtime is repinned. An explicit protected `codeRoot`
continues to require deliberate writable exceptions. The original diagnosis
and broader API proposal below are retained as incident history.

Validation: the real CLI regression failed on the earlier default because the
sibling write was denied, while credential and runtime protection still worked.
It passes with the narrower default. The 130 focused guard tests and the full
gate (4,691 passed, 17 skipped, plus compile, lint and format checks) passed.
Explicit code-root protection and its writable override remain covered.

## Symptom

Every `verify_fixes` stage whose declared rows write to the checkout comes back with every adopted finding under `unrun`. The item parks at `verify-fixes-unrun`, and the park pauses the entire workflow ("gate reached in item B1.00; unwinding workers for the pause"). A retry gives the same result: the verifier pool has one seat and the cause is structural. So in any plan where fixes need tests to verify them, every fix round stops the build until a human intervenes.

The verifier's report (codex, gpt-6-luna, journal seq 63), verbatim excerpt:

> The focused crash check and workflow row could not start because scripts/test-postgres.sh was denied writing .pg-url (read-only filesystem). Core and alternate-core tests, and the web test after codegen, could not start because Vite could not create node_modules/.vite-temp. … Typecheck exited 1 because Next could not write apps/web/next-env.d.ts.

The same rows passed in a writable detached worktree at the same commit (core 1576 tests, alt 300, workflows 73, web 61, crash 3, typecheck ok). The review checkout itself is `drwxrwxr-x` and owned by the agent user, so the denial comes from the sandbox, not the filesystem.

## Cause

Two halves, in two repos:

1. **writing-plans engine** (`src/planc/engine/core.py`, `_fix_verifier_spec`, around line 1589). This sets `isolation = "none"`, so the child's cwd and execution root is the **spine**. But `verify_fixes_prompt` (around line 1633) tells the verifier to work in `task.review_workspace`. That is a **sibling** worktree (`.worktrees/plan-<name>-review-<task>`), outside the spine.
2. **delegate** (this repo). For codex in work mode with the native `workspace-write` sandbox, the writable roots are the cwd, the git common dir, run scratch and temp, home caches, and `isolation.writeGuard.writable` / `--writable` (`write_guard.native_writable_roots`). The sibling review worktree is none of these, so writes there fail with EROFS. On the bwrap backend (non-codex engines) the write guard's protect-list treats sibling checkouts as protected too, so switching the verifier to claude would likely fail the same way. That is unverified.

The workflow-script `agent()` API (`workflows/runtime.py`, `def agent(` around line 3781) exposes no `cwd` or `writable` parameter. So the engine has no way to grant the verifier its review workspace.

## Why it cannot be patched in a live run

- Adding the review paths to `isolation.writeGuard.writable` in `config.work.local.json` does not reach the children. Workflow children load the attempt config (`workflow_attempts`), which re-reads only `OPS_KEYS`. `isolation` is frozen in the pin.
- `workflow resume/approve --repin` replaces only the runtime. Its help says frozen config is kept.
- `plan-unpark <state> <item> done` refuses because the park's gate payload has `"workflow": null` ("parked on Delegate gate … with no recorded workflow id; run `delegate workflow approve …`"). That's a separate small defect: the engine could record the workflow id on park gates.

The live workaround: the coordinator runs the rows by hand in a detached worktree, red-proofs the fix, answers the gate with `done`, and acknowledges at wave close. It works, but it costs a full workflow pause and a manual round per fix.

## Suggested fix (for the implementer to judge)

- **delegate:** add a `writable: list[str] | None` parameter to workflow `agent()`. It should flow to the child exactly as `--writable PATH` does, get recorded in the manifest, and apply in both the codex-native and bwrap backends. Alternatively add a `cwd` parameter that runs an `isolation="none"` child in a named existing checkout. `writable` is the narrower change, and it keeps the engine's cwd semantics.
- **writing-plans:** have `_fix_verifier_spec` / the agent call pass the task's review workspace (or all of `_review_workspaces(item)`) as writable. Also add a plan dry-run or preflight probe that the verifier seat can create a file in its review workspace, so this fails at launch instead of at the first fix round.
- Consider whether the reviewer stages should stay read-only by design. They currently only read the review workspace, and granting them writes is not needed.

## Acceptance

- A red test in delegate: a workflow `agent()` child with `isolation="none"` and `writable=[<sibling dir>]` can write into the sibling under the codex-native sandbox, and cannot without it. Watch the test fail before the fix.
- A writing-plans test showing the compiled verify-fixes agent call carries the review workspace as writable.
- End to end: a small compiled plan whose fix task's verify row writes a file in the checkout (for example `touch .probe && test -f .probe`) passes `verify_fixes` without parking.
