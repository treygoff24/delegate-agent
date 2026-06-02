# Development and installed runtime separation

A development checkout and an installed `delegate` command can coexist. They may not have the same code or config.

## Development checkout

When working inside this repository, use the repo-local entrypoint:

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

This avoids accidentally calling an older installed shim from `PATH`.

## Installed runtime

When Delegate is installed, `delegate` resolves through your shell `PATH`:

```bash
command -v delegate
delegate --json describe
```

The installed command may use user config from `~/.delegate/config.json` unless `DELEGATE_CONFIG` points elsewhere. `describe` reports the active `configSource`.

## Do not promote implicitly

Repository development should not overwrite an installed runtime, user config, or local worktree store as a side effect. Promote a checkout to an installed command only through an explicit install/update step after review and tests.

## Run metadata

Tracked runs may write workspace-local metadata under `.delegate/`. That metadata is for inspection commands such as:

```bash
delegate runs
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
```

Do not commit `.delegate/` run state. Do not tail raw logs as the normal integration path; use Delegate's bounded inspection commands.

For persistent worktree behavior, see [Worktrees](worktrees.md).
