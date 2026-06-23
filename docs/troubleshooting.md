# Troubleshooting

If you are troubleshooting from a source checkout, substitute the repo-local
entry point for installed examples:

```bash
python3 bin/delegate.py ...
```

## `missing_binary` / exit code 3

Real runs require the selected child runtime on `PATH`:

```bash
command -v agent
command -v droid
command -v codex
command -v claude
command -v kimi
```

Dry-run does not require the child binary:

```bash
delegate --json dry-run codex safe "Review only."
```

`missing_binary` searches the `PATH` of the process that launched Delegate, not
your interactive shell. If an installer only updated `.zshrc`/`.bashrc`, the
binary may work in a terminal and still be invisible to Delegate from an agent,
cron, launchd, or another non-interactive subprocess.

The durable fix is to set an absolute binary path in the active config, for
example `codex.binary`, `claude.binary`, `droid.binary`, `kimi.binary`, or `cursor.argvPrefix`.
JSON errors include `configPath`, `configKey`, and, when Delegate sees a likely
user-local install, `suggestedBinaryPath`.

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

If editing `~/.delegate/config.json` does not change behavior, check the active
config layers:

```bash
delegate --json describe
```

Inspect `configResolution.layers`; `DELEGATE_CONFIG` and workspace
`.delegate/config.json` can override the user config.

## `unsupported_reasoning_effort`

Delegate validates requested reasoning effort against the resolved harness and model before launch:

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."
delegate --json capabilities
```

Common causes:

- Codex effort was requested but no Codex model was resolved. Set `codex.defaultModel` or pass a Codex model in JSON run input.
- Claude effort must be one of Claude Code's native labels: `low`, `medium`, `high`, `xhigh`, or `max`.
- Cursor effort was requested but `cursor.reasoningEffortModels.<level>` is missing. Cursor effort uses model selection rather than a standalone effort flag.
- Droid or Codex model support is not in config, the workspace cache, or bundled fallback data.
- The effort string is misspelled. Delegate treats labels literally and does not translate between provider naming schemes.

These failures apply to explicit per-run effort (`--reasoning-effort` or JSON run input). For Cursor, Droid, and Codex, a config `defaultReasoningEffort` that cannot be satisfied does not fail the run; the run proceeds without reasoning effort and records a warning in the dry-run payload, manifest, and snapshot. Claude config defaults are validated against its static native labels at config load. Kimi does not support reasoning effort, so `kimi.defaultReasoningEffort` must stay `null`.

For private or newly released models, declare support in `reasoning.capabilities` in config. Inspect first with plain `capabilities`. To refresh workspace-local discovered data, run:

```bash
delegate --json capabilities refresh
```

Refresh is not read-only: it may invoke child CLIs and writes
`.delegate/capabilities/reasoning.json` only after the refreshed schema
validates. A malformed cache file is ignored at run time and overwritten by the
next refresh. That file is runtime state; do not commit it.

## Unexpected config source

`delegate --json describe` reports `configSource` and
`configResolution.layers`. If the source points somewhere unexpected, check:

```bash
echo "$DELEGATE_CONFIG"
ls -la ~/.delegate/config.json .delegate/config.json 2>/dev/null || true
```

When `DELEGATE_CONFIG` is set, the file must exist.

## Global options rejected

For launch commands and `dry-run`, global options must appear before the
subcommand:

```bash
# Correct
delegate --json --cwd /path/to/repo dry-run codex safe "Review only."

# Incorrect
delegate dry-run --json codex safe "Review only."
delegate codex safe --pass-through "Review only."
```

Some inspection commands accept trailing `--json` for convenience, such as
`delegate describe --json` and `delegate run-output <alias> --json`.

## Long foreground run looks silent

Tracked launches buffer child output so Delegate can return a bounded final
summary and preserve JSON stdout. For long-running foreground jobs, add
`--progress` after the mode and before prompt text:

```bash
delegate --json claude safe --progress "Review only. Do not edit."
delegate --json droid reviewer work --progress "Implement the scoped change."
```

Progress messages go to stderr. They are intentionally bounded and do not
include raw child output. `--progress` is incompatible with `--pass-through`,
which already streams raw child output.

## Safe-mode isolation fails

Cursor, Droid, Codex, Claude, and Kimi safe create an isolated temporary
workspace by default. In Git repositories, Delegate first tries a detached
worktree; for non-Git directories and some Git fallback cases, it uses a
directory copy. Codex safe is the only safe harness that may opt out with
`--isolation none`, because Codex still keeps its read-only sandbox active.
Cursor, Droid, Claude, and Kimi safe reject `--isolation none` because their
safe contracts depend on Delegate's temporary workspace boundary.

Dry-run can inspect the planned argv and isolation mode, but it does not
materialize the temporary workspace, create the detached worktree, copy files,
or apply a dirty diff. To troubleshoot actual isolation failures, check Git
state and then reproduce with a real run only in a disposable workspace:

```bash
git status --short
git rev-parse --verify HEAD
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

## Persistent worktree run refused

Work-mode persistent worktrees require a Git repository with a valid `HEAD` and a clean source checkout.

```bash
git status --short
git rev-parse --verify HEAD
delegate --json --isolation worktree dry-run cursor work "Implement only."
```

Detached `HEAD` is valid; an unborn repository with no commit is not. Dry-run
shows the planned branch/path but does not run the full launch preflight. If the
source checkout is dirty, commit, stash, or choose a different isolation mode
before launching.

## Persistent worktree run failed with `commit_policy_violated`

`--forbid-commit` is valid only for `work` mode with persistent worktree
isolation. When enabled, Delegate fails the run if the child creates commits:

```bash
delegate --json --isolation worktree cursor work --forbid-commit "Implement without committing."
```

The worktree and branch are preserved. Inspect `workSummary` in the completion
JSON or `delegate worktree show <alias-or-runId>` to see changed files, diff
stat, and created commits. If commits were intentional, review the worktree and
rerun without `--forbid-commit` or integrate the branch manually.

## `--pass-through` rejected

`--pass-through` is incompatible with `--json` and with persistent worktree
launches. Dry-run may still show the planned persistent-worktree argv; the real
launch is refused before child execution. `--pass-through` is intended only for
raw child stdout/stderr streaming and must appear before the subcommand. Normal
tracked runs already return bounded parent-facing summaries.

Use inspection commands instead:

```bash
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
delegate run-output <alias-or-runId> --stderr --tail 100
delegate run-output <alias-or-runId> --stdout --tail 80 --max-chars 20000
```

Non-raw stdout/stderr output is bounded by both line tail and character cap.
Use `--raw` only when you intentionally need the full stream; it is incompatible
with `--tail` and `--max-chars`, may print very large output, and includes
`rawOutputBytes` in JSON metadata so callers can see how much raw output was
returned.

If your prompt requires an exact structured final answer such as bare JSON, use
`--no-completion-report` today so Delegate does not inject completion-report
instructions into the child prompt.

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
delegate worktree remove <alias-or-runId> --force
```

`--force` combines discarding uncommitted edits with removing the branch.
`--keep-branch` leaves the branch in place and does not discard edits. These
flags can discard edits or delete unmerged branches. Use them only after
reviewing the worktree.

## CI does not have child runtimes

That is expected. Required tests do not need real Cursor, Droid, Codex, Claude, or Kimi binaries:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests
```

Integration tests that launch real child agents should be separate from required CI.
