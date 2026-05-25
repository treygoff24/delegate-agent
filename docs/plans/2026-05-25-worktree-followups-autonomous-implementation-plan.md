# Worktree Followups Autonomous Implementation Plan

**Goal:** Ship one consolidated cleanup pass that fixes every known worktree-isolation follow-up, the final-review bugs, and the related docs/test gaps without promoting the installed `~/.delegate` runtime unless Trey separately asks.

**Architecture:** Treat this as a single coordinator-owned integration run with Delegate subagents used for bounded implementation and review lanes. Correctness and public-contract changes land before refactors; refactors then preserve the already-green behavior through narrow tests, full unittest, and two final review loops.

**Tech Stack:** Python stdlib CLI, Git subprocess orchestration, Delegate run registry JSON files, `unittest`, local `python3 bin/delegate.py`, Delegate Cursor/Droid/Codex harnesses.

---

## Path Convention

Unless a command explicitly needs an absolute `--cwd`, all paths in this plan are relative to the repository root. Delegate implementation prompts must preserve repo-relative paths so child agents running in isolated execution worktrees do not accidentally target the source checkout.

## Source Inputs

- `docs/plans/2026-05-25-post-ship-followups.md`
- Final clean-code / Delegate review findings from 2026-05-25:
  - missing-path `remove --force-branch` does not delete branch
  - positional `Request(...)` construction corrupts temporary-isolation metadata
  - `config.example.json` omits `isolation` / `worktrees`
  - README understates Droid work permissions and persistent pass-through restriction
  - worktree JSON error payloads differ from standard `DelegateError` JSON
  - prune `--harness` / `--force` coverage gaps
  - `_remove_branch` overloads `branchKept` with git error strings
  - `gc_worktrees` informational `prunedSourceRoots` can be stale under races
  - `_DummyLockContext`, multiline warnings, and bare prune docs polish

## Non-Negotiable Invariants

- Use `python3 bin/delegate.py` from this checkout. Do **not** use bare `delegate`.
- Do **not** mutate `~/.delegate`, `~/.local/bin/delegate`, or installed shims.
- Do **not** delete `~/.delegate/worktrees/` manually.
- Preserve existing supported CLI contracts unless this plan explicitly changes them.
- Worktree-management destructive paths must remain fail-closed for dirty or unmerged work unless the user passed explicit destructive flags.
- Tests that create Delegate worktrees must set `HOME` to a `TemporaryDirectory`.
- Keep `.delegate/`, generated caches, and local runtime artifacts out of Git.
- The current source checkout must be clean before using `--isolation worktree` with work mode. If `.code-briefcaseignore` is still untracked, classify it before running persistent-worktree subagents: commit if intentionally repo-owned, otherwise add it to local exclude or remove only with explicit approval.

## Issue Coverage Matrix

| ID | Source | Issue | Planned task |
| --- | --- | --- | --- |
| BUG-A | final review | Missing-path `remove --force-branch` leaves branch behind | Task 2 |
| BUG-B | final review | `safe_isolated_request` positional `Request(...)` corrupts `model_alias` / `dry_run` / `workspace_kind` | Task 3 |
| BUG-C | follow-up §3.1 | `prune` treats merge-check `None` like unmerged | Task 2 |
| BUG-D | follow-up §3.6 / §2.1 | Branch collision greps Git stderr, blocking real L837 test | Task 4 |
| BUG-E | follow-up §3.9 | Git subprocess calls can hang without timeout | Task 5 |
| BUG-F | final review | Worktree JSON errors use `code` without normal `error` / `exitCode` | Task 6 |
| BUG-G | final review | `_remove_branch` can surface git errors as `branchKept` reasons | Task 2 |
| TEST-A | follow-up §2.2 | No full CLI `worktree list --no-auto-prune` test | Task 7 |
| TEST-B | follow-up §2.3 | No detached `prune --include-detached` happy-path test | Task 7 |
| TEST-C | follow-up §2.4 | Tri-state warning text shape unpinned | Task 7 |
| TEST-D | final review | No prune `--harness` and prune `--force` shorthand tests | Task 7 |
| REFACTOR-A | follow-up §1.1 | `_execute_persistent_worktree` decomposition | Task 8 |
| REFACTOR-B | follow-up §1.2 | `remove_worktree` decomposition | Task 9 |
| REFACTOR-C | follow-up §1.3 | Table-drive `parse_worktree` / `emit_worktree` | Task 10 |
| SMELL-A | follow-up §3.2 | `gc_worktrees` repeated reload/write blocks | Task 11 |
| SMELL-B | follow-up §3.3 | Cleanup-hint block duplicated in runner/rendering | Task 12 |
| SMELL-C | follow-up §3.4 | `_record_for_run` carries `_state` / `_manifest` / `_snapshot` | Task 13 |
| SMELL-D | follow-up §3.5 | `WorktreeManagementError` accepts malformed payloads | Task 6 |
| SMELL-E | follow-up §3.7 | `dirty_info` and `porcelain_status` overlap | Task 14 |
| SMELL-F | follow-up §3.8 | Magic dirty-path cap `20` | Task 14 |
| SMELL-G | follow-up §3.10 | `repo_fingerprint` does I/O despite pure-sounding name | Task 4 |
| SMELL-H | follow-up §3.11 | Repeated `isinstance(x, dict)` guards | Task 13 |
| SMELL-I | follow-up §3.12 | `_skip_lock` escape hatch is unenforced | Task 15 |
| DOC-A | follow-up §4.1 | Live runtime stale | Task 16 |
| DOC-B | follow-up §4.2 | Lingering persistent worktrees from cleanup sessions | Task 16 |
| DOC-C | follow-up §4.3 | README / AGENTS / live-runtime docs re-pass | Task 16 |
| DOC-D | final review | `config.example.json` missing `isolation` / `worktrees` | Task 16 |
| DOC-E | final review | README understates Droid work `--skip-permissions-unsafe` | Task 16 |
| DOC-F | final review | README pass-through restriction omits Droid/persistent-general case | Task 16 |
| DOC-G | final review | README shows invalid bare `delegate worktree prune` summary | Task 16 |

