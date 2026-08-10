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

When `AI_PROFILE=work|personal` is set and the matching
`~/.delegate/config.<profile>.json` overlay is missing, Delegate blocks launch
and mutation commands but allows read-only diagnostics (`profiles`, `runs`,
`run-output`, `snapshot`, cached `capabilities`, `worktree show`,
`worktree list`, `describe`, `models`) with a warning. This check runs inside
`delegate_agent.cli:main` (`src/delegate_agent/profile_guard.py`), so it applies
regardless of entrypoint: the installed pip console script, `python -m
delegate_agent.cli`, or `bin/delegate.py`. Some local installs additionally put
a profile-aware shell shim in front of the Python entrypoint -- the tracked
source for that shim is `bin/delegate-profile-shim` -- which applies the same
check even earlier, before Python starts. `AI_PROFILE` values other than
`work`/`personal` are not recognized profiles; Delegate warns and runs on the
base account rather than failing closed, since there is no config filename
convention to check against. Fix a half-configured install with
`env -u AI_PROFILE delegate config sync-profiles`, or bypass once with
`env -u AI_PROFILE delegate ...` or an explicit `DELEGATE_CONFIG=...`.
In profile-aware shell installs, profile selection loads credentials; an
incoming explicit `DELEGATE_CONFIG` remains the runtime-policy config.

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
