# Delegate Work-Mode Isolation Spec

## Vision and goal

Delegate should support a worktree-first agent development workflow without surprising existing users. The source checkout should be able to remain the orchestration and review surface, while edit-capable agents work in isolated Git worktrees that are preserved for inspection, testing, merge, cherry-pick, or deletion.

The immediate goal is to add first-class isolation controls around `delegate --isolation worktree ...`, especially for `work` mode, **plus** a complete worktree management surface (`delegate worktree list|show|remove|prune|gc`). Persistent artifacts without first-class retirement is a footgun for agent-driven workflows; this spec ships creation and retirement together. The embedded default remains backward-compatible for now; power users can opt into worktree defaults through config after the UX is dogfooded.

This feature is about **workspace placement**, not sandboxing. A Git worktree prevents normal relative-path edits from landing in the source checkout, but it does not stop a child runtime from using absolute paths, credentials, shell commands, or network access that are otherwise available to it.

## Background

Current Delegate behavior mixes two separate concepts:

- **Mode**: `safe` vs `work`, meaning read/review posture vs edit-capable posture.
- **Workspace containment**: source checkout vs isolated execution workspace.

Today:

- `delegate cursor safe` and `delegate codex safe` run in a temporary isolated workspace.
- `delegate droid safe` runs in the real source workspace with Droid's read-oriented defaults.
- All `work` modes run in the real source workspace.

This is understandable historically, but agents and humans naturally try commands like `delegate --isolation worktree ...` because they want edit-capable work without contaminating the orchestrator checkout. Delegate should make that workflow explicit.

## Decision summary

Add isolation as a first-class execution dimension, paired with a worktree management surface.

Primary MVP examples:

```bash
delegate --isolation worktree cursor work "Implement the fix and run the focused test."
delegate --isolation worktree codex work "Try a parser cleanup in an isolated branch."
delegate --isolation worktree droid qwen work "Make the bounded change and report verification."

delegate worktree list
delegate worktree show cursor-4
delegate worktree remove cursor-4
delegate worktree prune --merged
```

Add config defaults:

```json
{
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
}
```

`work = none` remains the embedded default for compatibility. This repository or a user's machine can later set `work = worktree` to dogfood the feature without changing public defaults. `worktrees.dataHome = null` keeps the embedded `~/.delegate/worktrees` default; teams can override.

## Terminology

- **Source workspace**: the workspace after Delegate's normal `--cwd` / Git-root resolution.
- **Execution workspace**: the actual directory passed to the child agent.
- **Isolation override**: explicit CLI or run-input value: `auto`, `none`, or `worktree`.
- **Effective isolation mode**: the final execution decision after CLI/input/config/default resolution.
- **Temporary isolation**: an execution workspace removed after the run.
- **Persistent isolation**: an execution workspace preserved after the run.
- **Persistent worktree run**: `mode == work` and effective isolation is `worktree`.

## Isolation values

### `auto`

Use Delegate's legacy behavior for the engine/mode pair.

| Command shape | Execution behavior |
| --- | --- |
| `cursor safe` | temporary isolated workspace |
| `codex safe` | temporary isolated workspace |
| `droid safe` | source workspace |
| any `work` | source workspace |

`auto` is intentionally an explicit way to request legacy behavior. If a user configures `isolation.work = "worktree"`, then omitting `--isolation` uses the config, while passing `--isolation auto` bypasses that config for the one command.

### `none`

Force the source workspace as the execution workspace.

Allowed for all engines and modes. For `cursor safe` / `codex safe`, this disables today's hard isolation boundary and must be visible in dry-run/JSON metadata. Cursor safe with `none` must **not** write `.cursor/cli.json` into the source workspace; that defense-in-depth file is only allowed inside isolated temp workspaces.

### `worktree`

Require a Git source workspace and create a separate Git worktree.

- `work` mode: persistent worktree, preserved after child exit.
- `safe` mode: temporary worktree, cleaned up after child exit.

`worktree` fails clearly in non-Git workspaces. Public `copy` isolation is deferred.

## Effective isolation resolution

Use one resolution function for all entry points:

```text
resolve_isolation(cli_value, input_json_value, loaded_config, engine, mode)
```

Algorithm:

1. Validate any CLI `--isolation` value.
2. Validate any run input JSON `isolation` value.
3. If CLI value is present, use it.
4. Else if run input JSON value is present, use it.
5. Else use `loaded_config.isolation[mode]` when present.
6. Else use embedded default: `safe = auto`, `work = none`.
7. Map the resulting value to execution behavior:
   - `auto`: use legacy behavior matrix.
   - `none`: use source workspace.
   - `worktree`: use Git worktree behavior.

Loaded config precedence remains Delegate's existing config precedence. The `isolation` key in run input JSON is **not** a config layer; it is a per-run request value below CLI and above loaded config.

## CLI interface

Global option, before the subcommand:

```bash
delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] cursor {safe,work} ...
delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] droid MODEL {safe,work} ...
delegate [--cwd PATH] [--json] [--isolation auto|none|worktree] codex {safe,work} ...
```

Dry-run supports the same option:

```bash
delegate --json --isolation worktree dry-run cursor work "..."
```

Invalid placement must fail with `misplaced_global_option`; this flag is operationally significant enough that `delegate cursor work --isolation worktree "..."` must not be silently treated as prompt text.

## Run input JSON interface

Add optional key:

```json
{
  "engine": "cursor",
  "mode": "work",
  "cwd": "/path/to/repo",
  "isolation": "worktree",
  "prompt": "Implement the bounded fix and report changed files."
}
```

Rules:

- Add `isolation` to `RUN_INPUT_KEYS`.
- Unknown isolation values fail before launch.
- CLI `--isolation` overrides run input JSON `isolation`.
- Existing run input JSON without `isolation` remains valid.

### Config loading for run input JSON

For `delegate run --input-json FILE`, workspace-local config must be loaded from the same resolved workspace that will execute the run.

Implementation rule:

1. Parse CLI global options first.
2. If subcommand is `run`, read only the minimal JSON fields needed for config discovery: `cwd` and `isolation`. Do not validate model aliases yet.
3. Resolve workspace from CLI `--cwd` plus JSON `cwd` using the same ambiguity rule as normal request construction.
4. Load config with that resolved workspace.
5. Build the full request and validate the full JSON input.

Tests:

- JSON `cwd` pointing at a repo with `.delegate/config.json` containing `{ "isolation": { "work": "worktree" } }` makes omitted CLI isolation resolve to worktree.
- CLI `--cwd` and JSON `cwd` conflict still fails before launch.
- CLI `--isolation auto` overrides JSON/config and restores legacy behavior.

## Config interface

Add two top-level config sections:

```json
{
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
}
```

Validation:

- `isolation` must be an object when present.
- `isolation.safe` and `isolation.work` must be one of `auto`, `none`, `worktree`.
- `worktrees` must be an object when present.
- `worktrees.dataHome` must be null or a non-empty string (absolute path or `~/`-prefixed). When set, replaces `~/.delegate/worktrees` as the persistent-worktree root. Tilde expansion uses `Path.expanduser()`.
- `worktrees.autoPrune.enabled` must be a boolean.
- `worktrees.autoPrune.mergedOlderThanDays` must be a non-negative integer.
- Missing fields use embedded defaults.

