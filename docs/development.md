# Development notes

Delegate Agent is intentionally small:

- `src/delegate_agent/cli.py` contains the CLI parser, validation, request builder, and child-process execution. `cursor safe` wraps execution in a temporary isolated workspace (git worktree or directory copy), writes `.cursor/cli.json` only there, and restores JSON `cwd` / `executionCwd` fields for the source vs isolated paths.
- `src/delegate_agent/run_registry.py`, `runner.py`, `rendering.py`, and `retention.py` implement workspace-local run tracking, bounded parent output, snapshots, and archive-only retention.
- `src/delegate_agent/isolation.py` owns first-class isolation constants, `IsolationContext`, effective-isolation planning, Git clean/HEAD checks, creation-context capture, and worktree create helpers. See `docs/plans/2026-05-24-work-mode-isolation-spec.md`.
- `src/delegate_agent/worktree_mgmt.py` owns lifecycle logic for persistent-worktree management: worktree status detection, dirty/merged predicates, ahead/behind computation, prune selection, gc reconciliation, and the single-entry removal primitive shared between `worktree remove` and `worktree prune`.
- `bin/delegate.py` runs the checkout directly without installing it.
- `config.example.json` documents safe default configuration shape, including `tracking.retention`, `tracking.completionReport`, `isolation`, and `worktrees` config sections.
- `tests/` covers parser, validation, command construction, execution output, snapshots, retention, static safety guards, and worktree management.

The live runtime used by an operator may be separate from this checkout. Do not update an installed shim or runtime as a side effect of normal development. Verify with `python3 bin/delegate.py` from the repo root; promote to `~/.delegate` only after review and explicit operator request.

## Persistent worktree development

When implementing or testing persistent-worktree features:

- Worktree creation helpers (`src/delegate_agent/isolation.py`) must never write to the real `~/.delegate/worktrees/`. Tests must set `HOME` to a `TemporaryDirectory` and assert the produced worktree path is under that temporary home.
- The worktree management surface (`delegate worktree {list,show,remove,prune,gc}`) is dispatched from `cli.py` but the core logic lives in `src/delegate_agent/worktree_mgmt.py` — pure functions for status detection, dirty/merged predicates, ahead/behind, and removal. Keep parsing in `cli.py` and logic in `worktree_mgmt.py`.
- Existing safe-mode behavior must remain backward-compatible when isolation plumbing changes. Every existing safe-mode test should pass with no behavior change.
- Dry-run (`--json dry-run`) must never create branches, worktrees, registry runs, or filesystem artifacts.

## Codex and policy (dev checkout)

Dry-run Codex without launching the binary:

```bash
python3 bin/delegate.py --json dry-run codex work "hello"
python3 bin/delegate.py --json dry-run codex safe "review only"
```

Inspect merged config, engines, mode mappings, and effective policy metadata:

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json models
```

Override policy for a one-off smoke test without editing repo config:

```bash
tmp_config="$(mktemp)"
python3 -c 'import json,sys; json.dump({"policy":{"profile":"trusted-hooks"}}, open(sys.argv[1],"w"))' "$tmp_config"
DELEGATE_CONFIG="$tmp_config" python3 bin/delegate.py --json dry-run codex work "hello"
```

`codex safe` dry-run should report `isolatedWorkspace: true` and argv with `--sandbox read-only` (no workspace-write network config, no dangerous bypass flags by default).