## Delegate / Subagent Strategy

Use Delegate for both implementation and review, but keep coordinator ownership of integration:

1. **Pre-implementation mapping subagents, read-only**
   - `python3 bin/delegate.py --json droid qwen safe --prompt-file docs/plans/prompts/worktree-followups-mapping-worktree-mgmt.md`
   - `python3 bin/delegate.py --json droid "deepseek v4 pro" safe --prompt-file docs/plans/prompts/worktree-followups-mapping-cli-isolation.md`
   - `python3 bin/delegate.py --json cursor safe --prompt-file docs/plans/prompts/worktree-followups-mapping-docs.md`
   - Store reusable prompt files under `docs/plans/prompts/` rather than `/tmp`; remove them before final commit unless they are intentionally useful artifacts.
2. **Implementation worktrees, only after clean-source preflight**
   - Maximum concurrent implementation worktrees: **1** through Task 10. Correctness tasks and the highest-risk `cli.py` refactors are serial because they share `cli.py`, `worktree_mgmt.py`, and test modules.
   - Prefer `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-N.md` for mechanical docs/refactor lanes.
   - Prefer `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-N.md` for bugfix/test lanes that need careful reasoning.
   - Inspect each run with `python3 bin/delegate.py worktree show <alias>` and merge/cherry-pick only reviewed diffs into the coordinator branch.
3. **Review loops**
   - After implementation: use at least two read-only Delegate reviewers, one Cursor Composer and one Droid/Codex, both instructed to load clean-code and review the full diff.
   - Patch any real findings, rerun narrow tests, then rerun the reviewer that found the issue.

Do not pipe Delegate launches through `tail`; use `snapshot`, `runs`, and `run-output`.

---

## Task 0: Preflight and Branch Setup

**Parallel:** no  
**Blocked by:** none  
**Owned files:** none  
**Invariants:** Do not delete or stage unrelated user files. Do not promote installed runtime.  
**Out of scope:** Any code changes.

**Files:**
- Read: `AGENTS.md`
- Read: `docs/plans/2026-05-25-post-ship-followups.md`
- Read: `docs/plans/2026-05-24-work-mode-isolation-spec.md`

**Step 1: Inspect state**
Run:
```bash
git status --short --branch
python3 bin/delegate.py --json models
python3 bin/delegate.py --json describe
```
Expected:
- Branch and dirty files are known.
- Delegate model aliases resolve.
- Config reports `isolation` and `worktrees` defaults.

**Step 2: Create implementation branch**
Run:
```bash
git switch main
git switch -c codex/worktree-followups-autonomous main
```
Expected: branch created from the local `main` tip. If `main` is detached or unavailable, stop and record the exact base SHA before branching. Do not pull/rebase automatically unless Trey explicitly asks.

**Step 3: Ensure persistent-worktree readiness**
If `git status --short` reports only local tooling artifacts such as `.code-briefcaseignore`, decide before work-mode Delegate isolation:
- If repo-owned: commit intentionally in Task 16.
- If local-only: add to `.git/info/exclude`.
- If accidental: ask before deletion.

**Verification plan:**
- Primary command: `git status --short --branch`
- Expected: implementation branch is active; source cleanliness strategy is explicit.

---

## Task 1: Read-Only Mapping Subagents

**Parallel:** yes  
**Blocked by:** Task 0  
**Owned files:** none  
**Invariants:** Read-only only; no files changed.  
**Out of scope:** Implementation.

**Files:**
- Read: all files named in this plan's tasks.

**Step 0: Create repo-local prompt files**
Create these prompt files before launching subagents:
- `docs/plans/prompts/worktree-followups-mapping-worktree-mgmt.md`
- `docs/plans/prompts/worktree-followups-mapping-cli-isolation.md`
- `docs/plans/prompts/worktree-followups-mapping-docs.md`
- `docs/plans/prompts/worktree-followups-task-2.md` through `docs/plans/prompts/worktree-followups-task-16.md` for any task delegated to a worktree agent.
- `docs/plans/prompts/worktree-followups-final-review-code.md`
- `docs/plans/prompts/worktree-followups-final-review-cursor.md`

Each prompt must be read-only, scoped to the files listed below, and must say to use repo-relative paths. Verify the files exist before launching.

**Step 1: Launch worktree-management mapper**
Prompt scope:
- `src/delegate_agent/worktree_mgmt.py`
- `src/delegate_agent/run_registry.py`
- `tests/test_delegate_worktree_mgmt.py`
- `tests/test_run_registry.py`
Ask for exact line references and implementation order for BUG-A, BUG-C, BUG-G, SMELL-A, SMELL-C, SMELL-E/F, SMELL-I.

