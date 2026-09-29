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
| Workflow replay, lifecycle commands, content-addressed runtime pins (`--repin`) | `workflows/runtime.py`, `workflows/commands.py`, `workflow_pinning.py` |

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

Run affected tests while iterating, then the full gate before handoff:

```bash
python3 scripts/test.py -- tests/test_delegate_parser.py
tests/acceptance.sh --fast
tests/acceptance.sh
git diff --check
```

`scripts/gate.sh` runs the same four checks through uv; `tests/acceptance.sh`
uses the existing dev environment. Both report tool versions and require the
pinned Ruff from the dev extra. Fast checks run a selected set of quick tests
plus compile, lint, and format; add affected paths after `--`. Their success
marker is `FAST CHECKS PASS`, distinct from the full `GATE PASS`.

The runner uses four workers on Linux, two on macOS, individual-test scheduling,
and low priority. When `testrun` is installed on Linux, its named scope limits
all descendants to two cores. macOS has no hard CPU quota. A common Git-directory
lock refuses overlapping runs across linked worktrees (exit 75); retry once the
first run finishes. Explicit `--workers` and `--cpu-limit` overrides are available.
Never use `-n auto` on a shared machine. The slowest 25 phases are reported; use
`--durations 50` for a longer profile. Full gates refuse test filters and clear
ambient `PYTEST_ADDOPTS` so they cannot silently run a subset.

Collection imports `tests/__init__.py`, which shims `src` onto `sys.path`,
installs a private HOME/temp environment, and strips ambient env.
`pyproject.toml` sets `testpaths = ["tests"]`, so a test file placed outside
`tests/` is never collected and never runs.

Required CI does not need real Cursor, Droid, Codex, Claude, Grok, Devin,
OpenCode, Pi, Oh My Pi, or Kimi binaries.
