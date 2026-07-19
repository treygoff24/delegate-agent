# Development notes

Delegate Agent is intentionally small and shell-first. This page is for contributors working in the repository checkout.

## Main modules

- `bin/delegate.py` runs the checkout directly without installing it.
- `src/delegate_agent/cli.py` contains CLI parsing, request validation, command construction, prompt transforms, and high-level dispatch.
- `src/delegate_agent/config.py` owns config defaults, merge precedence, validation, isolation defaults, and policy settings.
- `src/delegate_agent/isolation.py` owns isolation planning and Git worktree creation helpers.
- `src/delegate_agent/runner.py` launches tracked child processes and writes manifests, state, snapshots, and completion reports.
- `src/delegate_agent/run_registry.py`, `rendering.py`, `retention.py`, and `archived_logs.py` implement local run tracking, bounded output, redaction, and archive-only retention.
- `src/delegate_agent/worktree_mgmt.py` owns persistent-worktree lifecycle commands.
- `tests/` covers parser, validation, command construction, execution output, snapshots, retention, run registry, isolation, and worktree management.

## Development entrypoint

Use the checkout-local entrypoint while developing:

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

Do not overwrite an installed `delegate` shim, user config, or live runtime as a side effect of development. Promotion to an installed command should be an explicit operator action after review and tests.

## Config during development

Use `DELEGATE_CONFIG` when you need an explicit config overlay. For deterministic
output independent of user-level config, run with a temporary `HOME` too:

```bash
clean_home="$(mktemp -d)"
HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json describe
HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json models
```

Droid real runs require non-placeholder model IDs. Dry-runs for Cursor, Droid,
Codex, Claude, Grok, Devin, OpenCode, Pi, Oh My Pi, and Kimi do not require
child binaries. Engine event tests use captured fixtures under
`tests/fixtures/`.

## Persistent worktree development

When implementing or testing persistent-worktree behavior:

- Tests that create worktrees must set `HOME` to a temporary directory and assert generated paths are under that temporary home.
- Keep parsing in `cli.py` and lifecycle logic in `worktree_mgmt.py`.
- Preserve default safe-mode behavior when changing isolation plumbing.
- Dry-run must never create branches, worktrees, registry runs, or filesystem artifacts.
- Worktree cleanup commands must refuse dirty or unmerged work unless the caller passes explicit destructive flags.

See [Worktrees](worktrees.md) for the public lifecycle contract.

## Verification

Run focused tests first, then the broader checks before handoff:

```bash
python3 -m compileall -q src tests bin
git diff --check
python3 -m unittest discover -s tests
```

Required CI does not need real Cursor, Droid, Codex, Claude, Grok, Devin,
OpenCode, Pi, Oh My Pi, or Kimi binaries.