Run:
```bash
python3 bin/delegate.py --json droid qwen safe --prompt-file docs/plans/prompts/worktree-followups-mapping-worktree-mgmt.md
```

**Step 2: Launch CLI/isolation mapper**
Prompt scope:
- `src/delegate_agent/cli.py`
- `src/delegate_agent/isolation.py`
- `src/delegate_agent/runner.py`
- `tests/test_delegate_execution.py`
- `tests/test_delegate_parser.py`
Ask for exact line references and implementation order for BUG-B, BUG-D, BUG-E, BUG-F, REFACTOR-A, REFACTOR-C.

Run:
```bash
python3 bin/delegate.py --json droid "deepseek v4 pro" safe --prompt-file docs/plans/prompts/worktree-followups-mapping-cli-isolation.md
```

**Step 3: Launch docs/contract mapper**
Prompt scope:
- `README.md`
- `AGENTS.md`
- `docs/development.md`
- `docs/live-runtime.md`
- `config.example.json`
Ask for exact doc drift and config-example changes for DOC-A through DOC-G.

Run:
```bash
python3 bin/delegate.py --json cursor safe --prompt-file docs/plans/prompts/worktree-followups-mapping-docs.md
```

**Step 4: Verify implementation/review prompt files before any delegated work**
Before launching any `--prompt-file docs/plans/prompts/worktree-followups-task-*.md` or final-review prompt, run:
```bash
test -f docs/plans/prompts/worktree-followups-task-2.md
test -f docs/plans/prompts/worktree-followups-final-review-code.md
test -f docs/plans/prompts/worktree-followups-final-review-cursor.md
```
Expected: every prompt file needed for the selected delegated tasks exists.

**Verification plan:**
- Primary commands:
```bash
python3 bin/delegate.py run-output <alias> --completion-report
```
- Expected: each mapper returns findings or "no additional findings"; coordinator updates task details only if new real blockers appear.

---

## Task 2: Worktree Removal and Prune Correctness

**Parallel:** no  
**Blocked by:** Task 1  
**Execution:** `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-2.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Dirty worktrees and unmerged branches remain protected by default. `--force` remains shorthand for `--discard-uncommitted --force-branch`.  
**Out of scope:** Parser rewrites and docs.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Modify: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Add failing test for missing-path force branch**
Add a test that:
1. Creates a persistent-run record for a real branch.
2. Removes the worktree path outside Delegate or seeds `worktreeStatus="missing"`.
3. Calls `remove_worktree(..., force_branch=True)`.
4. Asserts the branch no longer resolves and payload has `branchRemoved: true`.

Run:
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt.DelegateWorktreeMgmtTests.test_worktree_remove_missing_path_force_branch_deletes_branch
```
Expected: FAIL before implementation.

**Step 2: Fix missing-path path**
In `remove_worktree`, when `status == STATUS_MISSING`, honor `force_branch` inside the existing `with lock_ctx:` critical section and before the early return:
- If `force_branch` and not `keep_branch`, call branch deletion.
- Preserve `branchKept: "path_missing"` only when branch was not deleted.
- Keep state mutation through `set_worktree_status`.

**Step 3: Add failing test for `_remove_branch` error-shape**
Mock or construct a case where branch deletion fails and assert payload does not encode raw git stderr as a `branchKept` business reason.

**Step 4: Split branch deletion result**
Replace `_remove_branch -> tuple[bool, str | None]` with either:
- a small dataclass `BranchRemovalResult(removed: bool, kept_reason: str | None, error: str | None)`, or
- two explicit helpers for intentional keep vs deletion error.

Expected payload contract:
- Intentional kept branch: `branchKept: "requested" | "unmerged" | "path_missing"`
- Deletion failure: `branchRemoved: false`, `branchRemovalError: <message>`, and `ok` reflects whether the containing operation should be considered successful.

**Step 5: Audit existing prune tests for old `None` semantics**
Before changing behavior, inspect existing `prune_worktrees` tests and confirm none intentionally assert the current `merged_value is None` behavior where the path is removed and branch is silently kept.

**Step 6: Add failing test for merge-check `None`**
Force `merged_into_source` to return `None` in prune selection and assert prune skips with `reason: "merge_check_failed"` instead of removing the path and keeping the branch.

**Step 7: Fix prune merge-check imprecision**
In `prune_worktrees`:
- `merged_value is False` -> current unmerged handling.
- `merged_value is None` -> skip with `merge_check_failed` and warnings if available.
- `merged_value is True` -> existing eligible path.

