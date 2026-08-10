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
command -v pi
command -v omp
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
`opencode.binary`, `pi.binary`, `omp.binary`, `droid.binary`, `kimi.binary`, or `cursor.argvPrefix`.
JSON errors include `configPath`, `configKey`, and, when Delegate sees a likely
user-local install, `suggestedBinaryPath`.

The OpenCode curl installer normally writes `opencode` under
`~/.opencode/bin`, which is often absent from the `PATH` inherited by agents and
other non-interactive processes. Add that directory to their `PATH` or set
`opencode.binary` to the absolute executable path.

After fixing `PATH` or a configured binary selector, refresh discovery:

```bash
delegate --json setup
```

Setup creates a minimal config only when none exists. It never repairs or
rewrites an existing config.

## Setup is not ready, or cached discovery is stale

`delegate --json setup` reports `discoveryReady` separately from `ready`.
Discovery can succeed while no harness is launchable. The per-harness
`nextAction` explains the remaining requirement. Droid, for example, stays
unlaunchable until a model is passed or `droid.defaultModel` is configured.

Use the smallest command that answers the question:

```bash
delegate --json models --summary
delegate --json models <engine> --live
delegate --json capabilities
delegate --json capabilities refresh
```

`models <engine> --live` performs a fresh one-off probe and never writes config
or cache. `capabilities refresh` probes all supported harnesses and updates the
active auth profile's cache. Add `--auth-profile NAME` before the command when
you need a defined profile other than the detected/default one.

Refresh is last-known-good by harness. One successful record can be written
while another failed probe retains its previous record and appears in
`staleHarnesses`. A configured executable selector that no longer matches the
cached selector is different: cached diagnostics mark that harness `stale`, and
ordinary launches ignore its discovered record until setup or refresh succeeds.

If setup reports `config_changed_during_setup`, another process created or
changed the config during probing. Its file was preserved. Rerun setup so the
profile and selectors are resolved from that current config.

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

Inspect `configResolution.layers`; `DELEGATE_CONFIG` can override the user
config. Workspace `.delegate/config.json` is reported but remains unapplied
unless selected explicitly through `DELEGATE_CONFIG`.

## `unsupported_reasoning_effort`

Delegate validates requested reasoning effort against the resolved harness and model before launch:

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."
delegate --json capabilities
```

Common causes:

- Codex effort was requested with a label not supported by the resolved model or the harness-default fallback capability.
- Codex `max` was requested for a model other than `gpt-5.6-sol`, the only bundled Codex model that supports it as of 2026-07, without an exact config, profile-discovery, or legacy workspace-cache declaration.
- Claude effort was absent from the installed harness's discovered native enum and from Delegate's bundled compatibility labels.
- Grok exact model declarations override its harness-wide compatibility enum; an effort can therefore be valid at the flag level but rejected for the selected model.
- OpenCode rejects a variant missing from an exact discovered variant menu. Without exact evidence, it preserves pass-through behavior and records `opencode_variant_unvalidated`.
- Pi effort must be `low`, `medium`, `high`, `xhigh`, or `max`; alias-only `thinking` may also use `off` or `minimal`.
- Pi models advertised with thinking disabled reject effort; thinking-enabled models use the harness-wide label menu as partial model evidence.
- Oh My Pi effort uses the same levels and alias-only `thinking` values as Pi. Exact per-model validation applies only when its catalog provides a `thinking` array.
- Cursor effort was requested but no applicable selector route exists. Without an explicit model pin, `cursor.reasoningEffortModels.<level>` can supply it. With a pin, that global map cannot override the selector, so an explicit effort requires an exact discovered same-family route. Cursor effort uses model selection rather than a standalone effort flag.
- Droid or Codex model support is not in config, exact profile discovery, the legacy workspace cache, or bundled fallback data.
- Kimi and Devin expose no Delegate reasoning-effort transport, even if harness metadata mentions an internal effort concept.
- The effort string is misspelled. Delegate treats labels literally and does not translate between provider naming schemes.

These failures apply to explicit per-run effort (`--reasoning-effort` or JSON run input). A config `defaultReasoningEffort` that current exact discovery or compatibility evidence cannot satisfy degrades to no requested effort and records a warning rather than failing the launch. Claude and Grok defaults are transport-safe strings at config load; runtime discovery/manual exact evidence is authoritative, so newly advertised labels do not require a Delegate release. For Kimi, `--reasoning-effort` is rejected outright: the Kimi CLI exposes no effort flag (k3 supports effort internally via `~/.kimi-code/config.toml`), so `kimi.defaultReasoningEffort` must stay `null`.

For private or newly released models, inspect the active profile's discovery
first. Use `reasoning.capabilities` in config for a deliberate Codex, Droid, or
Grok override. Refresh with:

```bash
delegate --json capabilities refresh
```

Refresh is not read-only: it invokes child metadata commands and atomically
writes the selected profile's private user cache after at least one valid
result. The older `.delegate/capabilities/reasoning.json` file is still read at
lower precedence but is no longer a refresh target. Do not commit that legacy
runtime state.

## OpenCode silently ignores an unknown variant

When Delegate has an exact discovered variant menu for the selected OpenCode
model, it rejects an unknown variant before launch. When no exact menu exists,
Delegate retains pass-through compatibility and records the warning
`opencode_variant_unvalidated`; OpenCode can then silently ignore a bogus
variant. Inspect discovery and the dry-run payload before retrying:

```bash
delegate --json models opencode --live
delegate --json dry-run opencode safe --reasoning-effort high "Review only."
```

## Pi cannot find a model or provider login

Pi owns provider authentication under `~/.pi/agent`. Delegate never passes
`--api-key`. Confirm Pi works directly, then inspect the exact Delegate argv:

```bash
pi -p --no-session "Reply with OK"
delegate --json dry-run pi safe --model provider/model-id "Review only."
delegate --json models pi --live
```

## Oh My Pi exits after only a session event

Oh My Pi 17.0.4 was observed to exit successfully without processing piped
stdin in non-interactive JSON mode. Delegate therefore passes the resolved
prompt as a positional argument. Confirm the direct positional form works, then
inspect Delegate's planned argv:

```bash
omp -p --mode json --no-session "Reply with OK"
delegate --json dry-run omp safe --model provider/model-id "Review only."
delegate --json models omp --live
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

