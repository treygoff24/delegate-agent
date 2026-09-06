# Development notes

Use the repository checkout for development; the installed command may run different code.

## Main modules

`bin/delegate.py` runs the checkout without installation. Implementation modules
live under `src/delegate_agent/`:

| Change | Owning module |
| --- | --- |
| Command syntax, discovery, help | `cli_parser.py`, `command_help.py`, `describe_payload.py` |
| CLI dispatch and human/JSON responses | `cli.py` |
| Config defaults, precedence, validation | `config.py` |
| CLI/JSON launch validation and isolation planning | `request_build.py`, `isolation.py` |
| Request policy copied into execution | `run_context.py`; do not duplicate this mapping for worktrees |
| Engine-specific flags and prompt delivery | `argv_builders.py`, `prompt_transport.py` |
| Process lifetime, terminal outcomes, byte budgets | `runner.py`, `harness_events.py`, `stream_capture.py` |
| Record paths and bounded reads | `record_io.py`; it does not import registry mutations |
| Registry mutations versus read-only status | `run_registry.py` versus `run_status.py`, `snapshot_view.py` |
| Shared metadata fields | `run_metadata.py`; manifest, state, and snapshot remain separate records |
| Persistent-worktree identity and retirement | `worktree_records.py`, `worktree_mgmt.py` |
| Workflow replay, lifecycle commands, immutable runtime pins | `workflows/runtime.py`, `workflows/commands.py`, `workflow_pinning.py` |

Unit tests should call the module that owns the behavior. Keep CLI tests for
parsing/dispatch/output contracts and subprocess fixtures for launch boundaries.
Adding a request field needs non-default propagation checks across ordinary,
temporary, persistent, attached, and grouped-call paths, not another copied
constructor block.

## Development entrypoint

Use the checkout-local entrypoint while developing:

```bash
python3 bin/delegate.py --json describe --overview
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
- Keep parsing in `cli_parser.py` and lifecycle logic in `worktree_mgmt.py`.
- Preserve default safe-mode behavior when changing isolation plumbing.
- Ordinary launch dry-run must not create branches, worktrees, or run records.
  Workflow dry-run is different: it executes the script's Python, so filesystem
  writes remain live unless the script branches on dry-run.
- Worktree cleanup commands must refuse dirty or unmerged work unless the caller passes explicit destructive flags.

See [Worktrees](worktrees.md) for the public lifecycle contract.

## Verification

Run focused tests first, then the broader checks before handoff:

```bash
python3 -m compileall -q src tests bin
git diff --check
python3 -m pytest -q
```

`tests/acceptance.sh` runs all four required gates, including Ruff lint and
format checks. It reports Python and Ruff versions and selects the pinned Ruff
from the checkout's `.venv`, the main checkout's `.venv` when in a linked
worktree, or PATH when its version matches the dev extra. A mismatched ambient
Ruff fails before running the gates; install the dev extra rather than accepting
different lint behavior on different machines.

Collection imports `tests/__init__.py`, which shims `src` onto `sys.path`,
installs a private HOME/temp environment, and strips ambient env.
`pyproject.toml` sets `testpaths = ["tests"]`, so a test file placed outside
`tests/` is never collected and never runs.

Required CI does not need real Cursor, Droid, Codex, Claude, Grok, Devin,
OpenCode, Pi, Oh My Pi, or Kimi binaries.
