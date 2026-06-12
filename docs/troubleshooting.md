# Troubleshooting

## `missing_binary` / exit code 3

Real runs require the selected child runtime on `PATH`:

```bash
command -v agent
command -v droid
command -v codex
```

Dry-run does not require the child binary:

```bash
delegate --json dry-run codex safe "Review only."
```

## `invalid_alias` or `unconfigured_model`

Droid uses local aliases from config:

```bash
delegate --json models
```

Copy `config.example.json` and replace placeholder IDs:

```bash
mkdir -p ~/.delegate
cp config.example.json ~/.delegate/config.json
$EDITOR ~/.delegate/config.json
```

Use aliases like `reviewer` or `implementer` in commands:

```bash
delegate droid reviewer safe "Investigate only. Do not edit."
```

## `unsupported_reasoning_effort`

Delegate validates requested reasoning effort against the resolved harness and model before launch:

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."
delegate --json capabilities
```

Common causes:

- Codex effort was requested but no Codex model was resolved. Set `codex.defaultModel` or pass a Codex model in JSON run input.
- Cursor effort was requested but `cursor.reasoningEffortModels.<level>` is missing. Cursor effort uses model selection rather than a standalone effort flag.
- Droid or Codex model support is not in config, the workspace cache, or bundled fallback data.
- The effort string is misspelled. Delegate treats labels literally and does not translate between provider naming schemes.

These failures apply to explicit per-run effort (`--reasoning-effort` or JSON run input). For Cursor, Droid, and Codex, a config `defaultReasoningEffort` that cannot be satisfied does not fail the run; the run proceeds without reasoning effort and records a warning in the dry-run payload, manifest, and snapshot. Kimi does not support reasoning effort, so `kimi.defaultReasoningEffort` must stay `null`.

For private or newly released models, declare support in `reasoning.capabilities` in config. To refresh workspace-local discovered data, run:

```bash
delegate --json capabilities refresh
```

Refresh may invoke child CLIs and writes `.delegate/capabilities/reasoning.json` only after the refreshed schema validates. A malformed cache file is ignored at run time and overwritten by the next refresh. That file is runtime state; do not commit it.

## Unexpected config source

`delegate --json describe` reports `configSource`. If it points somewhere unexpected, check:

```bash
echo "$DELEGATE_CONFIG"
ls -la ~/.delegate/config.json .delegate/config.json 2>/dev/null || true
```

When `DELEGATE_CONFIG` is set, the file must exist.

## Global options rejected

Global options must appear before the subcommand:

```bash
# Correct
delegate --json --cwd /path/to/repo dry-run codex safe "Review only."

# Incorrect
delegate dry-run --json codex safe "Review only."
```

## Safe-mode isolation fails

Cursor safe and Codex safe create an isolated temporary workspace. In Git repositories, Delegate first tries a detached worktree. If Git metadata is invalid, dirty diff application fails, or filesystem copying fails, use dry-run to inspect the plan and then check Git state:

```bash
git status --short
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

For non-Git directories, Delegate uses a directory copy for temporary safe isolation.

## Persistent worktree run refused

Work-mode persistent worktrees require a Git repository with a valid `HEAD` and a clean source checkout.

```bash
git status --short
delegate --json --isolation worktree dry-run cursor work "Implement only."
```

If the source checkout is dirty, commit, stash, or choose a different isolation mode before launching.

## `--pass-through` rejected

`--pass-through` is incompatible with `--json` and with persistent worktree runs. It is intended only for raw child stdout/stderr streaming. Normal tracked runs already return bounded parent-facing summaries.

Use inspection commands instead:

```bash
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
delegate run-output <alias-or-runId> --stderr --tail 100
```

## Worktree cleanup refused

`delegate worktree remove` refuses dirty worktrees and unmerged branches by default. Inspect first:

```bash
delegate worktree show <alias-or-runId>
```

Then choose an explicit cleanup path:

```bash
delegate worktree remove <alias-or-runId> --discard-uncommitted
delegate worktree remove <alias-or-runId> --force-branch
delegate worktree remove <alias-or-runId> --keep-branch
```

These flags can discard edits or delete unmerged branches. Use them only after reviewing the worktree.

## CI does not have child runtimes

That is expected. Required tests do not need real Cursor, Droid, or Codex binaries:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests
```

Integration tests that launch real child agents should be separate from required CI.
