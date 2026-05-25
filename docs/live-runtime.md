# Live runtime separation

This repository can be used as a development checkout while an already-installed
`delegate` command continues to run from a separate local runtime:

- shim: `~/.local/bin/delegate`
- runtime implementation: `~/.delegate/bin/delegate.py`
- runtime config: `~/.delegate/config.json`

Keep those paths unchanged while doing development here. Promote changes to a
live runtime only through an explicit install/update step after review and tests.

## Development vs installed runtime

Features added in this checkout are available through the development entry
point before they are available through the installed `delegate` command:

```bash
python3 bin/delegate.py --isolation worktree cursor work "task"
python3 bin/delegate.py worktree list
```

Users running bare `delegate` from `PATH` get the installed runtime, which may
lag behind this checkout. Do not assume installed-runtime worktree support until
promotion occurs.

The development checkout may add workspace-local `.delegate/` registries, bounded
default output, `snapshot` / `runs` / `run-output` commands, and archive-only
retention. None of that affects the live runtime until promotion. Orchestrating
agents should launch Delegate normally and use `delegate snapshot` plus related
commands instead of piping launches through `tail` or tailing raw log files under
`.delegate/runs/`.

## Worktree isolation

When `--isolation worktree` is used with `work` mode, Delegate creates persistent
Git worktrees under the Delegate data home (`~/.delegate/worktrees/<repo-fingerprint>/`)
that survive after the child agent exits. The persistent worktree is the
**execution workspace** — the source checkout is not modified by the child agent's
relative-path edits.

### Pass-through restriction

`--pass-through` is rejected with persistent worktree runs (work mode +
`--isolation worktree`). The combination would skip the registry-centered output
path, but persistent worktrees need a run id, branch name, metadata, and cleanup
instructions. Fail fast avoids orphaning worktrees without aliases or snapshots.

`--pass-through` is still allowed with:
- Default work mode (isolation `none` or `auto` legacy).
- Temporary safe-mode isolation (including `--isolation worktree cursor safe`).

### Config block

The `worktrees` config section controls persistent worktree behavior:

```json
{
  "worktrees": {
    "dataHome": null,
    "autoPrune": {
      "enabled": false,
      "mergedOlderThanDays": 7
    }
  }
}
```

- `worktrees.dataHome` — override the persistent-worktree root. Defaults to
  `~/.delegate/worktrees`. When set, must be an absolute or `~/`-prefixed path;
  tilde expansion uses `Path.expanduser()`.
- `worktrees.autoPrune.enabled` — when `true`, `delegate worktree list` runs a
  single opportunistic prune pass before producing output (only clean, fully-merged
  worktrees older than `mergedOlderThanDays` qualify). Disabled by default.
- `worktrees.autoPrune.mergedOlderThanDays` — non-negative integer, default 7.

### Worktree management

Use `delegate worktree {list,show,remove,prune,gc}` from the workspace that
spawned the run. Do not manually delete paths under `~/.delegate/worktrees/` —
this orphans registry entries. See the README for full command reference.

### Test/runtime separation

Tests that exercise worktree creation or management must set `HOME` to a
`TemporaryDirectory` and assert the produced worktree path is under that
temporary home. They must not write to the real `~/.delegate/worktrees/`
directory or use the installed `delegate` shim. Use `python3 bin/delegate.py`
from the repo root for development instead.

## Harness behavior

The development checkout uses the harness contracts below. After explicit
promotion, the installed runtime gains the same contracts:

| Harness | Safe | Work |
| --- | --- | --- |
| `cursor` | Isolated workspace copy; `-p --trust` only | Real workspace; `--approve-mcps --force` |
| `droid` | Real workspace; default read-only | Real workspace; `--skip-permissions-unsafe` |
| `codex` | Isolated workspace copy; `codex --ask-for-approval never exec --sandbox read-only` | Real workspace; `codex --ask-for-approval never exec --sandbox workspace-write` plus network config when policy allows |

`delegate codex safe` reports `isolatedWorkspace: true` in JSON/dry-run metadata, same isolation guarantee as `cursor safe`: the source tree is not passed as `--cd` and is not modified by the child process. Tracked runs may still write Delegate metadata under `.delegate/` in the source workspace.

Policy profiles (`safe`, `trusted-hooks`, `external-sandbox`, `custom`) and per-mode overrides apply system-wide; only fields a harness supports affect its argv. See README policy controls and `delegate --json describe` on the promoted runtime.