`setup`, `capabilities refresh`, and `models <engine> --live` are not read-only:
they run account-sensitive metadata probes, and the first two can write the
profile cache. The guard blocks them when the recognized overlay is missing.

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

## Devin safe reports `unsupported_mode`

Devin may implement a read-only filesystem survey through the generic `exec`
tool. Delegate cannot permit that tool in safe mode without weakening the
read-only boundary, so `delegate devin safe` is rejected before launch. Use
another safe Harness for filesystem review; Devin work and call modes remain
available.

## Safe-mode isolation fails

Cursor, Droid, Codex, Claude, Grok, OpenCode, Pi, Oh My Pi, and Kimi safe create an
isolated throwaway workspace by default. Safe mode reviews your **current working tree**,
uncommitted tracked edits and untracked, non-ignored files are mirrored into
that copy (only gitignored paths are excluded), so you do **not** need to
commit or stash before a safe review. In Git repositories, Delegate first tries
a detached worktree and syncs the dirty tree into it; for non-Git directories
and some Git fallback cases, it uses a directory copy. Codex safe is the only
safe harness that may opt out with `--isolation none`, because Codex still
keeps its read-only sandbox active. Cursor, Droid, Claude, Grok,
OpenCode, Pi, Oh My Pi, and Kimi safe
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
shows the planned branch/path but does not run the full launch preflight. Ordinary
tracked and untracked source changes auto-sync; commit or stash dirty submodules,
or choose a different isolation mode, before launching.

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

For a bare machine-parseable final message on Codex, use `--output-schema FILE`.
OpenAI enforces the JSON Schema on Codex's final message; Delegate
suppresses the completion-report prompt injection for that run, so the report
will not precede or wrap your payload. Relative schema paths resolve against the
launch cwd, like `--prompt-file`.

Delegate preflights Codex strict schemas recursively. It supplies a missing
`additionalProperties: false` in a temporary copy and warns; it does not edit
the source file. Every object property must already appear in `required`, because
auto-requiring an optional field would change the schema's meaning. Incomplete
`required` lists and explicit non-false `additionalProperties` fail immediately
as `invalid_output_schema` with the failing schema path.

Tracked child failures use `usage_limit`, `auth_failed`, and
`codex_thread_lost` when recognized, otherwise `child_failed`. Inspect the
typed message first, then use `delegate run-output <handle> --stdout` or
`--stderr` for raw diagnostics. On retry-safe `codex_thread_lost` failures,
Delegate retries once; if the same signature repeats it automatically tries an
ephemeral `--ignore-user-config` launch and records the fallback in the
envelope/events. Write-capable calls are not retry-safe and return the typed
failure after the first attempt.

Claude call mode also supports native `--output-schema`; other engines require
embedding the schema in the prompt and parsing the final message yourself.
Delegate still injects completion-report instructions unless you pass
`--no-completion-report`; when present, the report precedes any
operator-requested payload (payload-last ordering).

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
Grok, Devin, OpenCode, Pi, Oh My Pi, or Kimi binaries:

```bash
python3 -m compileall -q src tests bin
python3 -m unittest discover -s tests -t .
```

Integration tests that launch real child agents should be separate from required CI.

## `spawn_agent` fails with "no thread with id" inside a delegate child

Claude Code harness bug when forking session history in a delegate-launched
session. Not a delegate defect. Workaround: spawn with `fork_turns: none`
(loses inherited context but works).

## kimi launch fails with an unknown-model error while `delegate models` lists the alias

The kimi-code CLI's own `config.toml` is missing the model entry (machine
config drift). Fix the harness config — delegate forwards the alias as
configured.

## Never run `npm link` from inside a delegate/codex worktree

It repoints the machine-global package symlink at an ephemeral worktree path
that later vanishes.

## Node/tsx children fail with EINVAL on Unix IPC sockets

Delegate gives each run a private scratch `TMPDIR` whose deep path can exceed
the macOS `sun_path` limit for socket-creating tools. Workaround: have the
child set `TMPDIR=/tmp` (or another short dir) for those tools. The private
scratch dir is deliberate isolation, not a bug.
