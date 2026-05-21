# Delegate Run Snapshots Implementation Plan

**Goal:** Build the WIP spec into Delegate Agent v1 run tracking: always-on workspace-local run registry, bounded parent-facing output, live snapshots, run listings, completion-report discovery, and explicit raw-output retrieval.

**Architecture:** Keep Delegate synchronous and local. Add small internal modules for run registry, harness stream parsing, and rendering while preserving `src/delegate_agent/cli.py` as the public CLI entrypoint. All implementation execution is delegated through the repo-local Delegate CLI to Cursor Composer; phase-end code review is delegated to GLM, and Cursor Composer resolves review feedback.

**Tech Stack:** Python standard library only, `unittest`, existing `delegate cursor work`, existing `delegate droid glm safe`, repo-local `python3 bin/delegate.py`.

---

## Delegation protocol for every worker prompt

Every sub-agent prompt in this plan must begin with:

> Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Use repo-local Delegate, not the installed live shim:

```bash
python3 bin/delegate.py --cwd /Users/treygoff/Code/delegate-agent cursor work --prompt-file .delegate-task-prompts/<prompt>.md
python3 bin/delegate.py --cwd /Users/treygoff/Code/delegate-agent droid glm safe --prompt-file .delegate-task-prompts/<prompt>.md
```

Do not mutate `~/.delegate`, `~/.local/bin/delegate`, or any installed live Delegate runtime during implementation.

## Wave 0: Feature branch, skill install, baseline

**Parallel:** no
**Blocked by:** none
**Owned files:** `docs/plans/2026-05-20-delegate-run-snapshots-implementation.md`, `.codex/skills/writing-plans`
**Invariants:** Begin work on a feature branch before implementation; do not promote or install the checkout into the live runtime.
**Out of scope:** Any code changes beyond plan/skill setup.

**Files:**
- Create: `docs/plans/2026-05-20-delegate-run-snapshots-implementation.md`
- Project-local skill link: `.codex/skills/writing-plans` (ignored local Codex config)

**Step 1: Create feature branch**
Run:

```bash
git switch -c codex/delegate-run-snapshots
```

Expected: branch `codex/delegate-run-snapshots` exists and `git status --short --branch` shows it checked out.

**Step 2: Find, install, and load writing plan skill**
Run:

```bash
codex-skill search -n writing-plans
codex-skill add writing-plans
codex-skill installed writing-plans
```

Expected: `writing-plans` is listed and project-installed for `/Users/treygoff/Code/delegate-agent`.

**Step 3: Baseline verification**
Run:

```bash
python3 -m unittest discover -s tests
```

Expected: all existing tests pass before implementation.

**Wave 0 review step**

No GLM code review is needed for Wave 0 because it creates only the plan and local ignored skill link. Confirm with `git status --short --branch`.

## Wave 1: Registry foundation, IDs, aliases, config precedence

**Parallel:** no
**Blocked by:** Wave 0
**Owned files:** `src/delegate_agent/run_registry.py`, `src/delegate_agent/config.py`, `src/delegate_agent/cli.py`, `tests/test_run_registry.py`, `tests/test_delegate_validation.py`, `tests/test_delegate_parser.py`
**Invariants:** Registry is workspace-local by default; aliases are exact handles and never reused; `.delegate/` is excluded via `.git/info/exclude`, not tracked `.gitignore`; global/user config remains supported.
**Out of scope:** Streaming subprocess changes and snapshot rendering.

**Files:**
- Create: `src/delegate_agent/run_registry.py`
- Create: `src/delegate_agent/config.py`
- Create: `tests/test_run_registry.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_validation.py`
- Modify: `tests/test_delegate_parser.py`

**Cursor Composer implementation prompt:** `.delegate-task-prompts/wave-1-registry.md`

Required prompt body:

```md
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Implement Wave 1 from docs/plans/2026-05-20-delegate-run-snapshots-implementation.md.

Scope:
- Add a registry module for project-local .delegate run state.
- Add stable run IDs like del_YYYYMMDDTHHMMSSZ_<hex>.
- Allocate exact, non-reused aliases per harness: cursor, cursor-2, droid, droid-2.
- Use an atomic claim operation, e.g. mkdir/O_EXCL, so alias allocation is race-safe.
- Maintain .delegate/index.json with alias and runId lookup.
- Ensure Git workspaces add .delegate/ to .git/info/exclude, never tracked .gitignore.
- Add config loading support for precedence: CLI flags > explicit DELEGATE_CONFIG > workspace-local .delegate/config.json > global ~/.delegate/config.json > embedded defaults. Preserve existing behavior when no workspace-local config exists.

Constraints:
- Do not change live ~/.delegate or installed delegate shims.
- Keep implementation standard-library only.
- Do not implement subprocess streaming yet.

Verification:
- python3 -m unittest tests.test_run_registry tests.test_delegate_validation tests.test_delegate_parser
- python3 -m unittest discover -s tests

Report:
- changed files
- tests run/results
- any deferred items
```

**Step 1: Write failing tests**
Tests must cover:
- run IDs match `del_YYYYMMDDTHHMMSSZ_<hex>`;
- first alias for `cursor` is `cursor`, second is `cursor-2`;
- exact lookup does not treat `cursor` as latest;
- `.git/info/exclude` receives `.delegate/`;
- workspace-local config overrides global config while preserving embedded defaults;
- explicit `DELEGATE_CONFIG` wins over workspace-local config.

**Step 2: Implement minimal registry/config code**
Add small pure functions/classes. Keep filesystem write logic isolated from CLI parsing.

**Step 3: Run Wave 1 verification**

```bash
python3 -m unittest tests.test_run_registry tests.test_delegate_validation tests.test_delegate_parser
python3 -m unittest discover -s tests
```

Expected: all pass.

**Wave 1 GLM review step**

Run:

```bash
python3 bin/delegate.py --cwd /Users/treygoff/Code/delegate-agent droid glm safe --prompt-file .delegate-task-prompts/wave-1-glm-review.md
```

Review prompt must start with the global-skill instruction and ask GLM to review the diff against `main` for:
- race safety,
- config precedence regressions,
- accidental live-runtime mutation,
- test adequacy,
- unnecessary complexity.

If GLM reports actionable issues, resolve them with Cursor Composer:

```bash
python3 bin/delegate.py --cwd /Users/treygoff/Code/delegate-agent cursor work --prompt-file .delegate-task-prompts/wave-1-fixes.md
```

Then rerun Wave 1 verification.

## Wave 2: Streaming runner, capture, bounded completion output

**Parallel:** no
**Blocked by:** Wave 1
**Owned files:** `src/delegate_agent/runner.py`, `src/delegate_agent/harness_events.py`, `src/delegate_agent/cli.py`, `tests/test_delegate_execution.py`, `tests/test_runner_capture.py`, `tests/test_harness_events.py`
**Invariants:** Default parent-facing output is bounded; raw harness output is stored locally, not streamed to the caller unless `--pass-through`; `--json --pass-through` is invalid; Delegate remains synchronous.
**Out of scope:** Full snapshot command rendering and archive retention.

**Files:**
- Create: `src/delegate_agent/runner.py`
- Create: `src/delegate_agent/harness_events.py`
- Create: `tests/test_runner_capture.py`
- Create: `tests/test_harness_events.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_execution.py`

**Cursor Composer implementation prompt:** `.delegate-task-prompts/wave-2-runner.md`

Required prompt body:

```md
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Implement Wave 2 from docs/plans/2026-05-20-delegate-run-snapshots-implementation.md.

Scope:
- Replace one-shot child execution for normal runs with streaming subprocess capture.
- Capture stdout.log and stderr.log under .delegate/runs/<runId>/.
- Update manifest.json, state.json, snapshot.json, events.jsonl incrementally using atomic JSON writes where appropriate.
- Preserve synchronous behavior: command exits when child exits.
- Change default text completion output to a bounded Delegate-owned summary with alias, status, snapshot command, and completion report command.
- Change JSON completion output to one concise Delegate-owned object without raw stdout/stderr.
- Add --pass-through as an explicit escape hatch preserving previous raw streaming behavior.
- Make --pass-through incompatible with --json.
- Add completion-report prompt injection by default for work/safe prompts, without mutating prompt files on disk.
- Support --completion-report markdown|none and --no-completion-report. CLI flags override config.

Constraints:
- Cursor safe isolation must keep reporting source cwd/executionCwd correctly.
- Do not expose raw tool outputs or full logs in default parent-facing output.
- Keep pass-through behavior available for exact legacy/raw use cases.

Verification:
- python3 -m unittest tests.test_delegate_execution tests.test_runner_capture tests.test_harness_events tests.test_delegate_parser
- python3 -m unittest discover -s tests

Report:
- changed files
- tests run/results
- any deferred items
```

**Step 1: Parser tests**
Add tests for:
- `--pass-through` valid in text mode;
- `--json --pass-through` invalid;
- `--completion-report none`;
- `--no-completion-report`;
- prompt-file injection is in memory only.

**Step 2: Runner tests**
Use fake binaries that emit stdout/stderr lines over time. Assert:
- logs are written;
- default text output omits raw `OUT:` / `ERR:`;
- JSON output omits raw stdout/stderr and includes `alias`, `runId`, `snapshotCommand`, `completionReportCommand`, bytes, duration;
- pass-through still exposes raw child output.

**Step 3: Harness event parser tests**
Add JSONL parser tests for Cursor/Droid-shaped events:
- assistant visible text is captured;
- reasoning/tool-result payloads are ignored;
- tool metadata targets are normalized;
- invalid JSON falls back to bounded text event.

**Step 4: Run Wave 2 verification**

```bash
python3 -m unittest tests.test_delegate_execution tests.test_runner_capture tests.test_harness_events tests.test_delegate_parser
python3 -m unittest discover -s tests
```

Expected: all pass.

**Wave 2 GLM review step**

Run GLM safe review with a prompt starting with the global-skill instruction. Ask for review of:
- token-safety of default output,
- subprocess streaming correctness/deadlock risks,
- prompt injection opt-out behavior,
- JSON contract stability,
- legacy compatibility via `--pass-through`.

Resolve actionable findings with Cursor Composer, then rerun Wave 2 verification.

## Wave 3: `runs`, `snapshot`, and `run-output` commands

**Parallel:** no
**Blocked by:** Wave 2
**Owned files:** `src/delegate_agent/rendering.py`, `src/delegate_agent/cli.py`, `src/delegate_agent/run_registry.py`, `tests/test_snapshot_commands.py`, `tests/test_delegate_parser.py`, `tests/test_run_registry.py`
**Invariants:** Snapshot/listing commands are bounded by default and never dump raw logs; exact alias lookup never guesses latest; raw retrieval requires explicit `run-output`.
**Out of scope:** Age-based archival.

**Files:**
- Create: `src/delegate_agent/rendering.py`
- Create: `tests/test_snapshot_commands.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `src/delegate_agent/run_registry.py`
- Modify: `tests/test_delegate_parser.py`
- Modify: `tests/test_run_registry.py`

**Cursor Composer implementation prompt:** `.delegate-task-prompts/wave-3-snapshot.md`

Required prompt body:

```md
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Implement Wave 3 from docs/plans/2026-05-20-delegate-run-snapshots-implementation.md.

Scope:
- Add delegate snapshot <alias-or-runId> and delegate --json snapshot <alias-or-runId>.
- Add delegate snapshot --latest <harness>.
- Add delegate runs with --active, --recent, --harness, --limit, and --json.
- Add delegate run-output <alias-or-runId> with --completion-report, --stdout, --stderr, --tail N, and --raw.
- Implement text and JSON renderers for snapshot/runs/run-output.
- Enforce bounded defaults: assistant text head+tail guard, event count head+tail guard, raw logs omitted by default.
- Include large-log warnings when stdout/stderr exceeds 50 MB.
- Redact obvious secrets in snapshot/runs metadata by default; support snapshot --no-redact.