## Behavior matrix

With embedded defaults:

| Engine/mode | No flag | `--isolation none` | `--isolation worktree` |
| --- | --- | --- | --- |
| `cursor safe` | temp isolated workspace | source workspace, no `.cursor/cli.json` write | temp Git worktree |
| `codex safe` | temp isolated workspace + Codex read-only sandbox | source workspace + Codex read-only sandbox | temp Git worktree + Codex read-only sandbox |
| `droid safe` | source workspace | source workspace | temp Git worktree |
| `cursor work` | source workspace | source workspace | persistent Git worktree |
| `codex work` | source workspace + configured work sandbox | source workspace + configured work sandbox | persistent Git worktree + configured work sandbox |
| `droid work` | source workspace | source workspace | persistent Git worktree |

## Persistent worktree behavior for `work` mode

When mode is `work` and effective isolation is `worktree`:

1. Resolve the source workspace normally.
2. Require `workspaceKind == git`.
3. Require a valid source `HEAD` and capture creation-time integration state (see below).
4. Reject `--pass-through` in the MVP.
5. Ensure Delegate registry/exclude metadata needed for this run.
6. Run the strict clean-source check from the source Git root.
7. Register the run and immediately write pre-launch state.
8. Create a local branch and persistent Git worktree from the captured `HEAD` commit.
9. Rewrite the child argv workspace argument to the worktree path.
10. Prepend the persistent-worktree context note to the child prompt.
11. Launch the child agent in the worktree.
12. Preserve the worktree and branch regardless of child exit code.
13. Record source/execution/isolation metadata in the registry and completion output.

Delegate registry metadata is the only permitted source-workspace mutation before launch. The clean-source check must not ignore ordinary untracked files merely because they are under the source checkout.

### Creation-time integration state

At step 3 the registry must capture, in `creationContext`:

- `sourceHeadOid`: full SHA from `git rev-parse HEAD` in the source.
- `sourceHeadRef`: `git symbolic-ref --quiet HEAD` (e.g., `refs/heads/main`) or `null` if detached.
- `sourceBranch`: short branch name when `sourceHeadRef` resolves, else `null`.
- `sourceGitCommonDir`: result of `git rev-parse --git-common-dir`, used for fingerprint and management commands.

These fields are immutable for the lifetime of the run. Worktree management commands distinguish two reference points:

- **Creation base**: `creationContext.sourceHeadOid`. Always available.
- **Current source HEAD**: re-read each time `worktree list/show` runs. May have advanced, rewound, or switched branches since creation.

`mergedIntoSource` is true when the branch tip is reachable from **current source HEAD**. `aheadBehind` reports two pairs: `vsCreationBase` and `vsCurrentHead`. Suggested-commands strings always state which reference point they use, e.g., `delegate worktree show` prints `branch ahead 3 / behind 0 vs creation base (abc1234); ahead 3 / behind 2 vs current HEAD (def5678)`.

Detached source HEAD at creation is allowed (legitimate during bisect, rebase, code review). When `sourceHeadRef` is `null`:

- The persistent worktree run still succeeds.
- `mergedIntoSource` and `vsCurrentHead` are computed against the current source HEAD as usual.
- Suggested-commands strings include the warning `source was detached at creation; integration target unknown`.
- `worktree prune --merged` skips entries whose `creationContext.sourceHeadRef` is `null` unless `--include-detached` is passed, since "merged into source" is ambiguous without a target branch.

Source HEAD checked out in a different linked worktree (Git rejects branch checkouts that conflict with existing worktrees) is handled at branch creation: if `git worktree add` fails with `branch already checked out`, surface the underlying Git error as `worktree_create_failed`.

### Empty Git repositories

`--isolation worktree` requires a valid `HEAD`.

If the source workspace is a Git repository with no commits, fail before branch or worktree creation:

```text
missing_git_head: --isolation worktree requires a Git workspace with at least one commit.
```

Tests:

- Clean but unborn Git repo fails with `missing_git_head`.
- The failure does not create a branch, worktree, or child process.

### Clean source definition

A source workspace is clean when this command returns no output from the source Git root:

```bash
git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none
```

This rejects staged changes, unstaged tracked changes, untracked files, and submodule dirtiness. Ignored files are allowed. This strict rule keeps persistent worktree diffs attributable to the child run.

User-facing docs should call out that an untracked prompt file or `task.json` inside the source repository will make persistent worktree mode fail the clean-source check. Users should keep prompt/input files outside the repo, commit them intentionally, or run in-place with `--isolation none`.

Dirty-source error:

```text
dirty_source_workspace: --isolation worktree for work mode requires a clean source workspace. Commit/stash/delete local changes, run in-place with --isolation none, or wait for a future --include-dirty option.
```

Safe-mode temporary isolation can continue using the current dirty snapshot behavior because the temporary workspace is not an integration artifact.

### Branch naming

Use branch names based on run id, not alias, to avoid registry alias/path ordering ambiguity and branch collisions:

```text
delegate/<label>-<short-run-id>
```

Label rules:

- Cursor: `cursor`
- Codex: `codex`
- Droid: `droid-<model-alias-slug>`

Examples:

```text
delegate/cursor-20260524T184455Z-a1b2c3
delegate/codex-20260524T184501Z-b2c3d4
delegate/droid-qwen-20260524T184507Z-c3d4e5
```

Live Delegate run ids have the form:

```text
del_YYYYMMDDTHHMMSSZ_<hex>
```

Define `<short-run-id>` as the run id with the `del_` prefix removed and all characters outside `[A-Za-z0-9._-]` replaced by `-`, then truncated to at most 32 characters.

Example:

```text
run id:       del_20260524T184455Z_a1b2c3
short id:     20260524T184455Z-a1b2c3
branch:       delegate/cursor-20260524T184455Z-a1b2c3
worktree dir: cursor-20260524T184455Z-a1b2c3
```

Do not change the run id format as part of this feature.

Requirements:

- Branch names must be valid Git refs.
- Branches are local-only.
- Delegate never auto-merges, auto-commits, or auto-pushes.
- If the generated branch already exists, fail before launch with `branch_collision`. Never silently reuse or suffix a branch — the run id is already time+hex unique, so a collision indicates registry corruption or a clock anomaly worth surfacing.

### Worktree path

Use a Delegate data-home path outside the source working tree:

```text
~/.delegate/worktrees/<repo-fingerprint>/<label>-<short-run-id>/
```

Rationale:

- Avoids Git worktree restrictions around nesting a worktree inside another worktree.
- Keeps preserved agent work separate from source checkout clutter.
- Still makes the path discoverable through run metadata, snapshots, and completion output.

