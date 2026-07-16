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
command -v grok
command -v devin
command -v opencode
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
example `codex.binary`, `claude.binary`, `grok.binary`, `devin.binary`,
`opencode.binary`, `droid.binary`, `kimi.binary`, or `cursor.argvPrefix`.
JSON errors include `configPath`, `configKey`, and, when Delegate sees a likely
user-local install, `suggestedBinaryPath`.

The OpenCode curl installer normally writes `opencode` under
`~/.opencode/bin`, which is often absent from the `PATH` inherited by agents and
other non-interactive processes. Add that directory to their `PATH` or set
`opencode.binary` to the absolute executable path.

## OpenCode exits `1` and mentions `OPENCODE_CONFIG_CONTENT`

OpenCode validates injected config against its current strict schema. If this
starts after an OpenCode upgrade, the injected lockdown schema and the installed
OpenCode version may no longer agree. Confirm the OpenCode version and the error
before changing Delegate's read-only policy.

## `invalid_alias` or `unconfigured_model`

Droid uses local aliases from config:

```bash
delegate --json models
```

Initialize config and replace placeholder IDs:

```bash
delegate config init
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

- Codex effort was requested with a label not supported by the resolved model or the harness-default fallback capability.
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

## OpenCode silently ignores an unknown variant

Delegate maps OpenCode `--reasoning-effort LEVEL` directly to `--variant LEVEL`
without model validation. OpenCode silently ignores bogus variant names, so a
typo can leave the run at its normal variant without an error. Inspect the
dry-run argv and correct the variant spelling:

```bash
delegate --json dry-run opencode safe --reasoning-effort high "Review only."
```

## Unexpected config source

`delegate --json describe` reports `configSource` and
`configResolution.layers`. If the source points somewhere unexpected, check:

```bash
echo "$DELEGATE_CONFIG"
ls -la ~/.delegate/config.json .delegate/config.json 2>/dev/null || true
```

When `DELEGATE_CONFIG` is set, the file must exist.

## `AI_PROFILE=...` but `config.<profile>.json` is missing

Delegate uses `~/.delegate/config.work.json` or `~/.delegate/config.personal.json`
when `AI_PROFILE=work|personal` is present. If that overlay is missing, launch
and mutation commands fail closed so they do not silently use the wrong
account. Read-only diagnostics (`profiles`, `runs`, `run-output`, `snapshot`,
cached `capabilities`, `worktree show`, `worktree list`, `describe`, `models`)
should still run with a warning. This check runs in the Python CLI itself, so
it applies whether or not a profile-aware launcher shim
(`bin/delegate-profile-shim`) is in front of `delegate`.

A miscased or unrecognized `AI_PROFILE` value (anything other than exactly
`work` or `personal`) is not treated as a profile crossover risk at all --
Delegate warns that it is running on the base account and proceeds normally.

Fix the install:

```bash
env -u AI_PROFILE delegate config sync-profiles
```

Temporary bypasses:

```bash
env -u AI_PROFILE delegate profiles
DELEGATE_CONFIG=/path/to/config.json delegate profiles
```

## WSL path or Git warnings

Inside WSL, use Linux paths and Linux-installed tools:

```bash
command -v git
wslpath -u 'C:\Users\you\repo'
```

Delegate rejects Windows-style paths such as `C:\Users\...` or
`%USERPROFILE%\...` in `--cwd`, `DELEGATE_CONFIG`, `CODEX_HOME`, prompt/schema
paths, and `worktrees.dataHome`. Convert them first with `wslpath -u`, or use a
native WSL path under `/home/<user>/...`.

If Delegate reports `windows_git_in_wsl`, `git` resolved to Windows `git.exe`.
Install Git inside WSL, for example:

```bash
sudo apt install git
```

If a dry-run or launch warns that the workspace is under `/mnt/c`, the run can
still work, but WSL filesystem performance and private-file permissions are
better under `/home/<user>/...`.

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
`--progress` after the mode and before prompt text, or set `progress.enabled`
to `true` in config and use `--no-progress` to override for one launch:

```bash
delegate --json claude safe --progress "Review only. Do not edit."
delegate --json droid reviewer work --progress "Implement the scoped change."
```

Progress messages go to stderr. They are intentionally bounded, credential-scrubbed
labels before printing, and do not include raw child output.
`--progress` is incompatible with `--pass-through`, which already streams raw
child output.

OpenCode v1.17.17 buffers stdout until completion. A tracked OpenCode run can
remain at "no events yet" while it is still running; the final events appear
after the child exits.

## Need one-hop output instead of a tracked run

Use `call` mode when you just need a prompt answered and do not want Delegate to
resolve the repo, create `.delegate/runs`, inject completion-report framing, or
require a later `snapshot`/`run-output` lookup:

```bash
delegate --json codex call "Summarize this context."
delegate --json droid reviewer call --prompt-file prompt.md
```

Call mode returns captured assistant text in JSON `text` when available. Use
`safe` or `work` for project-aware review/implementation and tracked output.

## Safe-mode isolation fails

Cursor, Droid, Codex, Claude, Grok, Devin, OpenCode, and Kimi safe create an
isolated throwaway workspace by default. Safe mode reviews your **current working tree**,
uncommitted tracked edits and untracked, non-ignored files are mirrored into
that copy (only gitignored paths are excluded), so you do **not** need to
commit or stash before a safe review. In Git repositories, Delegate first tries
a detached worktree and syncs the dirty tree into it; for non-Git directories
and some Git fallback cases, it uses a directory copy. Codex safe is the only
safe harness that may opt out with `--isolation none`, because Codex still
keeps its read-only sandbox active. Cursor, Droid, Claude, Grok, Devin,
OpenCode, and Kimi safe
normalize `--isolation none` back to `auto` with a warning because their safe
contracts depend on Delegate's temporary workspace boundary.

Dry-run can inspect the planned argv and isolation mode, but it does not
materialize the temporary workspace, create the detached worktree, copy files,
or apply the working-tree sync. To troubleshoot actual isolation failures,
check Git state and then reproduce with a real run only in a disposable
workspace:

```bash
git status --short
git rev-parse --verify HEAD
python3 bin/delegate.py --json dry-run codex safe "Review only."
python3 bin/delegate.py codex safe "Review my uncommitted changes. Do not edit."
```

## Persistent worktree run refused

Work-mode persistent worktrees require a Git repository with a valid `HEAD`.
Dirty tracked and untracked non-ignored files are synced automatically; sync
failures abort and tear down the new worktree before child launch.

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
isolation. When enabled, Delegate fails the run if commits remain ahead of the
creation base when the child exits:

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

## Structured / JSON-only final output

For a bare machine-parseable final message on Codex, use `--output-schema FILE`
(codex-only). OpenAI enforces the JSON Schema on Codex's final message; Delegate
suppresses the completion-report prompt injection for that run, so the report
will not precede or wrap your payload. Relative schema paths resolve against the
launch cwd, like `--prompt-file`.

For engines other than Codex there is no native schema enforcement.
Embed the schema in the prompt and parse the final message yourself. Delegate
still injects completion-report instructions unless you pass
`--no-completion-report`; when present, the report precedes any operator-requested
payload (payload-last ordering).

```bash
delegate --json codex safe --output-schema findings.schema.json "Audit auth handlers."
delegate --json cursor safe --no-completion-report "Return bare JSON matching the schema in the prompt."
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
delegate worktree remove <alias-or-runId> --force
```

`--force` combines discarding uncommitted edits with removing the branch.
`--keep-branch` leaves the branch in place and does not discard edits. These
flags can discard edits or delete unmerged branches. Use them only after
reviewing the worktree.

## CI does not have child runtimes

That is expected. Required tests do not need real Cursor, Droid, Codex, Claude,
Grok, Devin, OpenCode, or Kimi binaries:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests
```

Integration tests that launch real child agents should be separate from required CI.
