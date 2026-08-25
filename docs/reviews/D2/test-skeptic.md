# test-skeptic — D2

_lane: test-skeptic:cursor: · alias cursor-1 · exit 0 · 7316 bytes · review-shaped: yes_

=== completionReport ===
I'll start by loading the review-relevant skills and house rules, then inspect commit `f627faf` with tests first.Skills loaded: test-skeptic (primary), review-agent, code-review, and repo house rules. Fleet is skipped because this is read-only. Next I’ll pull the commit, tests, and parent-commit defects.Shell was blocked on the first try; retrying with narrower read-only commands.Grep works; git via shell was blocked. I'll read the tests and implementation from the tree and retry a narrower git probe.Shell and `.git` are blocked, so this review is static against the current tree. Next I’ll map tests, `WorkflowState` constructors, and the three claimed fixes.## Completion report

- **Status:** completed (static). Every shell invocation was rejected, including `echo`, `git show`, and `git rev-parse`. `.git/HEAD` was permission-denied. Claims are from the current tree only; parent `ce295d7` was **not** executed.
- **What I did:** Read AGENTS.md, CLAUDE.md, shared rules, and the test-skeptic / review-agent skills. Reviewed the four new tests first, then `WorkflowState` constructors, status writes, both `agent_timeout` sites, and all `corrected_command_suffix` / `DRY_RUN_HINT` call sites.
- **Files reviewed:** `tests/test_workflow_commands.py`, `tests/test_notify.py`, `tests/test_delegate_help_cli.py`, `src/delegate_agent/workflows/runtime.py`, `src/delegate_agent/workflows/commands.py`, `src/delegate_agent/workflows/registry.py`, `src/delegate_agent/cli_parser.py`, `src/delegate_agent/constants.py`, `src/delegate_agent/notify.py`, `src/delegate_agent/request_build.py`, `src/delegate_agent/command_help.py`
- **Verification:** none executed (shell blocked). User-stated gate at this commit was not re-run.
- **Follow-ups:** nested and dry-run `WorkflowState` writers can still drop `notify`; live `agent_timeout` fields and `DRY_RUN_HINT` have no new tests.

VERDICT: behavioral. Worst leftover: `test_notify_target_is_parsed_and_rejected_early` would have been green on the parent that still refused `--notify` for `workflow`.

```
[DECOR] test_notify_target_is_parsed_and_rejected_early · tests/test_workflow_commands.py:266
What it pins: `--notify nonsense workflow run` exits nonzero and the combined streams contain the substring "notify"
Why it fails us: `parse_notify_target` runs in the global-option loop before `NOTIFY_SUBCOMMANDS` is consulted (`cli_parser.py:440` then `480`). Garbage targets already failed closed when `workflow` was not a notify subcommand, and that error already contains "notify". This test stays green through the actual defect (valid `--notify` rejected for `workflow run`) and through a fix that only changed the allow-list.
Fix: parse_cli a *valid* target (`channel:x`) against `workflow run` and assert it is accepted; keep the garbage-target case in `tests/test_notify.py` where it already lives.
```

```
[DECOR] test_ambiguous_prompt_source_includes_corrected_command · tests/test_delegate_help_cli.py:558
What it pins: intended to pin that the corrected argv re-parses; after this commit it splits `Corrected command: ….{DRY_RUN_HINT}` and `removesuffix(".")` peels the hint's period, not the command's
Why it fails us: hint words (`Validate`, `any`, `` `delegate ``, …) become extra `prompt_parts`. `parse_cli` still succeeds, so a broken corrected command with a parseable prefix would still pass. No test in this commit asserts `DRY_RUN_HINT` appears, and none pins an exact pre-hint string either — the third claimed fix is unpinned.
Fix: strip `DRY_RUN_HINT` before `shlex.split`, and add one `assertIn("delegate dry-run", message)` on a *launch* parse error (not on `runs` / `capabilities`).
```

The suite actually pins: after a live `workflow run --notify channel:somewhere` of a script that never writes nested events, `status.json` still has that target (`tests/test_workflow_commands.py:211`); a workflow without `--notify` stores none (`:242`); missing `post` on PATH does not stop a trivial script from reaching `status=succeeded` (`:277`). Those three survive an internal rewrite that still emits `notify` on supervisor writes and still calls `send_notification`. They are not theater.

Claims (static):

- **Rebuild-erases-notify.** `_write_status_locked` still rebuilds from scratch (`runtime.py:235`) and now re-emits `notify` from `WorkflowState.notify_target` (`:260`). `run_supervisor` reads it once from disk (`:1727–1747`). `write_status` already preserved `createdAt`, `createdOrdinal`, `scriptSha256`, and `args` (`registry.py:115–131`); notify was the unlisted sibling, handled by carrying state instead of joining that list. Resume (`commands.py:251–254`), kill (merge, `:544–556`), and approve (resume with `notify is None`, so keep, `:518–519`) do not themselves drop a target that is still on disk. Two other `WorkflowState(...)` sites do: nested child at `runtime.py:644` (no `notify_target=`) and dry-run at `commands.py:309`. Either path's `append_event` writes `"notify": null` and will lose the target; a later resume then rings nobody. Nested writes also replace `scriptPath` / `supervisorToken` with the child's — same rebuild class, pre-existing, not newly carried. `test_notify_survives_the_supervisors_status_rebuild` uses `return {"ok": True}` with no nesting and no `--dry-run`, so those writers stay green.

- **Notify failure modes.** `send_notification` already degrades on missing binary, hang (`NOTIFY_TIMEOUT_SEC=10`, killpg), nonzero, and stderr-on-success (`notify.py:148–200`); it does not raise. `notify_event`'s `except Exception` (`runtime.py:296`) is therefore unhit by the PATH test. Worse, `write_status("succeeded")` happens *before* `notify_event` (`:1773–1774`), and the test never waits for the supervisor lock to drop — a hang or a raise-after-success still looks like `succeeded`. Missing `post` is the right customer-visible pin for "launch + run still succeed"; hang is not pinned at the workflow layer and would not even slow this test. Nonzero/stderr-success are already owned by `tests/test_notify.py`.

- **`agent_timeout`.** The fixed live site now records `key`, `label`, `model` (plus engine/timeout) (`runtime.py:1273`). The sibling adoption site still records `key`, `scope`, `runId` only (`:967`). They agree on `key`; they are not the same row. `test_adoption_wait_timeout_cancels_child_without_duplicate` (`:2507`) only asserts the type exists, on the sibling that already had `key`. Reverting the live-site fields would stay green. `agent_failed` (`:920`) and `agent_structured_retry` (`:1196`) still have no label.

- **`DRY_RUN_HINT`.** Appended by all five `corrected_command_suffix` sites (`cli_parser.py:890`, used at `922`, `1068`, `1205`, `2482`, `2496`) plus direct concatenations (`:1560`, `request_build.py:427`). On a launch combo error the hint is the right pointer. On `raise_misplaced_global_option` for inspection commands it is misleading: `delegate dry-run --auth-profile work capabilities refresh` is not a validator. Messages still read; no exact old-string pin through this helper.

- **Class arrangement.** One class, `WorkflowCommandTests` (`tests/test_workflow_commands.py:27`), 94 `test_*` methods, no subclass. The four new tests are methods on that class (`:211–304`). They collect once; they do not re-run the other ninety.