**Verification plan:**
- Primary command:
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
```
- Expected: all worktree-management tests pass.
- Secondary command:
```bash
python3 -m unittest discover -s tests
```
- Expected: full suite remains green before moving to refactors.

---

## Task 3: Temporary Isolation Request Construction Bug

**Parallel:** no  
**Blocked by:** Task 2  
**Execution:** coordinator preferred (small targeted `cli.py` fix)  
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_execution.py`  
**Invariants:** Cursor/Codex safe still run in isolated temporary workspaces by default. JSON `cwd` remains source; `executionCwd` remains isolated copy.  
**Out of scope:** Persistent-worktree implementation refactor.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_execution.py`

**Step 1: Add failing test**
Add a safe-isolation test that constructs a Droid or Cursor `Request` with:
- `model_alias="qwen"` or equivalent non-null alias
- `dry_run=True`
- `workspace_kind="git"`
- effective temporary worktree isolation

Assert the yielded isolated request preserves:
- `model_alias == request.model_alias` (currently lost and replaced by `request.dry_run`)
- `dry_run == request.dry_run` (currently receives `request.workspace_kind`)
- `workspace_kind == request.workspace_kind` (currently falls back to the dataclass default)
- `isolation_context.isolation_lifecycle == "temporary"`

Run:
```bash
python3 -m unittest tests.test_delegate_execution.DelegateExecutionTests.test_safe_isolated_request_preserves_request_metadata
```
Expected: FAIL before fix.

**Step 2: Convert positional constructor to keyword args**
Change `safe_isolated_request` to construct `Request(...)` with keyword arguments for every field, explicitly including:
```python
model_alias=request.model_alias,
dry_run=request.dry_run,
workspace_kind=request.workspace_kind,
isolation_context=isolation,
```

**Step 3: Verify other `Request(...)` call sites**
Verify the three production `Request(...)` call sites in `request_from_parsed` already use keyword style for `model_alias`, `dry_run`, `workspace_kind`, and `isolation_context`. Do not churn tests unless needed.

**Verification plan:**
- Primary command:
```bash
python3 -m unittest tests.test_delegate_execution.DelegateExecutionTests.test_safe_isolated_request_preserves_request_metadata
```
- Secondary command:
```bash
python3 -m unittest tests.test_delegate_execution
```

---

## Task 4: Branch-Collision Detection and Repo Fingerprint Naming

**Parallel:** no  
**Blocked by:** Task 3  
**Execution:** `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-4.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/isolation.py`, `src/delegate_agent/cli.py`, `tests/test_delegate_execution.py`, `tests/test_delegate_isolation.py`  
**Invariants:** Existing `branch_collision` behavior remains for pre-existing branch refs. Real `git worktree add` failures surface as `worktree_create_failed` with Git stderr.  
**Out of scope:** General git timeout helper; Task 5 owns that.

**Files:**
- Modify: `src/delegate_agent/isolation.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_execution.py`
- Modify: `tests/test_delegate_isolation.py`

**Step 1: Add explicit branch-ref probe helper**
`worktree_mgmt.py` already has a private `_branch_exists` helper that probes an unqualified ref name. For worktree creation, add a separate helper in `isolation.py` with branch-ref-specific semantics and a distinct name:
```python
def branch_ref_exists(source_git_root: str, branch: str) -> bool | None:
    ...
```
Use `git rev-parse --verify --quiet refs/heads/<branch>`.

**Step 2: Add failing L837 test**
Create a scenario where `git worktree add` fails because the branch is already checked out or another non-branch-collision worktree-add failure occurs, and assert:
- error is `worktree_create_failed`
- message includes underlying Git stderr
- cleanup does not delete pre-existing branch refs.

Expected: currently fails because stderr grep can reclassify.

**Step 3: Replace stderr-grep classification**
Before `git worktree add`, probe branch existence:
- If branch exists before creation, raise `branch_collision`.
- Otherwise run `git worktree add`.
- If `git worktree add` fails, raise `worktree_create_failed` without stderr pattern classification.

**Step 4: Rename `repo_fingerprint`**
Rename `repo_fingerprint` to `compute_repo_fingerprint_from_common_dir` and update call sites. The new name should make the filesystem-resolution cost explicit in the name and docstring.

**Verification plan:**
- Primary commands:
```bash
python3 -m unittest tests.test_delegate_isolation
python3 -m unittest tests.test_delegate_execution.DelegateExecutionTests.test_persistent_worktree_add_collision_fails_and_cleans_up
```
- Expected: isolation and targeted execution tests pass.

---

## Task 5: Git Subprocess Timeout Helper

**Parallel:** no  
**Blocked by:** Tasks 2 and 4  
**Execution:** `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-5.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/isolation.py`, `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_isolation.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Timeout failures become structured, user-actionable errors; normal Git failures still surface stderr.  
**Out of scope:** Child-agent process runtime timeout policy.

**Files:**
- Create: `src/delegate_agent/git_utils.py`
- Modify: `src/delegate_agent/isolation.py`
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Modify: `tests/test_delegate_isolation.py`
- Modify: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Introduce constants**
Create `src/delegate_agent/git_utils.py` as the shared no-cycle owner for Git subprocess constants and timeout exceptions. Add:
```python
GIT_QUICK_TIMEOUT_SECONDS = 10
GIT_MUTATION_TIMEOUT_SECONDS = 30
```
Also add a small `GitCommandTimeout` exception if it makes translation cleaner. `isolation.py` should translate it into `IsolationExecutionError`; `worktree_mgmt.py` should translate it into `WorktreeManagementError` or a structured git failure payload.

**Step 2: Add timeout tests**
Use monkeypatching/mocking of `subprocess.run` to raise `subprocess.TimeoutExpired` for:
- `worktree_mgmt._run_git`
- `isolation.require_clean_source`
- `isolation.create_persistent_worktree`

Expected: structured errors include timeout code/message.

**Step 3: Implement timeout handling**
- In `worktree_mgmt._run_git`, pass `timeout=...` and convert `TimeoutExpired` into `WorktreeManagementError` where call sites can propagate it cleanly, or return a `CompletedProcess`-like failure only if the existing tuple APIs need that shape.
- In `isolation.py`, catch `TimeoutExpired` and raise `IsolationExecutionError("git_timeout", ...)`.

