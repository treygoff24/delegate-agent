# 3am-ops — D2

_lane: 3am-ops:omp:gemini · alias omp-10 · exit 0 · 6312 bytes · review-shaped: yes_

=== completionReport ===
## Delegate Sub-Agent Skill Review

- Reviewed available environment skills (`delegate-agent`, `delegate-workflows`, `code-review`, `post`).
- Applied relevant domain contracts from `delegate-workflows` (supervisor detach lifecycle, durability, gate checkpoints, replay, status rebuild) and `code-review` (standards & spec defect tracing).
- **Runtime Environment Note:** Shell execution tools (`bash`/`exec`) are unavailable in this environment; all analysis, code mapping, and sequence validations are grounded in static review of the codebase at commit `f627faf` (parent `ce295d7`).

---

## Delegate Completion Report

- **Status:** completed
- **What was done/found:**
  - Audited commit `f627faf` against all failure boundaries (crashes, timeouts, retries, resume, sub-workflows, and gate approvals).
  - Verified the fix for defect #1 (`--notify` threading into workflow status): confirmed that while top-level status writes preserve `notify`, nested child workflows (`WorkflowDsl.workflow`) construct `child_state` without `notify_target`, causing child status writes to overwrite `status.json` on disk with `"notify": None` and permanently lose notifications after gate pauses or resumes.
  - Verified defect #2 (`agent_timeout` journal detail): found that while the live timeout site in `_run_delegate` was fixed, the sibling adoption-timeout site in `_adopt_existing_agent_run` remains impoverished and fails to notify.
  - Verified defect #3 (`DRY_RUN_HINT`): verified message generation across all call sites; identified misleading guidance for non-launch subcommands (`workflow`, `mail`, `runs`).
  - Verified test arrangement: confirmed the 4 new workflow notify tests are directly attached to `WorkflowCommandTests` rather than a subclass, preventing suite duplication.
- **Files reviewed:**
  - `src/delegate_agent/notify.py`
  - `src/delegate_agent/workflows/runtime.py`
  - `src/delegate_agent/workflows/commands.py`
  - `src/delegate_agent/workflows/registry.py`
  - `src/delegate_agent/constants.py`
  - `src/delegate_agent/cli_parser.py`
  - `tests/test_notify.py`
  - `tests/test_workflow_commands.py`
  - `tests/test_launch_error_hints.py`
- **Verification:** Static code path audit and control flow trace across all supervisor lifecycle and parsing entry points.
- **Remaining risks / follow-ups:** Apply the 1-line propagation of `notify_target` and `replay_journal` in `WorkflowDsl.workflow` and align `_adopt_existing_agent_run`'s timeout event with the live path.

---

VERDICT: exposed (3) · A nested workflow silently erases the supervisor's persisted `notify` target on its first status write, leaving the supervisor permanently mute when it pauses at a gate or completes after resume.

[PAGE] Nested workflow status write clobbers `notify` target in `status.json` · src/delegate_agent/workflows/runtime.py:538
Sequence:
1. Operator launches a gated workflow with `--notify room:ops` (`delegate --notify room:ops workflow run main.py`).
2. `register_workflow` writes `"notify": "room:ops"` to `status.json`; supervisor starts with `notify_target = "room:ops"`.
3. `main.py` invokes a sub-workflow: `workflow("child.py", gate=True)`.
4. `WorkflowDsl.workflow` constructs `child_state = WorkflowState(...)` without passing `notify_target` (defaulting `child_state.notify_target = None`).
5. On the first event or status update inside `child.py`, `child_state._write_status_locked()` writes `payload` containing `"notify": None` to the root's `status.json`.
6. Sub-workflow hits gate checkpoint (`GateExit`), supervisor writes `status="paused"`.
7. Operator approves via `delegate workflow approve <wfId>`, which invokes `workflow run --resume <wfId>` without passing `--notify`.
8. The resumed supervisor reads `status.json` from disk, sees `status.get("notify") == None`, and initializes `WorkflowState(notify_target=None)`.
9. The resumed workflow runs to completion or fails, but sends zero notifications. The operator relying on the doorbell is never alerted.
Who finds out: The operator polling at 3am wondering why a 42-task gated pipeline paused or finished hours ago without ringing.
Fix: Pass `notify_target=self.state.notify_target` and `replay_journal=self.state.replay_journal` when constructing `child_state` in `WorkflowDsl.workflow`.

[PAGE] Adoption wait timeout omits task identity and skips notification · src/delegate_agent/workflows/runtime.py:951
Sequence:
1. Supervisor resumes a workflow where a child run was in flight; `_adopt_existing_agent_run` waits for the child.
2. The adoption wait times out (`_wait_for_workflow_agent_run` returns `False`).
3. Supervisor cancels the child and calls `self.state.append_event("agent_timeout", key=key, scope=scope, runId=run_id)`.
4. Unlike the live timeout site (`_run_delegate:1256`), this site omits `label`, `engine`, `timeout`, and `model`, and never calls `self.state.notify_event("agent_timeout", ...)`.
Who finds out: An operator using `--notify` never receives the timeout alert for adopted children; downstream log scrapers expecting `label`/`engine` on `agent_timeout` find missing keys.
Fix: Thread `engine`, `model`, `timeout`, and `label` into `append_event("agent_timeout", ...)` and invoke `self.state.notify_event("agent_timeout", detail=...)` at the adoption timeout site.

[DRIFT] `DRY_RUN_HINT` advises invalid `delegate dry-run` prefix for non-launch subcommands · src/delegate_agent/constants.py:151
Sequence:
1. Operator makes a syntax or misplaced-option error on a workflow or utility subcommand (e.g. `delegate workflow run script.py --json` or `delegate mail send ...`).
2. Parser raises `misplaced_global_option` with `DRY_RUN_HINT`: `"Validate any invocation without launching it: prefix it with delegate dry-run."`
3. Operator attempts `delegate dry-run workflow run script.py` or `delegate dry-run mail send ...`.
4. Command fails with `invalid_engine: dry-run engine must be cursor, droid, codex, ...` because `delegate dry-run` only accepts harness launch grammar, whereas workflows use `delegate workflow run ... --dry-run`.
Who finds out: An operator debugging CLI syntax at 3am following the error message's instructions verbatim.
Fix: Scope `DRY_RUN_HINT` to harness launch errors, or provide context-specific dry-run guidance (`--dry-run` for workflows).