`repo-fingerprint` is computed as `hashlib.sha256(resolved_path.encode("utf-8")).hexdigest()[:12]` where `resolved_path` is `Path(git_common_dir).resolve(strict=True).as_posix()`. The Git common directory (returned by `git rev-parse --git-common-dir`) is used so secondary worktrees of the same repo share a fingerprint and don't fragment storage. Tests must cover paths with spaces, unicode, symlinks, and two unrelated repos producing distinct fingerprints.

Do not place persistent worktrees under `/tmp`, because users lose work and cleanup semantics become ambiguous.

Tests must never write to the real `~/.delegate`. Every subprocess execution test that can create `~/.delegate/worktrees` must set `HOME` to a `TemporaryDirectory`, assert the produced worktree path is under that temporary home, and avoid the installed `delegate` shim/live runtime.

### Prompt context note

Persistent worktree runs must prepend this note after the mandatory skill-review prefix and before the user's prompt:

```text
You are running in a Delegate-created isolated Git worktree. Make changes in this execution workspace only. Do not attempt to modify, merge into, or clean the source checkout. Do not delete, rename, or `git worktree remove` this workspace; the orchestrator manages worktree lifecycle. Report changed files, verification, and suggested integration steps. Your orchestrator can inspect this run via `delegate worktree show <alias>` and retire it via `delegate worktree remove <alias>` (refuses on dirty or unmerged), `delegate worktree remove <alias> --force-branch` (allow unmerged-branch deletion), `delegate worktree remove <alias> --discard-uncommitted` (DISCARDS uncommitted edits), or `delegate worktree prune --merged` for bulk integrated entries.
```

This note is required for all engines in persistent worktree runs. It must not claim the run is read-only.

## Temporary worktree behavior for `safe` mode

When mode is `safe` and effective isolation is `worktree`:

- Require Git source workspace.
- Create a temporary detached Git worktree.
- Mirror the source dirty snapshot using the existing safe-mode snapshot behavior where applicable.
- For Cursor, write `.cursor/cli.json` only inside the temporary worktree.
- Clean up the temporary worktree after the child exits.

When mode is `safe` and effective isolation is `auto`, preserve current behavior exactly, including directory-copy fallback for non-Git Cursor/Codex safe runs.

When mode is `safe` and effective isolation is `none`, run in the source workspace and do not write temporary isolation files into the source workspace.

### Safe + worktree + pass-through dispatch order

`--pass-through` is allowed with `safe` + `worktree`. The existing pass-through path (`runner.execute_passthrough` style) bypasses the registry-centered bounded-output flow, but safe-mode worktree isolation still needs the temporary worktree created before the child runs and cleaned up after it exits.

Required dispatch order in `cli.execute_request` (and any future generalization):

1. Resolve isolation → `effectiveIsolation = "worktree"`, `mode = "safe"`, `passThrough = True`.
2. Enter a `safe_isolated_request`-style context manager that creates the temporary worktree (and mirrors the dirty snapshot) **before** branching into the pass-through code path. The context manager's `finally` block performs cleanup regardless of which exit path the child takes.
3. Inside that context, dispatch to pass-through child invocation.
4. On exit (success, failure, or signal), the context manager removes the worktree and temp base.