Constraints:
- Do not add destructive prune/delete commands.
- Do not stream full logs through snapshot or runs.
- Keep exact lookup semantics: missing exact handles return suggestions, not guessed latest.

Verification:
- python3 -m unittest tests.test_snapshot_commands tests.test_run_registry tests.test_delegate_parser
- python3 -m unittest discover -s tests

Report:
- changed files
- tests run/results
- any deferred items
```

**Step 1: Parser tests**
Cover all new command shapes and invalid combinations.

**Step 2: Renderer tests**
Create fixture run directories and assert bounded text/JSON output for running, succeeded, failed, stale, latest, and missing handles.

**Step 3: Retrieval tests**
Assert `run-output` returns only requested completion/stdout/stderr material and honors `--tail`.

**Step 4: Run Wave 3 verification**

```bash
python3 -m unittest tests.test_snapshot_commands tests.test_run_registry tests.test_delegate_parser
python3 -m unittest discover -s tests
```

Expected: all pass.

**Wave 3 GLM review step**

Run GLM safe review with a prompt starting with the global-skill instruction. Ask for review of:
- raw-output exposure mistakes,
- alias lookup ambiguity,
- renderer bounds/redaction,
- command UX and error messages,
- test gaps.

Resolve actionable findings with Cursor Composer, then rerun Wave 3 verification.

## Wave 4: Retention/archive pass and documentation/skill surface

**Parallel:** no
**Blocked by:** Wave 3
**Owned files:** `src/delegate_agent/retention.py`, `src/delegate_agent/cli.py`, `tests/test_retention.py`, `README.md`, `CONTEXT.md`, `docs/development.md`, `docs/live-runtime.md`, `.codex/skills/delegate-agent/SKILL.md`
**Invariants:** Retention is archive-only; active runs are never archived; docs distinguish repo checkout from live installed runtime.
**Out of scope:** Installing/promoting the changed CLI into `~/.delegate` or `~/.local/bin`.

**Files:**
- Create: `src/delegate_agent/retention.py`
- Create: `tests/test_retention.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `docs/development.md`
- Modify: `docs/live-runtime.md`
- Modify: `.codex/skills/delegate-agent/SKILL.md` if tracked and present; otherwise update the repo documentation that instructs agents.

**Cursor Composer implementation prompt:** `.delegate-task-prompts/wave-4-retention-docs.md`

Required prompt body:

```md
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Implement Wave 4 from docs/plans/2026-05-20-delegate-run-snapshots-implementation.md.

Scope:
- Add opportunistic archive-only retention pass for completed runs older than the raw-log retention window.
- Preserve lightweight manifests/state/snapshot/completion reports and index lookup.
- Archive raw stdout.log, stderr.log, and events.jsonl into .delegate/archive/<runId>.tar.gz with stdlib tarfile/gzip.
- Do not add delete/prune commands.
- Update README and docs for:
  - default bounded output,
  - --pass-through,
  - snapshot/runs/run-output usage,
  - completion-report injection and opt-out,
  - local .delegate registry,
  - retention/archive behavior,
  - not promoting repo changes to the live runtime.
- Update Delegate agent-facing skill/docs so orchestrating agents know to use snapshot/runs instead of tailing raw logs.

Constraints:
- No live runtime mutation.
- Keep docs concise and command-driven.

Verification:
- python3 -m unittest tests.test_retention tests.test_snapshot_commands tests.test_delegate_execution
- python3 -m unittest discover -s tests

Report:
- changed files
- tests run/results
- any deferred items
```

**Step 1: Retention tests**
Assert:
- active/running runs are skipped;
- old completed raw logs are archived;
- lightweight metadata remains readable;
- snapshot still works after archival;
- no irreversible delete behavior exists.