**Step 4: Avoid holding lock across unbounded Git**
After `_run_git` has timeouts, verify all lock-held Git calls in `gc_worktrees`, `remove_worktree`, and `maybe_auto_prune` use timed helpers.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_isolation tests.test_delegate_worktree_mgmt
```
Expected: pass.

---

## Task 6: Worktree Error Payload Contract

**Parallel:** no  
**Blocked by:** Task 5  
**Execution:** coordinator preferred because this touches shared CLI/error contract  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `src/delegate_agent/cli.py`, `tests/test_delegate_worktree_mgmt.py`, `tests/test_delegate_parser.py`  
**Invariants:** Existing `code` field remains for compatibility; JSON errors also include standard `error` and `exitCode`.  
**Out of scope:** Large renderer refactor.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_worktree_mgmt.py`
- Modify: `tests/test_delegate_parser.py`

**Step 1: Add parser characterization tests**
Before changing worktree error payload shape, add or verify parser characterization tests for:
- misplaced global option in worktree subcommands
- unknown option per action
- `show --latest` mutually exclusive with handle
- `remove --keep-branch --force` invalid
- `prune` requires at least one filter at execution time

These characterize public `cli.py` behavior before this task touches error normalization.

**Step 2: Add failing JSON schema test**
For a worktree management error, e.g. no registry or unknown handle, assert JSON has:
```json
{
  "ok": false,
  "code": "...",
  "error": "...",
  "message": "...",
  "exitCode": 2
}
```

**Step 3: Harden `WorktreeManagementError`**
Change constructor to require valid shape:
- `code` must be a non-empty string.
- `message` must be a non-empty string.
- `ok` should be `False`.

Option A: leave payload-builder call sites but validate.  
Option B: change constructor to `WorktreeManagementError(code, message, **extra)` and build payload centrally. Prefer Option A if less invasive.

**Step 4: Normalize in `_error_payload` or catch block**
Ensure every worktree error payload includes:
- `error` alias equal to `code`
- `exitCode` equal to `EXIT_USAGE` unless a different exit is explicitly appropriate.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt tests.test_delegate_parser
```

---

## Task 7: Coverage Gap Closeout

**Parallel:** no  
**Blocked by:** Tasks 2, 4, 5, 6  
**Execution:** coordinator preferred; use Delegate safe reviewers for test adequacy only  
**Owned files:** `tests/test_delegate_worktree_mgmt.py`, `tests/test_delegate_execution.py`, `tests/test_delegate_parser.py`  
**Invariants:** Tests must use temp homes and must not write to real `~/.delegate`.  
**Out of scope:** New behavior not needed to satisfy tests.

**Files:**
- Modify: `tests/test_delegate_worktree_mgmt.py`
- Modify: `tests/test_delegate_execution.py`
- Modify: `tests/test_delegate_parser.py`

**Step 1: Add full CLI `--no-auto-prune` test**
Invoke `main()` through the existing helper with config opt-in and `worktree list --no-auto-prune`; assert no opportunistic prune result and no removal.

**Step 2: Add detached include happy path**
First check for existing API coverage such as `test_prune_includes_detached_with_flag`. If API coverage already exists, add the missing CLI-level `main()` coverage with argv equivalent to `python3 bin/delegate.py worktree prune --merged --include-detached`; otherwise add the API test and then the CLI test if the CLI path is still unpinned.

**Step 3: Pin warning text shape**
For dirty and merged tri-state failures, assert warnings include stable prefixes:
- `git status failed:`
- `could not determine whether branch is merged`

**Step 4: Add prune `--harness` filter test**
Seed cursor + droid worktrees; prune with harness filter; assert only matching harness is planned/removed.

**Step 5: Add prune `--force` shorthand test**
Seed dirty but otherwise eligible worktree; prune with `force=True`; assert `discard_uncommitted` and `force_branch` semantics both apply.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt tests.test_delegate_execution tests.test_delegate_parser
```

---

## Task 8: Decompose `_execute_persistent_worktree`

**Parallel:** no  
**Blocked by:** Tasks 2-7  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-8.md` acceptable after Task 7 passes; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_execution.py`  
**Invariants:** No behavior changes; this task is refactor-only.  
**Out of scope:** Parser rewrite.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Test: `tests/test_delegate_execution.py`

**Step 1: Extract preflight**
Extract:
```python
def _validate_persistent_worktree_request(...)
```
Owns Git workspace check, valid HEAD, pass-through rejection, registry setup, retention, and clean-source check.

**Step 2: Extract registration/context build**
Extract:
```python
def _register_persistent_worktree_run(...)
```
Returns run id, alias, run path, branch, worktree path, creation context, and pre-launch `RunContext`.

**Step 3: Extract create-or-record-failure**
Extract:
```python
def _create_persistent_worktree_or_record_failure(...)
```
Owns `create_persistent_worktree`, failed state/snapshot, partial cleanup.

**Step 4: Extract child launch**
Extract:
```python
def _launch_child_in_persistent_worktree(...)
```
Owns argv rewrite, prompt insertion, final manifest write, and tracked execution.