This means safe+worktree+pass-through skips bounded output (as today's pass-through does) but does **not** skip isolation lifecycle. Persistent worktree + pass-through is still rejected up-front as today's spec already requires.

## Dry-run behavior

Dry-run must not create branches, worktrees, registry runs, or filesystem artifacts.

For persistent worktree runs, dry-run should show planned metadata with placeholders:

```json
{
  "effectiveIsolation": "worktree",
  "isolationMode": "worktree",
  "isolationLifecycle": "persistent",
  "preservedWorkspace": true,
  "cwd": "/source/path",
  "plannedExecutionCwd": "~/.delegate/worktrees/<repo-fingerprint>/<label>-<short-run-id>",
  "plannedBranch": "delegate/<label>-<short-run-id>"
}
```

Dry-run argv should either show the planned placeholder execution path or explicitly mark the workspace argument as planned. It must not claim a real `executionCwd` exists.

## JSON and output metadata

Do not repurpose the existing `isolation` JSON field from a human-readable sentence into an enum in the same release. To avoid breaking existing tests or orchestrators:

- Keep `isolation` as a human-readable note when present.
- Add `isolationMode`: `auto`, `none`, or `worktree`.
- Add `effectiveIsolation`: final behavior value after `auto` mapping where useful.
- Add `isolationLifecycle`: `none`, `temporary`, or `persistent`.
- Add `preservedWorkspace`: boolean.
- Add `executionCwd`: actual path for real runs.
- Add `plannedExecutionCwd`: placeholder path for dry-runs.
- Add `branch`: actual branch for real persistent worktree runs.
- Add `plannedBranch`: placeholder branch for dry-runs.

Example completion output for a persistent worktree run:

```text
delegate run cursor completed in 12m31s
alias: cursor-4
status: succeeded
source: /Users/treygoff/Code/example
execution: /Users/treygoff/.delegate/worktrees/a1b2c3d4/cursor-20260524T184455Z-a1b2c3
branch: delegate/cursor-20260524T184455Z-a1b2c3
isolation: worktree persistent
snapshot: delegate snapshot cursor-4
inspect: git -C /Users/treygoff/.delegate/worktrees/a1b2c3d4/cursor-20260524T184455Z-a1b2c3 status --short
review diff: git -C /Users/treygoff/.delegate/worktrees/a1b2c3d4/cursor-20260524T184455Z-a1b2c3 diff --stat HEAD
cleanup after integrating or discarding: git -C /Users/treygoff/Code/example worktree remove --force /Users/treygoff/.delegate/worktrees/a1b2c3d4/cursor-20260524T184455Z-a1b2c3 && git -C /Users/treygoff/Code/example branch -D delegate/cursor-20260524T184455Z-a1b2c3
```

The `review diff` command intentionally uses `HEAD`, not `main...HEAD`, because many agent work runs leave uncommitted changes. If a future workflow asks agents to commit, Delegate can add a base-ref diff helper later.

## Registry and snapshot changes

Run context should track all isolated runs, not only safe-mode isolated runs:

- source cwd
- source Git common dir (recorded for persistent worktree runs; required so worktree management commands can locate the owning repo without relying on the user's current `--cwd`)
- execution cwd
- requested isolation value
- effective isolation mode
- isolation lifecycle
- preserved workspace boolean
- branch name when applicable
- worktree path when applicable
- `worktreeStatus`: one of `present`, `removed`, `missing`, `unknown` (set lazily by `worktree list/show/gc`; `present` is the value at run completion)
- `worktreeRemovedAt` (UTC ISO timestamp set by `worktree remove` / `worktree prune`)

Snapshots should expose these fields in JSON and compact text. `delegate runs` should remain concise but mark persistent isolated work runs clearly enough that users can find preserved worktrees.

Raw-log retention must not delete preserved worktrees. Worktree cleanup is a separate user action, not part of log retention.

### Persistent worktree pre-launch state

Persistent worktree isolation registers a run before child launch so the branch/path can be tied to a stable run id. That creates a pre-launch failure case that must be inspectable.

Rules:

- Immediately after registration, write a state/snapshot with status `creating_isolation`.
- If branch/worktree creation fails after registration, write final state `failed`, include `error`, `message`, `plannedBranch`, and `plannedExecutionCwd`, and return a normal Delegate error payload.
- If cleanup of a partial branch/worktree is unsafe or fails, preserve the path, record it in the snapshot, and print manual cleanup instructions.

Acceptance tests:

- Simulated branch/worktree creation failure leaves `delegate snapshot <alias>` with failed status and the creation error.
- No successful child process is required for the failed pre-launch run to be inspectable.

## Pass-through behavior

MVP rule: `--pass-through` is unsupported with persistent worktree runs.

Reason: pass-through currently skips the registry-centered output path, but persistent worktrees need a run id, branch name, metadata, and cleanup instructions. Failing fast avoids orphaning worktrees without aliases or snapshots.

Allowed:

- `--pass-through` with current default work mode (`isolation none` / `auto` legacy).
- `--pass-through` with temporary safe isolation as currently supported.

Rejected:

```bash
delegate --pass-through --isolation worktree cursor work "..."
```

## Cleanup UX

Completion output and snapshots must show explicit manual cleanup commands **and** call out the structured equivalents (`delegate worktree remove <alias>` / `delegate worktree prune ...`) so agents have a single discoverable verb rather than memorizing raw `git worktree` invocations.

Because persistent work runs usually leave uncommitted changes, raw `git worktree remove` lines printed in completion output use the `--force` form, but docs and the structured commands distinguish safe removal (refuse on dirty) from forced removal (discards). The default `delegate worktree remove` refuses on dirty; explicit `--force` discards. See the next section for the full management surface.

## Worktree management commands

A new top-level subcommand tree owns persistent-worktree lifecycle for `work`-mode runs. All commands operate on the current workspace's registry (`<workspace>/.delegate/`) and resolve handles using the existing alias/runId rules. Cross-repo management is a follow-up; for now, run management commands from the workspace that spawned the run.

### Shared rules

- Commands fail with `no_registry` when the current workspace has no `.delegate/` registry.
- Commands fail with `unknown_handle` when the handle resolves nothing, and surface the same alias suggestions as `delegate snapshot`.
- Commands fail with `not_worktree_run` when the resolved run is not a persistent worktree run.
- Removal/prune mutations take the `registry_lock` for the duration of registry writes.
- Commands never run `git fetch` and never touch the network.
- Commands never mutate the source workspace beyond Delegate registry metadata. Note: `<workspace>/.delegate/` writes (including the opportunistic prune triggered by `worktree list`) count as Delegate registry metadata, not source-workspace mutations. The strict invariant is that no source-tracked or source-untracked path outside `<workspace>/.delegate/` is touched.
- Tests that exercise these commands set `HOME` to a `TemporaryDirectory` and assert no real `~/.delegate` mutation, same as creation tests.
- `--json` mode is supported on every command and emits the schemas listed below.

### Concurrency model

`list` and `show` are advisory snapshots computed **without** the registry lock. Their `worktreeStatus`, `dirty`, `mergedIntoSource`, and `aheadBehind` fields can be stale by the time the caller acts on them. Every entry includes a `computedAt` UTC timestamp so callers can reason about freshness.

`remove`, `prune`, and `gc` re-read the run state and re-run the dirty/merged/status checks **under the registry lock** immediately before any Git mutation. A stale advisory from `list` cannot cause a wrong mutation; at worst it causes a now-redundant command to no-op gracefully (e.g., a second `remove` sees `worktreeStatus = removed` and exits success without invoking Git).

If a Git mutation fails because the underlying state changed between revalidation and the Git call (extremely narrow window), surface the underlying Git error in the error payload and leave the registry untouched.

### Error payload schema

Every management command that can fail emits an error envelope with this stable shape (subset of fields populated per error code):

```json
{
  "ok": false,
  "code": "dirty_worktree",
  "message": "Worktree has 3 uncommitted changes; pass --discard-uncommitted to remove anyway.",
  "alias": "cursor-4",
  "runId": "del_20260524T184455Z_a1b2c3",
  "branch": "delegate/cursor-20260524T184455Z-a1b2c3",
  "executionCwd": "/Users/treygoff/.delegate/worktrees/abc123def456/cursor-20260524T184455Z-a1b2c3",
  "sourceGitRoot": "/Users/treygoff/Code/example",
  "dirtyPaths": [" M src/foo.py", "?? scratch.txt"],
  "nextActions": [
    "delegate worktree show cursor-4",
    "delegate worktree remove cursor-4 --discard-uncommitted"
  ],
  "retrySafe": false
}
```

Field rules:

- `code` is one of the codes listed under "Error handling" (single source of truth); never invent ad-hoc strings.
- `nextActions` is an ordered list of structured suggestions; agents should prefer these over parsing `message`.
- `retrySafe` is true when re-invoking the same command verbatim could succeed (e.g., transient lock contention). False when the user must change something first.
- `dirtyPaths` is capped at 20 entries; if truncated, append a `…` sentinel and include `dirtyPathsTotal: N`.

### `delegate worktree list`

```bash
delegate [--cwd PATH] [--json] worktree list [--harness HARNESS] [--status STATUS] [--limit N] [--no-auto-prune]
```

Lists persistent-worktree runs from the current workspace registry. Defaults: all harnesses, all statuses, limit 20 (same default as `delegate runs`).

For each entry, compute lazily (without taking the registry lock — see "Concurrency model" above):

- `alias`, `runId`, `harness`, `branch`, `executionCwd`, `sourceGitRoot`, `createdAt`, `lastActivityAt`, `computedAt`
- `worktreeStatus`: `present` (path + branch both exist), `removed` (registry says removed), `missing` (registry says present but path no longer exists on disk), or `unknown`
- `dirty`: tri-state — `true`, `false`, or `null`. Only attempted for `present` entries; result of `git -C <executionCwd> status --porcelain=v1 --untracked-files=normal --ignore-submodules=none`. `null` when git is unavailable or the command errors; the underlying error is recorded in the entry's `warnings` list.
- `mergedIntoSource`: tri-state — `true`, `false`, or `null`. Only attempted for `present` entries; computed via `git -C <sourceGitRoot> merge-base --is-ancestor <branch> HEAD`. `null` when git errors or when `creationContext.sourceHeadRef` is null and `--include-detached` was not passed.

`lastActivityAt` is the run's last state-file write timestamp (set by the runner during execution and frozen at child exit). Management commands never update `lastActivityAt`; it is immutable after the child exits.

Sort by `lastActivityAt` descending. Filter via `--harness` and `--status`. JSON schema: `delegate.worktree-list.v1`.

`--no-auto-prune` disables the opportunistic prune pass for this invocation even when `worktrees.autoPrune.enabled = true`. Use when a read-only orchestrator must guarantee no registry mutation.

Text output mirrors `delegate runs`:

```text
alias        status   harness  age      branch                                              dirty merged
cursor-4     present  cursor   2h13m    delegate/cursor-20260524T184455Z-a1b2c3              yes   no
codex-2      missing  codex    1d4h     delegate/codex-20260524T184501Z-b2c3d4               -     -
droid-qwen-1 removed  droid    3d2h     delegate/droid-qwen-20260524T184507Z-c3d4e5          -     -
```

### `delegate worktree show`

```bash
delegate [--cwd PATH] [--json] worktree show <alias-or-runId>
delegate [--cwd PATH] [--json] worktree show --latest HARNESS
```

Deep view of a single persistent worktree. Reuses `resolve_run_target` for handle resolution.

Fields returned (JSON schema `delegate.worktree-show.v1`):

- All fields from `worktree list` plus:
- `creationContext`: `{ sourceHeadOid, sourceHeadRef, sourceBranch, sourceGitCommonDir }` captured at run launch.
- `porcelainStatus`: first 50 lines of `git status --porcelain=v1` inside the worktree, plus `porcelainStatusTruncated: bool` and `porcelainStatusTotalLines: int`. `null` if the path is missing.
- `aheadBehind`: object with two sub-objects: `vsCreationBase: { ahead, behind, baseOid }` and `vsCurrentHead: { ahead, behind, baseOid }` where `baseOid` is the comparison reference. Either sub-object is `null` when uncomputable (path missing, git error, detached creation without `--include-detached`).
- `suggestedCommands`: object with `reviewDiff`, `reviewDiffVsCreationBase`, `mergeIntoSource`, `cherryPickRange`, `safeRemove`, `discardAndRemove` shell strings (or `null` for any that don't apply). Each value is a self-contained shell-safe string.

Text output renders alias, status, creation context line (e.g., `created from main@abc1234; source now at main@def5678`), dirty/merged flags, both ahead/behind pairs, the porcelain block (with truncation indicator), and the suggested-commands block.

### `delegate worktree remove`

```bash
delegate [--cwd PATH] [--json] worktree remove <alias-or-runId>
    [--discard-uncommitted] [--force-branch] [--force] [--keep-branch]
```

Removes one persistent worktree. The default refuses both destructive overrides — agents must opt into each one explicitly.

The two destructive overrides are split deliberately so an LLM orchestrator cannot reach for a single `--force` and silently destroy work. They map to two distinct risks:

- `--discard-uncommitted`: override the dirty-worktree refusal. **This is the data-loss flag** — uncommitted edits in the worktree are lost.
- `--force-branch`: switch `git branch -d` → `git branch -D` so an unmerged branch is deleted. The branch tip is still recoverable via reflog for ~30 days, so this is less destructive than `--discard-uncommitted` but still worth being explicit.
- `--force`: shorthand for `--discard-uncommitted --force-branch`. Provided for convenience; docs and the prompt note discourage routine use. Completion output prints the split form, not the shorthand.

`--keep-branch`: removes the worktree path but skips branch deletion entirely. Useful when the branch was already merged/pushed externally and the user wants to retain it as a ref. Mutually exclusive with `--force-branch`.

Default flow (no override flags):

1. Resolve handle to a persistent-worktree run.
2. Acquire `registry_lock`. Re-read run state and re-run the dirty/merged/status checks under the lock (see "Concurrency model").
3. If `worktreeStatus == removed` already, exit success with `removed: true, pathRemoved: false, branchRemoved: false, noop: true`.
4. If `worktreeStatus == missing`, fall through to registry cleanup and exit success with `removed: true, pathRemoved: false`.
5. Run the strict dirty check inside the worktree. If dirty, fail with `dirty_worktree`. Include `dirtyPaths` (first 20) and a `nextActions` array containing `delegate worktree show <alias>` and `delegate worktree remove <alias> --discard-uncommitted`.
6. If branch deletion is in scope and the branch is not merged into source `HEAD`, fail with `unmerged_branch` before removing the path. Include `nextActions` for `delegate worktree show <alias>`, `delegate worktree remove <alias> --keep-branch`, and `delegate worktree remove <alias> --force-branch`.
7. Run `git -C <sourceGitRoot> worktree remove <executionCwd>`. On non-zero, fail with `worktree_remove_failed` (include git stderr) and preserve everything. Release lock.
8. Unless `--keep-branch`, run `git -C <sourceGitRoot> branch -d <branch>`.
9. Update registry: set `worktreeStatus = removed`, `worktreeRemovedAt = utc_now_iso()`. Do not delete the run record itself; snapshot/run-output history is preserved. Release lock.

`--discard-uncommitted` flow: same as default but step 5 logs `dirtyPaths` to the run's state file (so the user can see what was discarded post-hoc via `delegate snapshot`) and step 6 uses `git worktree remove --force`.

`--force-branch` flow: same as default but skips the step 6 refusal and step 8 uses `git branch -D`.

JSON schema: `delegate.worktree-remove.v1`. Fields: `ok`, `alias`, `runId`, `branch`, `executionCwd`, `sourceGitRoot`, `removed`, `pathRemoved`, `branchRemoved`, `branchKept`, `worktreeStatus`, `discardedDirtyPaths` (when `--discard-uncommitted` was used), `noop`.

### `delegate worktree prune`

```bash
delegate [--cwd PATH] [--json] worktree prune [--merged] [--older-than DAYS]
    [--harness HARNESS] [--include-detached] [--dry-run]
    [--discard-uncommitted] [--force-branch] [--force]
```

Bulk removal. At least one of `--merged` or `--older-than` must be passed (else `prune_filter_required`); passing both narrows the set to entries matching **both** filters. Selection rules:

- `--merged`: only `present` worktrees whose branch is reachable from current source `HEAD`. Entries whose `creationContext.sourceHeadRef` is null are skipped unless `--include-detached` is passed.
- `--older-than DAYS`: only `present` worktrees whose `lastActivityAt` is older than `DAYS` days.
- `--harness HARNESS`: filter to one harness.
- Dirty worktrees are skipped unless `--discard-uncommitted` is passed.
- Unmerged branches in clean worktrees are removed at the path level but the branch is kept (`branchKept: "unmerged"`) unless `--force-branch` is passed.
- `--force` is shorthand for `--discard-uncommitted --force-branch` and carries the same warnings as on `remove`.

`--dry-run` prints what would be removed and exits without mutating. Without `--dry-run`, removal runs the same lock-protected per-entry logic as `delegate worktree remove`. Each entry is atomic; one failure does not block the rest.

JSON schema: `delegate.worktree-prune.v1`. Returns `planned` (array), `removed` (array), `skipped` (array with per-entry `reason`), and `errors` (array with per-entry error envelopes). Recognized skip reasons: `dirty`, `unmerged_branch`, `path_missing`, `detached_source`, `not_yet_old_enough`, `not_merged`, `harness_filter`.

### `delegate worktree gc`

```bash
delegate [--cwd PATH] [--json] worktree gc [--dry-run]
```

Reconcile registry vs disk reality. For every persistent-worktree run in the registry whose `worktreeStatus` is `present` or `unknown`:

1. Run `git -C <sourceGitRoot> worktree list --porcelain` once per distinct `sourceGitRoot` and parse the output into a set of known worktree paths.
2. If `executionCwd` does not exist on disk: run `git -C <sourceGitRoot> worktree prune` once per source root (idempotent), then set the registry entry's `worktreeStatus = missing` and record `worktreeRemovedAt = utc_now_iso()`. The registry treats this as an external-cleanup acknowledgement, not a Delegate-driven removal.
3. If `executionCwd` exists but is not in the `git worktree list --porcelain` set (orphaned after manual deletion of `.git/worktrees/<id>` administrative metadata), set `worktreeStatus = unknown` and surface the path in the `orphans` array. Do not delete the path.
4. If `executionCwd` and the path appear in `git worktree list --porcelain` but the branch (`creationContext.branch`) no longer resolves via `git rev-parse --verify <branch>`, surface in `orphans` with reason `branch_missing`. Do not delete the path.

`gc` never deletes a worktree path; only `remove` / `prune` do. `--dry-run` prints findings without writing registry changes.

JSON schema: `delegate.worktree-gc.v1`. Fields: `prunedSourceRoots` (count of `git worktree prune` calls), `reconciled` (count of entries whose `worktreeStatus` changed), `orphans` (array with per-entry path, branch, reason).

### Opportunistic prune on read

If `worktrees.autoPrune.enabled` is true, `delegate worktree list` runs a single opportunistic `worktree prune --merged --older-than <mergedOlderThanDays>` pass before producing output. This is bounded: only fully merged-into-source, clean, older-than-threshold entries qualify. Disabled by default; users opt in.

The opportunistic pass acquires the registry lock briefly and never blocks listing if the lock is contended (skip-and-report behavior, same pattern as the retention pass in `cli.py:maybe_run_retention_pass`).

### Cleanup hints in completion output and snapshots

The completion output for a persistent worktree run prints structured cleanup verbs first, with raw `git` lines below for transparency:

```text
cleanup (refuses dirty / unmerged):       delegate worktree remove cursor-4
cleanup (allow unmerged branch deletion): delegate worktree remove cursor-4 --force-branch
cleanup (DISCARD uncommitted edits):      delegate worktree remove cursor-4 --discard-uncommitted
raw git equivalent:                       git -C /Users/treygoff/Code/example worktree remove --force /Users/treygoff/.delegate/worktrees/abc123def456/cursor-20260524T184455Z-a1b2c3 && git -C /Users/treygoff/Code/example branch -D delegate/cursor-20260524T184455Z-a1b2c3
```

Snapshot JSON gains `worktreeCleanupCommands` (object with `safe`, `forceBranch`, `discardUncommitted`, and `rawGit` keys) when the run is persistent worktree mode. Agents should prefer the structured verbs; the raw line is for human eyeball use.

## Policy and sandbox interaction

Isolation must not imply sandboxing.

Examples:

- `delegate --isolation worktree cursor work ...` still uses Cursor work flags.
- `delegate --isolation worktree droid qwen work ...` still uses Droid work flags.
- `delegate --isolation worktree codex work ...` still uses the configured Codex work sandbox and policy.

Docs must clearly state:

> Worktree isolation protects the source checkout from ordinary relative-path edits. It does not prevent the child runtime from running commands, using credentials available to the process, accessing the network according to that runtime and Delegate policy, or intentionally writing to absolute paths outside the execution workspace.

## Error handling

Required errors:

- Unknown `--isolation` value.
- Unknown run input JSON `isolation` value.
- `--isolation worktree` outside a Git workspace.
- Persistent worktree requested from dirty source workspace.
- `--pass-through` with persistent worktree isolation.
- Failure to create branch/worktree.
- Branch/path collision that cannot be resolved.
- Cursor safe `--isolation none` must not overwrite source `.cursor/cli.json`.
- `no_registry`: any `worktree {list,show,remove,prune,gc}` run from a workspace without `.delegate/`.
- `unknown_handle`: `worktree {show,remove}` with an unresolved handle (suggestions surfaced).
- `not_worktree_run`: management command applied to a run that is not persistent-worktree.
- `dirty_worktree`: `worktree remove` without `--discard-uncommitted` on a dirty worktree; payload includes `dirtyPaths` (first 20) and `nextActions`.
- `worktree_create_failed`: `git worktree add` returned non-zero (includes the `branch already checked out` case); payload includes git stderr.
- `worktree_remove_failed`: underlying `git worktree remove` returned non-zero; payload includes git stderr.
- `branch_collision`: target branch name already exists at run launch.
- `prune_filter_required`: `worktree prune` with neither `--merged` nor `--older-than`.
- `invalid_option_combination`: e.g., `worktree remove --keep-branch --force-branch`.
- `invalid_worktrees_config`: malformed `worktrees` config block.

For failed persistent worktree creation, clean up any partially created worktree/branch if safe. If cleanup is not safe, preserve the partial path and report it clearly. Never delete or mutate the source workspace except for Delegate registry metadata.

## Backward compatibility

Existing commands remain valid and retain default behavior.

The intentional new explicit behavior is that `--isolation none` can disable current Cursor/Codex safe isolation. This is acceptable because it is opt-in, visible in metadata, and useful for testing, but docs should discourage casual use.

The existing JSON field `isolation` remains a human-readable note. New structured fields are additive.

## Tests and acceptance criteria

Parser/config:

- `delegate --isolation worktree cursor work "..."` parses successfully.
- `delegate cursor work --isolation worktree "..."` fails clearly as `misplaced_global_option`.
- `delegate --isolation bananas cursor work "..."` fails before launch.
- `delegate run --input-json task.json` accepts `isolation`.
- CLI isolation overrides input JSON isolation.
- Input JSON isolation overrides loaded config isolation.
- Config `isolation.work = "worktree"` affects omitted CLI isolation.
- Explicit `--isolation auto` bypasses configured worktree defaults and restores legacy behavior.
- JSON `isolation = "auto"` overrides config worktree defaults and restores legacy behavior unless CLI overrides it.
- For `run --input-json`, JSON `cwd` is used to discover workspace-local config before final request construction.
- Config validates `isolation.safe` / `isolation.work`.
- `--pass-through --isolation worktree cursor work "..."` fails clearly.

Dry-run:

- Default `--json dry-run cursor work` reports source execution behavior.
- `--json --isolation worktree dry-run cursor work` reports planned persistent metadata without creating a branch/worktree.
- `--json --isolation none dry-run cursor safe` reports source execution behavior and does not claim isolation.
- Dry-run output does not repurpose `isolation` from note to enum.

Execution with fake child binaries:

- Worktree-isolated `cursor work` runs in a persistent Git worktree and does not mutate source files.
- Same for `codex work` and `droid work`.
- Droid `safe` and `work` argv `--cwd` is rewritten to the execution worktree for `--isolation worktree`.
- Persistent worktree remains after successful and failed child runs.
- Registry records source cwd, execution cwd, isolation mode, lifecycle, preserved flag, and branch.
- Simulated branch/worktree creation failure leaves an inspectable failed snapshot.
- Safe-mode temporary `worktree` isolation cleans up.
- Non-Git `--isolation worktree` fails clearly.
- Clean but unborn Git repository fails with `missing_git_head`.
- Dirty source persistent worktree fails clearly.
- Cursor safe `--isolation none` does not write source `.cursor/cli.json`.
- Persistent prompt note appears after the mandatory skill-review prefix and before the user prompt for Cursor, Droid, and Codex work mode.
- `--pass-through --isolation worktree cursor safe "..."` is allowed and cleans up.
- `--pass-through --isolation worktree cursor work "..."` fails before creating artifacts.
- Raw-log retention leaves preserved worktree directories untouched.
- `delegate runs` text and JSON expose enough persistent-worktree metadata to find the preserved execution workspace.
- Tests that can create `~/.delegate/worktrees` set `HOME` to a temp directory and assert no real `~/.delegate` mutation.

Worktree management:

- `delegate worktree list` returns only persistent-worktree runs, sorted by `lastActivityAt` desc, with correct `worktreeStatus`, `dirty`, and `mergedIntoSource` for each entry.
- `delegate worktree list --harness HARNESS` and `--status STATUS` filter correctly.
- `delegate worktree show <alias>` returns porcelain status capped at 50 lines, ahead/behind counts, and structured `suggestedCommands`.
- `delegate worktree show --latest HARNESS` resolves the most recent matching persistent worktree run.
- `delegate worktree remove <alias>` on a clean fully-merged worktree removes path + branch and sets `worktreeStatus = removed`.
- `delegate worktree remove <alias>` on a dirty worktree fails with `dirty_worktree`, includes `dirtyPaths` and the structured `nextActions`, and preserves both path and branch.
- `delegate worktree remove <alias> --discard-uncommitted` removes a dirty worktree and records `discardedDirtyPaths` in the run's state file for post-hoc inspection via `delegate snapshot`.
- `delegate worktree remove <alias>` on a clean worktree with an unmerged branch fails with `unmerged_branch` before removing the path; use `--keep-branch` to remove only the path or `--force-branch` to delete the branch explicitly.
- `delegate worktree remove <alias> --force` is equivalent to passing both `--discard-uncommitted` and `--force-branch`.
- `delegate worktree remove <alias> --keep-branch` removes the path but never deletes the branch.
- `delegate worktree remove <alias> --keep-branch --force-branch` fails with `invalid_option_combination`.
- `delegate worktree remove <alias>` when the path is `missing` succeeds with `pathRemoved: false` and still updates registry status.
- `delegate worktree prune` requires at least one of `--merged` or `--older-than` (else `prune_filter_required`).
- `delegate worktree prune --merged` removes only fully-merged clean worktrees; dirty, unmerged, and detached-creation entries appear in `skipped` with reasons.
- `delegate worktree prune --merged --include-detached` includes entries whose `creationContext.sourceHeadRef` is null in the merge check.
- `delegate worktree prune --older-than DAYS` filters by `lastActivityAt`.
- `delegate worktree prune --dry-run` mutates nothing and prints the planned set.
- `delegate worktree gc` parses `git worktree list --porcelain` and reconciles `worktreeStatus` for paths deleted out-of-band; never deletes worktree paths itself; reports orphans with reasons.
- Opportunistic prune fires when `worktrees.autoPrune.enabled = true` during `worktree list` and is a no-op otherwise. A contended lock skips the pass without blocking listing.
- `delegate worktree list --no-auto-prune` skips the opportunistic pass even with config opt-in.
- Commands run from a workspace without `.delegate/` fail with `no_registry`.
- Commands fail with `not_worktree_run` when handed a non-persistent-worktree run handle.
- All commands round-trip through `--json` with their declared schemas.
- Error payloads include `code`, `message`, `alias`, `runId`, `branch`, `executionCwd`, `sourceGitRoot`, optional `dirtyPaths`, `nextActions`, and `retrySafe`. Tested for `dirty_worktree`, `worktree_remove_failed`, `not_worktree_run`, `no_registry`, `unknown_handle`.
- Two `delegate worktree remove` invocations on the same handle race-free: the second sees `worktreeStatus = removed` and exits success with `noop: true` without re-running git, and without blocking for the full 30s `registry_lock` timeout (assert elapsed < 5s).
- Lazy `dirty` / `mergedIntoSource` fields are tri-state and return `null` (with a warning) when git is unavailable or returns non-zero.
- Cleanup hints in completion output and snapshot JSON show `safe`, `forceBranch`, `discardUncommitted`, and `rawGit` forms.
- `ahead/behind` reports both `vsCreationBase` (using `creationContext.sourceHeadOid`) and `vsCurrentHead`, and the two diverge correctly after the source advances.
- Persistent worktree run with source `HEAD` detached at creation records `sourceHeadRef: null` and `worktree show` includes the "integration target unknown" warning.
- `git worktree add` failure with `branch already checked out` surfaces as `worktree_create_failed` with the underlying git stderr.
- `branch_collision` fails the run before launch; no branch, worktree, or registry mutation beyond pre-launch `creating_isolation` state.
- `repo-fingerprint` is stable across paths with spaces and unicode, the same path read via symlink, and produces distinct values for two unrelated repos. Algorithm pinned to `sha256(resolved_path)[:12]`.
- Worktree management tests set `HOME` to a temp directory.
- Safe + `--isolation worktree` + `--pass-through` creates the temporary worktree, runs the pass-through child inside it, and cleans up via the context manager's `finally` block on both success and child failure.

Docs:

- README mode table distinguishes mode, isolation, and policy.
- README documents `delegate worktree {list,show,remove,prune,gc}` and the worktree lifecycle contract for agents.
- `delegate --json describe` includes isolation defaults, supported values, and the `worktrees` config block.
- `docs/live-runtime.md` and `docs/development.md` document persistent worktree behavior, pass-through restriction, and management commands.
- AGENTS.md documents the new worktree workflow, the agent-facing prompt note, and reiterates not to promote repo changes into `~/.delegate` unless explicitly asked.
- Out-of-tree skill update (`~/.agents/skills/delegate-agent/SKILL.md`) documents that worktree-isolated runs return a `branch` + `executionCwd`, and that orchestrators must use `delegate worktree show/remove/prune` rather than touching paths directly.

## File ownership and module boundaries

`src/delegate_agent/cli.py` already owns parsing, request construction, prompt transforms, safe isolation, argv construction, dry-run, execution dispatch, describe output, and `main()`. This feature should avoid turning `cli.py` into a larger collision point.

Preferred ownership:

- `src/delegate_agent/config.py`: isolation defaults, `worktrees` config validation.
- `src/delegate_agent/cli.py`: global option parsing, request orchestration, subcommand dispatch, `parse_worktree_*` parsers, and `emit_worktree_*` handlers.
- `src/delegate_agent/isolation.py` (new): isolation constants, `IsolationContext`, effective-isolation resolution, Git clean/HEAD checks, creation-context capture, worktree branch/path planning, worktree create helpers. **Creation-side only.**
- `src/delegate_agent/worktree_mgmt.py` (new): pure logic for worktree status detection (`present`/`removed`/`missing`/`unknown`), dirty checks, ahead/behind computation (both `vsCreationBase` and `vsCurrentHead`), `merged-into-source` predicate, prune selection, `gc` reconciliation, **and the single-entry removal primitive shared between `worktree remove` and `worktree prune`**. Importable by `cli.py` and tests; no direct argv parsing here. Keeping removal here (not in `isolation.py`) avoids a `worktree_mgmt → isolation → run_registry → runner` import tangle.
- `src/delegate_agent/runner.py`: run context/state updates and completion output fields, including the new structured cleanup hints.
- `src/delegate_agent/run_registry.py`: registry metadata (including `sourceGitRoot`, `worktreeStatus`, `worktreeRemovedAt`), pre-launch state helpers, and a `set_worktree_status(run_id, status, *, removed_at=None)` helper used by management commands.
- `src/delegate_agent/rendering.py`: JSON/text renderers for `worktree list/show/remove/prune/gc` payloads.

## Implementation sequencing and gates

Use these waves in order. Do not start child-launch behavior until parser/config resolution is green.

### Wave 1: parser/config plumbing

Files:

- `src/delegate_agent/config.py`
- `src/delegate_agent/cli.py`
- parser/config tests

Work:

- Add isolation constants/defaults/validation.
- Add `isolation: str | None` to `ParsedCommand`.
- Add `--isolation` parsing in the global option loop.
- Add `isolation` to `RUN_INPUT_KEYS`.
- Add minimal JSON pre-read for `run --input-json` config discovery.
- Add `resolve_isolation()` with tests covering CLI/input/config/default precedence.

Gate:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_validation
```

### Wave 2: isolation planning and dry-run

Files:

- `src/delegate_agent/cli.py`
- `src/delegate_agent/isolation.py`
- dry-run tests

Work:

- Add generalized `IsolationContext` replacing `SafeIsolationContext`. **Hard constraint:** every existing safe-mode test (`test_delegate_execution`, `test_delegate_commands`, `test_end_to_end_tracking`) must still pass with no behavior change. The generalization is a refactor; user-visible safe-mode behavior is identical until Wave 3 adds the new isolation values.
- Add planned branch/path metadata without creating filesystem artifacts.
- Update dry-run payloads for structured isolation fields.
- Add no-artifact assertions for dry-run.

Gate:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_execution tests.test_delegate_commands
```

### Wave 3: persistent and temporary execution

Files:

- `src/delegate_agent/isolation.py`
- `src/delegate_agent/cli.py`
- `src/delegate_agent/runner.py`
- execution tests

Work:

- Add valid-HEAD and clean-source checks for persistent worktree runs.
- Add pre-launch run state for persistent worktree creation.
- Add persistent branch/worktree creation.
- Preserve persistent worktrees after success/failure.
- Keep temporary safe worktree cleanup behavior.
- Rewrite child argv after isolation resolution for Cursor, Droid, and Codex.
- Add persistent-worktree prompt context injection.
- Enforce `--pass-through` restrictions.

Gate:

```bash
python3 -m unittest tests.test_delegate_execution tests.test_delegate_commands tests.test_runner_capture tests.test_run_registry
```

### Wave 4: registry, rendering, snapshot/completion plumbing

Files:

- `src/delegate_agent/run_registry.py`
- `src/delegate_agent/runner.py`
- `src/delegate_agent/rendering.py`
- registry/rendering tests

Work:

- Add `sourceGitRoot`, `worktreeStatus`, `worktreeRemovedAt` to manifest/snapshot/`runs` output.
- Add `worktreeCleanupCommands` to snapshot JSON for persistent worktree runs.
- Add structured cleanup hints to completion output.
- Add `set_worktree_status` registry helper.
- Add retention tests proving preserved worktrees are not deleted by raw-log retention.

Gate:

```bash
python3 -m unittest tests.test_run_registry tests.test_snapshot_commands tests.test_retention tests.test_end_to_end_tracking
```

### Wave 5: worktree management commands

Files:

- `src/delegate_agent/cli.py`
- `src/delegate_agent/worktree_mgmt.py`
- `src/delegate_agent/rendering.py`
- `tests/test_delegate_worktree_mgmt.py` (new)
- `tests/test_delegate_isolation.py` (new — unit tests for `isolation.py` pure logic: resolution, planning, fingerprint, creation-context capture, clean/HEAD checks. Mirrors the load-module-in-isolation pattern used by `test_retention.py`.)

Work:

- Add `worktree {list,show,remove,prune,gc}` parser and dispatcher.
- Implement status detection, dirty/merged predicates, ahead/behind, removal, prune selection, gc reconciliation.
- Implement opportunistic prune-on-read controlled by `worktrees.autoPrune.enabled`.
- Implement JSON/text rendering for all five commands.
- Add unit tests for each command, plus end-to-end tests that create a persistent worktree, mutate it (clean/dirty/merged), and exercise the full lifecycle.

Gate:

```bash
python3 -m unittest tests.test_delegate_worktree_mgmt tests.test_delegate_parser tests.test_delegate_execution
```

### Wave 6: docs and skill updates

Files:

- `README.md`
- `AGENTS.md`
- `docs/live-runtime.md`
- `docs/development.md`
- `~/.agents/skills/delegate-agent/SKILL.md` (out-of-tree; update separately and note in PR description)

Work:

- README mode table distinguishes mode, isolation, and policy.
- README and AGENTS.md document the worktree management surface and the agent-facing contract (when a `work` run is worktree-isolated, the orchestrator must use `worktree show`/`remove`/`prune` rather than touching `~/.delegate/worktrees/` directly).
- `docs/live-runtime.md` and `docs/development.md` document persistent worktree behavior, pass-through restriction, and the `worktrees` config block.
- Document test/runtime separation from live `~/.delegate`.
- Update the delegate-agent skill so child agents and orchestrators understand the worktree lifecycle and the dogfooded `isolation.work = "worktree"` default in this repo's `.delegate/config.json`.

Final gate before implementation is considered done:

```bash
python3 -m unittest discover -s tests
```

## Non-goals

- Auto-merge worktree changes into source.
- Auto-commit child changes.
- Auto-push Delegate branches.
- Dirty source snapshotting for persistent work mode.
- Public non-Git persistent copy isolation.
- Cross-repo worktree listing/cleanup from outside the spawning workspace.
- Archive-first removal (snapshot the diff before removing the worktree).
- Changing embedded default work-mode isolation to `worktree` in the same release.
- Treating worktree isolation as a security sandbox.

## Follow-up feature candidates

- `--include-dirty` for persistent worktree runs, with explicit baseline patch metadata.
- `--preserve-isolation` for safe-mode investigation worktrees.
- `delegate worktree list --all` that walks `~/.delegate/worktrees/*/` across repositories, deriving registry pointers from the worktree's `.git` file.
- `delegate worktree archive <alias>` that bundles the worktree diff into the run's archive before removal.
- `delegate integrate <alias-or-runId>` to show merge/cherry-pick/apply options without doing them automatically.
- A first-class `worktrees.dataHome` override beyond the simple per-machine config knob added here (e.g., per-repo overrides).
