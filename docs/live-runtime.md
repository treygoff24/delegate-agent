# Live runtime separation

This repository can be used as a development checkout while an already-installed
`delegate` command continues to run from a separate local runtime:

- shim: `~/.local/bin/delegate`
- runtime implementation: `~/.delegate/bin/delegate.py`
- runtime config: `~/.delegate/config.json`

Keep those paths unchanged while doing development here. Promote changes to a
live runtime only through an explicit install/update step after review and tests.

The development checkout may add workspace-local `.delegate/` registries, bounded
default output, `snapshot` / `runs` / `run-output` commands, and archive-only
retention. None of that affects the live runtime until promotion. Orchestrating
agents should launch Delegate normally and use `delegate snapshot` plus related
commands instead of piping launches through `tail` or tailing raw log files under
`.delegate/runs/`.

## Harness behavior (after promotion)

When this checkout is promoted, the live runtime gains the same harness contracts documented in the repo:

| Harness | Safe | Work |
| --- | --- | --- |
| `cursor` | Isolated workspace copy; `-p --trust` only | Real workspace; `--approve-mcps --force` |
| `droid` | Real workspace; default read-only | Real workspace; `--skip-permissions-unsafe` |
| `codex` | Isolated workspace copy; `codex exec --sandbox read-only --ask-for-approval never` | Real workspace; `codex exec --sandbox workspace-write` plus network config when policy allows |

`delegate codex safe` reports `isolatedWorkspace: true` in JSON/dry-run metadata, same isolation guarantee as `cursor safe`: the source tree is not passed as `--cd` and is not modified by the child process.

Policy profiles (`safe`, `trusted-hooks`, `external-sandbox`, `custom`) and per-mode overrides apply system-wide; only fields a harness supports affect its argv. See README policy controls and `delegate --json describe` on the promoted runtime.