**Step 5: Remove numbered position-marker comments**
Replace step comments with intention-revealing function names.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_execution
```
Expected: pass.

---

## Task 9: Decompose `remove_worktree`

**Parallel:** no  
**Blocked by:** Tasks 2, 6, and 7  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-9.md` acceptable after Task 7 passes; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Payload keys and safety semantics stay compatible except intentional additions from Tasks 2 and 6.  
**Out of scope:** Parser changes.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Test: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Extract option normalization**
```python
def _normalize_remove_options(...)
```
Returns normalized discard/force/keep booleans and validates mutual exclusions.

**Step 2: Extract payload factory**
```python
def _remove_payload(...)
```
Used for already-removed, missing-path, and normal success paths.

**Step 3: Extract safety guards**
- `_raise_if_dirty_without_discard`
- `_raise_if_unmerged_without_override`
- `_require_removal_metadata`

**Step 4: Extract mutation helpers**
- `_remove_worktree_path`
- `_remove_branch_if_requested`
- `_mark_worktree_removed`

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
```

---

## Task 10: Table-Drive Worktree Parser and Dispatcher

**Parallel:** no  
**Blocked by:** Tasks 6, 7, and 8  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-10.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_parser.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Existing error codes (`misplaced_global_option`, `unknown_option`, `missing_handle`, etc.) remain stable.  
**Out of scope:** Worktree-management behavior.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_parser.py`
- Modify: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Verify parser characterization tests**
Task 6 should already have added or verified parser characterization tests for:
- misplaced global option in worktree subcommands
- unknown option per action
- `show --latest` mutually exclusive with handle
- `remove --keep-branch --force` invalid
- `prune` requires at least one filter at execution time
Before refactoring, run those tests and add any missing cases.

**Step 2: Add option spec table**
Example shape:
```python
WORKTREE_OPTION_SPECS = {
    "list": {"--harness": _set_str(...), "--status": _set_status(...), ...},
    "prune": {"--merged": _set_flag(...), "--older-than": _set_non_negative_int(...), ...},
}
```

**Step 3: Replace cascade with generic loop**
Keep positional validation per action after option parsing.

**Step 4: Table-drive dispatcher**
Use action map for function + renderer pairs while preserving per-action argument assembly.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_worktree_mgmt
```

---

## Task 11: Deduplicate `gc_worktrees`

**Parallel:** no  
**Blocked by:** Tasks 5, 7, and 9  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-11.md` acceptable after Task 7 passes; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** `gc` never deletes worktree paths; it only reconciles registry state and runs safe `git worktree prune` for missing paths.  
**Out of scope:** Remove/prune behavior.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Test: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Extract reconciliation helper**
```python
def _reconcile_record_under_lock(registry_root, record, predicate, status, append_result) -> bool:
    ...
```

**Step 2: Use helper for three branches**
Apply to:
- missing path -> `missing`
- metadata missing -> `unknown`
- branch missing -> `unknown`

**Step 3: Make `prunedSourceRoots` actual-operation-based**
Only count roots where a missing-path reconciliation actually occurred in this run.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
```

---

## Task 12: Centralize Cleanup-Hint Rendering

**Parallel:** yes  
**Blocked by:** Tasks 2 and 6  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-12.md`; may run in parallel only if no other active task touches `runner.py`, `rendering.py`, `test_runner_capture.py`, or `test_snapshot_commands.py`  
**Owned files:** `src/delegate_agent/rendering.py`, `src/delegate_agent/runner.py`, `tests/test_runner_capture.py`, `tests/test_snapshot_commands.py`  
**Invariants:** Text output stays byte-for-byte compatible unless tests are updated in the same task for intentional wording changes.  
**Out of scope:** Worktree-management logic.

**Files:**
- Modify: `src/delegate_agent/rendering.py`
- Modify: `src/delegate_agent/runner.py`
- Modify: `tests/test_runner_capture.py`
- Modify: `tests/test_snapshot_commands.py`

**Step 1: Add shared helper**
In `rendering.py`:
```python
def render_worktree_cleanup_commands(cleanup: JsonObject, stdout: TextIO) -> None:
    ...
```

**Step 2: Use helper in snapshot and bounded summary renderers**
Have `runner.py` call the shared helper from `rendering.py` without introducing import cycles. If an import cycle appears, move the helper to a new lightweight module such as `text_blocks.py`.

**Step 3: Verify output tests**
Run:
```bash
python3 -m unittest tests.test_runner_capture tests.test_snapshot_commands
```

---

## Task 13: Typed Persistent Record and Dict Access Helpers

**Parallel:** no  
**Blocked by:** Tasks 2 and 11  
**Execution:** `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-13.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Public JSON from `list/show/remove/prune/gc` never includes private `_state`, `_manifest`, `_snapshot`.  
**Out of scope:** Run registry storage format changes.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Test: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Add helper accessors**
Add:
```python
def _get_str(source: object, key: str) -> str | None:
    ...
def _get_dict(source: object, key: str) -> JsonObject:
    ...
```

**Step 2: Replace repeated guards**
Apply to `_branch_from`, `_execution_cwd_from`, `_source_git_root_from`, `_is_persistent_worktree_run`, and `_record_for_run`.

**Step 3: Remove private cargo from returned records**
If internals are not needed downstream, stop attaching `_state`, `_manifest`, `_snapshot`.
If internals are needed, use a private dataclass and convert to public dict before rendering.

**Step 4: Add no-private-keys assertion**
Test `list_worktrees` and `show_worktree` JSON payloads do not contain keys beginning with `_`.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
```