**Step 2: Docs update**
Update user-facing and agent-facing docs with examples that avoid raw stream passthrough by default.

**Step 3: Run Wave 4 verification**

```bash
python3 -m unittest tests.test_retention tests.test_snapshot_commands tests.test_delegate_execution
python3 -m unittest discover -s tests
```

Expected: all pass.

**Wave 4 GLM review step**

Run GLM safe review with a prompt starting with the global-skill instruction. Ask for review of:
- archival safety,
- docs/behavior mismatches,
- live-runtime boundary violations,
- missing test coverage.

Resolve actionable findings with Cursor Composer, then rerun Wave 4 verification.

## Wave 5: End-to-end dogfood and final review/fix loop

**Parallel:** no
**Blocked by:** Wave 4
**Owned files:** `tests/test_end_to_end_tracking.py`, any files touched by review fixes
**Invariants:** Dogfood must use fake harness binaries or controlled repo-local commands; do not require real Cursor/Droid auth for automated tests; final Codex review is against `main`.
**Out of scope:** Commit, push, release, or live install unless Trey separately asks.

**Files:**
- Create: `tests/test_end_to_end_tracking.py`
- Modify: any files needed for review fixes.

**Cursor Composer implementation prompt:** `.delegate-task-prompts/wave-5-e2e.md`

Required prompt body:

```md
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/documentation skills when available. Report which skills you loaded or that none were available.

Implement Wave 5 from docs/plans/2026-05-20-delegate-run-snapshots-implementation.md.

Scope:
- Add end-to-end tests using fake cursor/droid binaries and temp config.
- Verify a tracked run creates registry files, bounded default output, snapshot output, runs listing, completion report retrieval, raw stdout/stderr retrieval by explicit command, and pass-through legacy behavior.
- Run final full unit suite.

Verification:
- python3 -m unittest tests.test_end_to_end_tracking
- python3 -m unittest discover -s tests
- git diff --check

Report:
- changed files
- tests run/results
- remaining risks
```

**Wave 5 GLM review step**

Run GLM safe review with a prompt starting with the global-skill instruction. Ask for review of:
- end-to-end realism,
- command contract consistency,
- failure/stale handling,
- any untested spec requirement.

Resolve actionable findings with Cursor Composer, then rerun:

```bash
python3 -m unittest discover -s tests
git diff --check
```

## Final native Codex sub-agent review

After all waves pass, spawn a dedicated Codex code-review sub-agent against the full diff from `main`.

Sub-agent prompt must begin with:

```text
Before you begin, search and load relevant skills from your global skills library that can improve code quality or support this specific task. Prefer clean-code/refactoring/testing/security skills when available. Report which skills you loaded or that none were available.
```

Ask the reviewer to inspect:
- full `git diff main...HEAD`,
- default output token safety,
- registry race safety and stale-state behavior,
- config precedence and live-runtime isolation,
- snapshot/run-output raw data boundaries,
- tests and docs.

The reviewer is read-only. Any actionable findings are fixed by the orchestrator personally, using Cursor Composer through Delegate only if the fix is non-trivial. After fixes, rerun:

```bash
python3 -m unittest discover -s tests
git diff --check
```

## Final acceptance checklist

- Feature branch exists: `codex/delegate-run-snapshots`.
- `writing-plans` is project-installed and loaded for plan writing.
- No mutation to `~/.delegate` live runtime or installed delegate shim.
- Default run output is bounded and does not dump raw harness streams.
- `--pass-through` is explicit and incompatible with `--json`.
- Run registry is workspace-local under `.delegate/`.
- `.delegate/` is added to `.git/info/exclude`, not tracked `.gitignore`.
- `delegate runs`, `delegate snapshot`, and `delegate run-output` work in text and JSON where specified.
- Completion-report injection is default-on and can be disabled.
- GLM review/fix loop completed at each implementation wave.
- Final Codex clean-code review completed and findings addressed.
- `python3 -m unittest discover -s tests` passes.
- `git diff --check` passes.