---

## Task 14: Dirty Status Helpers and Constants

**Parallel:** no  
**Blocked by:** Task 13  
**Execution:** coordinator preferred; small refactor in shared worktree-management path  
**Owned files:** `src/delegate_agent/worktree_mgmt.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** Dirty-path truncation remains 20 entries unless explicitly changed later.  
**Out of scope:** Prune/remove safety policy.

**Files:**
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Test: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Add constant**
```python
MAX_DIRTY_PATHS_REPORTED = 20
```

**Step 2: Collapse overlap**
Either:
- make `dirty_info` a thin wrapper around `porcelain_status`, or
- replace `dirty_info` with `_dirty_paths_for_record(record, limit=None)`.

**Step 3: Improve multiline warning rendering**
If Git stderr has multiple lines, either split warnings in `porcelain_status` or update renderer to display multiline warnings legibly.

**Verification plan:**
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
```

---

## Task 15: Lock Discipline Hardening

**Parallel:** no  
**Blocked by:** Tasks 2, 6, 9, 11  
**Execution:** `python3 bin/delegate.py --json --isolation worktree droid "deepseek v4 pro" work --prompt-file docs/plans/prompts/worktree-followups-task-15.md` or coordinator; max concurrent implementation worktrees = 1  
**Owned files:** `src/delegate_agent/run_registry.py`, `src/delegate_agent/worktree_mgmt.py`, `tests/test_run_registry.py`, `tests/test_delegate_worktree_mgmt.py`  
**Invariants:** No nested-flock deadlock; all registry mutations still happen under lock.  
**Out of scope:** Replacing file locks entirely.

**Files:**
- Modify: `src/delegate_agent/run_registry.py`
- Modify: `src/delegate_agent/worktree_mgmt.py`
- Modify: `tests/test_run_registry.py`
- Modify: `tests/test_delegate_worktree_mgmt.py`

**Step 1: Replace `_DummyLockContext`**
Use `contextlib.nullcontext()` or introduce an explicit locked mutation helper.

**Step 2: Add lock-token concept if low-churn**
Preferred:
```python
with run_registry.locked_registry(registry_root) as locked:
    locked.set_worktree_status(...)
```
Fallback:
- Keep `_skip_lock`, but add a clear helper only callable inside lock-held code paths and tests proving misuse is not exposed through CLI.

**Step 3: Keep contention behavior**
`maybe_auto_prune` still skips quickly under lock contention.

**Verification plan:**
```bash
python3 -m unittest tests.test_run_registry tests.test_delegate_worktree_mgmt
```

---

## Task 16: Docs, Examples, and Runtime-Handoff Cleanup

**Parallel:** yes  
**Blocked by:** Tasks 2-7 for behavior facts; can draft earlier but final patch must wait.  
**Execution:** `python3 bin/delegate.py --json --isolation worktree cursor work --prompt-file docs/plans/prompts/worktree-followups-task-16.md`; may run in parallel with Task 12 only if Task 12 is not modifying docs and the coordinator can integrate one worktree at a time  
**Owned files:** `README.md`, `AGENTS.md`, `docs/development.md`, `docs/live-runtime.md`, `config.example.json`, `docs/plans/2026-05-25-post-ship-followups.md`  
**Invariants:** Do not claim installed `delegate` has the feature until promotion occurs. Do not instruct manual deletion of Delegate-managed worktrees.  
**Out of scope:** Promoting live runtime.

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/development.md`
- Modify: `docs/live-runtime.md`
- Modify: `config.example.json`
- Modify: `docs/plans/2026-05-25-post-ship-followups.md`

**Step 1: Update `config.example.json`**
Insert after the `codex` object, with a trailing comma on the `codex` closing brace before these new top-level keys:
```json
"isolation": {
  "safe": "auto",
  "work": "none"
},
"worktrees": {
  "dataHome": null,
  "autoPrune": {
    "enabled": false,
    "mergedOlderThanDays": 7
  }
}
```

**Step 2: Update README default behavior table**
Change Droid work row from "none beyond Droid work defaults" to explicit `--skip-permissions-unsafe`.

**Step 3: Update pass-through restriction text**
State that `--pass-through` is unsupported for any persistent worktree run (`work` mode + effective `worktree` isolation), including Droid.

**Step 4: Fix bare prune summary**
Change command summary from `delegate worktree prune` to `delegate worktree prune --merged --dry-run` or add a note that prune requires `--merged` and/or `--older-than DAYS`.

**Step 5: Update stale-runtime handoff**
In `docs/live-runtime.md` and post-ship followups, keep a clear split:
- development command: `python3 bin/delegate.py`
- installed runtime promotion: explicit operator task only.

**Step 6: Update follow-up doc status**
Mark every item resolved by this plan with commit/task references once implementation lands.

**Step 7: Inventory lingering persistent worktrees**
Run:
```bash
python3 bin/delegate.py worktree list --no-auto-prune
```
Record alias list and disposition in `docs/plans/2026-05-25-post-ship-followups.md` §4.2 or in the coordinator handoff if no doc update is warranted. Remove/prune only through `python3 bin/delegate.py worktree remove ...` or `python3 bin/delegate.py worktree prune ...` after inspection; never manually delete `~/.delegate/worktrees/`.

**Verification plan:**
```bash
python3 -c "import json; json.load(open('config.example.json'))"
python3 bin/delegate.py --help
python3 bin/delegate.py agent-help
python3 bin/delegate.py --json describe
```
Expected:
- Help text and docs are consistent.
- Describe includes `isolation` and `worktrees`.

---

## Task 17: Full Integration Review and Gates

**Parallel:** no  
**Blocked by:** Tasks 2-16  
**Owned files:** all touched files  
**Invariants:** Review findings must be grounded in diff lines. Patch real issues before final readiness.  
**Out of scope:** Live runtime promotion unless Trey explicitly asks.

**Files:**
- Modify only as needed based on review findings.

**Step 1: Run narrow gates by area**
Run:
```bash
python3 -m unittest tests.test_delegate_worktree_mgmt
python3 -m unittest tests.test_delegate_execution
python3 -m unittest tests.test_delegate_parser
python3 -m unittest tests.test_delegate_validation
python3 -m unittest tests.test_run_registry
python3 -m unittest tests.test_runner_capture tests.test_snapshot_commands
```
Expected: all pass.

**Step 2: Run full canonical gate**
Run:
```bash
python3 -m unittest discover -s tests
```
Expected: `OK`.

**Step 3: Run static sanity checks**
Run:
```bash
git diff --check
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json dry-run cursor safe "review only"
python3 bin/delegate.py --json dry-run codex work "implement bounded task"
```
Expected: commands exit 0 and JSON parses.

**Step 4: Delegate clean-code review**
Run read-only review lanes:
```bash
python3 bin/delegate.py --json droid qwen safe --prompt-file docs/plans/prompts/worktree-followups-final-review-code.md
python3 bin/delegate.py --json cursor safe --prompt-file docs/plans/prompts/worktree-followups-final-review-cursor.md
```
Expected:
- No P0/P1/P2 correctness findings.
- Any P3 polish is either patched or recorded.

**Step 5: Patch review findings and rerun targeted gates**
For each real finding:
1. Patch the smallest relevant files.
2. Run targeted tests for those files.
3. Rerun full unittest if the finding touched shared code.
4. Rerun the reviewer or a focused reviewer prompt.

**Step 6: Final status**
Run:
```bash
git status --short --branch
git diff --stat
```
Expected:
- Only intended files are dirty.
- Summary includes verification commands and unresolved risks.

---

## Suggested Commit Plan

Commit only during implementation, not during plan review.

1. `worktree-mgmt: fix removal and prune correctness`
2. `isolation: harden worktree creation errors and git timeouts`
3. `cli: normalize worktree errors and request construction`
4. `tests: close worktree follow-up coverage gaps`
5. `refactor: simplify persistent worktree orchestration`
6. `refactor: simplify worktree management internals`
7. `docs: refresh worktree isolation examples and runtime handoff`

If the implementation run becomes too large, stop after commit 4 and run review before continuing refactors. Correctness beats completing every cleanup in one risky diff.

## Rollback / Safety Plan

- Because this pass touches core CLI paths, keep commits small enough to revert individually.
- Do not run `git reset --hard` or delete worktrees.
- After each Delegate work-mode subagent run, inspect with `python3 bin/delegate.py worktree show <alias>`, cherry-pick/merge only reviewed diffs, then retire the worktree intentionally. If integrating, use `python3 bin/delegate.py worktree remove <alias> --force-branch` only after the branch content is safely integrated. If discarding, use `python3 bin/delegate.py worktree remove <alias> --discard-uncommitted --force-branch` only after inspecting the diff. Do not let unreviewed unmerged worktrees accumulate.
- If Delegate persistent worktree runs create branches, inspect with:
```bash
python3 bin/delegate.py worktree list --no-auto-prune
python3 bin/delegate.py worktree show <alias>
```
- Remove only after integration:
```bash
python3 bin/delegate.py worktree remove <alias>
```
Use `--discard-uncommitted` or `--force-branch` only when the diff/branch has been inspected and intentionally discarded.

## Final Acceptance Criteria

- Every item in the issue coverage matrix is either fixed or explicitly reclassified with rationale.
- `python3 -m unittest discover -s tests` passes.
- `git diff --check` passes.
- `python3 -c "import json; json.load(open('config.example.json'))"` passes.
- `python3 bin/delegate.py --json describe` reports expected isolation/worktrees config.
- README, AGENTS, development docs, live-runtime docs, and config example agree on:
  - `python3 bin/delegate.py` for dev checkout use
  - no installed-runtime promotion without explicit request
  - Droid work uses `--skip-permissions-unsafe`
  - persistent worktree pass-through restriction is harness-agnostic
  - prune requires filters
- Final Delegate review loops report no blocking findings.

## Current Plan Review Status

- Drafted by Codex on 2026-05-25.
- Plan-reviewer subagent (`droid-49`) reviewed and approved with minor revisions; P1/P2/P3 clarifications patched.
- Cursor Composer Delegate review (`cursor-17`) reviewed and found two autonomous-execution blockers; sequencing, per-task execution ownership, path conventions, and docs/runtime cleanup steps patched.
- Focused Cursor recheck (`cursor-18`) found no remaining prior thematic blockers but flagged invalid `Execution:` shorthand and prompt-file sequencing; patched.
- Final Cursor recheck (`cursor-19`) reported: "Ready: no remaining P0/P1/P2 blockers." Optional P3 notes were patched for prompt scaffolding and Task 9/11 ordering.
