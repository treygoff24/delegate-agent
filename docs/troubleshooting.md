# Troubleshooting

If you are troubleshooting from a source checkout, substitute the repo-local
entry point for installed examples:

```bash
python3 bin/delegate.py ...
```

## `missing_binary` / exit code 3

Real runs require the selected child runtime on `PATH`:

```bash
cursor-agent --version
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
delegate droid safe --model reviewer "Investigate only. Do not edit."
```

If editing `~/.delegate/config.json` does not change behavior, check the active
config layers:

```bash
delegate --json describe
```

Inspect `configResolution.layers`; `DELEGATE_CONFIG` can override the user
config. Workspace `.delegate/config.json` is reported but remains unapplied
unless selected explicitly through `DELEGATE_CONFIG`.

`omp` also raises `invalid_alias` for a bare model name that is not a key of
`omp.models` and not exactly the model ID of an entry in the discovered omp
catalog (`Unknown omp model alias 'kimi'`). OMP resolves a bare name by fuzzy
match against its own catalog and may serve a different provider, so Delegate
refuses instead of guessing. Use a configured alias or a full
`provider/model` selector (`delegate models omp` lists them). If the model is
real and new, refresh the catalog and retry:

```bash
delegate capabilities refresh
```

## `model_continuity_paused` on an OMP run

`Pinned model continuity refused a substitution: requested opencode-go/glm-5.3,
but omp tried to serve fireworks/glm-5p3` means the run named an explicit
provider and OMP moved it to another one anyway. A `provider/model` you typed
yourself is pinned by default and launches with OMP's own retry failover
switched off, so this is rare; an alias or `defaultModel` is not pinned unless
you named `--continuity-mode pinned`. When it happens the run (or, for `delegate
omp call`, the call, even if the child exited 0) is stopped and its output is
not the requested model's. To allow failover on purpose, rerun with
`--continuity-mode fungible`; the record then carries a `model_substitution`
warning naming the provider that answered.

## `unsupported_reasoning_effort`

Delegate validates requested reasoning effort against the resolved harness and model before launch:

```bash
delegate --json dry-run codex safe --reasoning-effort high "Review only."
delegate --json capabilities
```

Common causes:

- Codex effort was requested with a label not supported by the resolved model or the harness-default fallback capability.
- Codex `max` was requested for a model outside the bundled set that declares it — the GPT-6 models (`gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`) as of 2026-09 — without an exact config, profile-discovery, or legacy workspace-cache declaration.
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

Oh My Pi 18.1.13 reads a piped prompt in every non-protocol mode, including
`--mode json`, and Delegate delivers the prompt on stdin. Whitespace-only stdin
counts as no prompt at all, and the read blocks until EOF, so a caller that
holds the pipe open leaves the child waiting. Confirm the direct piped form
works, then inspect Delegate's planned argv:

```bash
printf '%s\n' "Reply with OK" | omp -p --mode json --no-session
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

Temporary bypass:

```bash
env -u AI_PROFILE delegate profiles
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

## Literal flag-like prompt text

Global options may appear anywhere before `--`, including after the subcommand
or inline prompt text:

```bash
delegate --json --cwd /path/to/repo dry-run codex safe "Review only."
delegate dry-run --json codex safe "Review only."
delegate codex safe --pass-through "Review only."
```

To send a token that looks like a global option as literal child-prompt text,
put it after the option terminator: `delegate codex safe -- --json`.

## `option_after_handle` on `resume` or `followup`

Everything after the handle is prompt text, so an option typed there would be
sent to the child instead of applying (`delegate followup x fix it --dry-run`
would start a real run). Delegate refuses that before launching. Move the option
before the prompt text (`delegate followup --dry-run x fix it`; `followup`
accepts options on either side of the handle, `resume` only before it), or, if
the token really is prompt text, put `--` in front of the prompt:

```bash
delegate followup x -- explain what --dry-run does
delegate resume x -- --model is the flag to change
```

## `session_expired` or `session-missing` on `followup`

`session-missing` means the source Run recorded no native session: it was
launched with `--no-resumable`, before Codex and Claude work Runs saved their
session by default, or as a workflow `agent()` call without `resumable=True`.
Use `delegate resume <handle> "<instructions>"`, which relaunches from the
original prompt and a report digest and needs no native session.

`session_expired` (failure kind `session_lost`) means the session was saved but
the resumed launch could not find it (Claude reports "No conversation found",
Codex "no thread with id"). The usual cause is a different account: the launch
ran under an account other than the one holding the session, for example when a
launcher chooses an account by usage on each start. Check that the followup runs
with the same profile and `CODEX_HOME` or Claude config directory as the source
Run, or use `delegate resume <handle>` to carry the report into a new Run. A
workflow structured retry that hits this falls back to a fresh relaunch on its
own; see [workflows](delegate-workflows.md).

## Provider errors, known-bad lanes, and automatic resume

When a child dies on a provider error, the failed run's envelope, run record,
and completion report carry a structured `providerError`:

```json
{
  "status": 401,
  "providerCode": "authentication_error",
  "message": "invalid x-api-key",
  "engine": "claude",
  "signature": "auth_rejected",
  "class": "persistent",
  "hint": "Re-authenticate the claude CLI, then relaunch."
}
```

The message is bounded and redacted. Classification looks at the HTTP status
first, then the provider's error code, then message text. One engine-keyed
signature table decides it, so the signature, hint, and class always agree, and
when a signature names a failure reason, `failureReason` and `failureKind` carry
it. One signature deliberately names none: plain throttling (a bare HTTP 429
with no quota wording) is recorded as `rate_limited`, but the run's failure
reason comes from its ordinary exit classification, so throttling never reads
as a usage limit and never steers credential rotation. `class` is one of:

- `persistent`: it will keep failing until someone changes something.
- `transient`: it clears on its own; relaunching later is reasonable.
- `unknown`: no signature matched. The record still carries the raw status and
  message. Unknown errors never mark a lane and are never resumed.

A `persistent` signature is either lane-scoped (the credentials, account,
billing, or model access are wrong, so the next launch on that lane fails the
same way) or request-scoped (this prompt is the problem, such as too many
images; the lane is healthy). Only lane-scoped persistent errors mark a lane.

| Signature | Class | Meaning and next step |
| --- | --- | --- |
| `auth_rejected` | persistent | HTTP 401: the provider rejected this lane's credentials. Re-authenticate the engine CLI. |
| `forbidden` | persistent | HTTP 403: the account may not use this model or region. Check access; re-authenticate if the login changed. |
| `auth_token_rejected` | persistent | The harness reports an expired or rejected token. Re-authenticate. |
| `claude_login_required` | persistent | Claude is not signed in or its login expired. Sign in again with `/login`. |
| `cursor_auth_required` | persistent | Cursor is not signed in. Run `estate-cursor login`. |
| `api_key_missing` | persistent | The provider behind an OMP, Pi, or OpenCode alias has no API key. Set it or pick another alias. |
| `payment_required` | persistent | HTTP 402: no credit on the account. Add credit. |
| `usage_limit` | persistent | Usage or quota limit reached (HTTP 429 with a quota message). Wait for the reset or use another account or lane. |
| `usage_balance_exhausted` | persistent | The provider's usage balance is exhausted. Top up or wait for the reset. |
| `model_unavailable` | persistent | HTTP 404: the account cannot use the requested model. Pick another alias (`delegate models`). |
| `age_confirmation_required` | persistent | The provider wants an age confirmation on this account. Complete it, then relaunch. |
| `harness_config_rejected` | persistent | The harness rejected its own configuration (an unsupported `-c` override or config key) before running. Fix the config or upgrade the harness. |
| `broker_binding_inactive`, `broker_uid_unmapped`, `broker_principal_not_cell` | persistent | The estate broker refused the launch (HTTP 403); no vendor process ran. Launch from a bound, mapped cell. |
| `request_image_limit` | persistent, this request only | HTTP 400, 413, or 422 naming an image count or size limit. This is not an auth failure. Attach fewer or smaller images. |
| `request_too_large` | persistent, this request only | HTTP 413: the request exceeds the size or context limit. Shorten the prompt or attachments. |
| `request_rejected` | persistent, this request only | HTTP 400 or 422: the provider called the request malformed. Fix what the message names. |
| `content_flagged` | persistent, this request only | The provider flagged the request as a possible cybersecurity risk. Rephrase or use another lane. |
| `thread_lost` | persistent, this request only | Codex could not find the saved thread. Relaunch without it; it is gone or lives under a different `CODEX_HOME`. |
| `rate_limited` | transient | HTTP 429 throttling. Wait a moment and relaunch. |
| `model_at_capacity` | transient | HTTP 503 or 529 saying the model is at capacity. Retry later or use another lane. |
| `provider_unavailable` | transient | HTTP 500, 502, 503, 504, or 529, or an overloaded message. Eligible for one automatic resume. |
| `stream_disconnected` | transient | The provider stream dropped before the response completed (for example Codex's websocket closing early). Eligible for one automatic resume. |

### Known-bad lanes

A lane is one engine on one provider, model, and account (the Codex failover
identity, the Claude config directory, or the auth profile). For every engine,
the credentials the child will see also count: API keys and tokens in its
environment, broker, realm, account, or endpoint settings, and for OpenCode the
provider keys, base URLs, and `Authorization` headers inside
`OPENCODE_CONFIG_CONTENT` (not its per-run persona or permission content). An
account label that looks like an email address is masked wherever the lane is
shown or stored. Two launches on
one model with different keys are different lanes, so a bad key never refuses a
healthy account. Delegate folds these into the lane as a salted hash (the salt
is `~/.delegate/state/lane-health.salt`, mode 0600); the key values are never
stored or shown, and `doctor` and the refusal only show a short
`[credential xxxxxxxx]` tag to tell such lanes apart. Rotating a key reads as a
new lane, which costs at most one failed launch to re-learn. A success clears
the lane's marker even when the store's lock is stuck, so a lane that has just
worked is never refused on its next launch. When a run fails
with a persistent lane-scoped signature, Delegate records a marker for that lane
under `~/.delegate/state/lane-health/` for `providerErrors.knownBadLaneMinutes`
(default 15). Until it expires, another launch on the lane is refused in
milliseconds, before any child starts:

```text
Refusing to launch: lane claude claude-sonnet-5-5 is marked known-bad until
...  after a persistent auth_rejected HTTP 401 failure. No child process was started.
```

The refusal exits `4` and the JSON error is `lane_known_bad`, with `signature`,
`class`, `hint`, `expiresAt`, `secondsLeft`, and `markedRunId`. Transient,
request-scoped, and unknown failures never write a marker, and a failure
Delegate established itself (timeout, stall, output cap, cancel) says nothing
about the lane. A success on the lane clears its marker. A Codex usage limit
with `codex.fallbackProfile` set stays with the profile failover, which swaps
accounts instead of refusing.

- `delegate doctor` lists live markers under `knownBadLanes`.
- `--force-launch` (a global option) launches on a marked lane anyway; a
  success clears the marker, another persistent failure renews it.
- `providerErrors.knownBadLaneMinutes: 0` turns the markers off. A corrupt marker
  file is removed with a warning and the lane is treated as healthy.
- Provider messages saved in a marker and shown in the envelope have credentials
  redacted and email addresses masked (`[email]`) before they are stored.

Workflows have their own stop on top of this: see
[workflows](delegate-workflows.md) for `provider_exhausted` and
`lane_known_bad` outcomes.

### Automatic resume after a transient drop

A Codex or Claude `work` run that fails with `stream_disconnected` or
`provider_unavailable`, and saved its native session (the default; see
`--no-resumable`), gets exactly one automatic continuation. Delegate launches it
the way `delegate followup` does: the same session, a new run linked by
`followupOf`. The continuation's envelope and manifest carry
`autoResume: {"automatic": true, "attempt": 1, "of": {...}, "trigger": {...}}`
so it never reads as an operator action. If the continuation drops too, the
result is final; there is no second attempt. If the continuation cannot be
built, the first run's envelope carries `autoResume` with `attempted: false` and
a `reason`.

It never applies to `safe` or `call` mode, pass-through, structured-output runs
(the workflow supervisor owns that retry), runs with no saved session, runs in a
temporary worktree (its files are gone), or any other error class. Set
`providerErrors.autoResume` to `false` to opt out. Workflow children run
unresumable unless the call passes `resumable=True`, so this applies to them
only then.

## Long foreground run looks silent

Tracked launches buffer child output so Delegate can return a bounded final
summary and preserve JSON stdout. For long-running foreground jobs, add
`--progress` after the mode and before prompt text, or set `progress.enabled`
to `true` in config and use `--no-progress` to override for one launch:

```bash
delegate --json claude safe --progress "Review only. Do not edit."
delegate --json droid work --model reviewer --progress "Implement the scoped change."
```

Progress messages go to stderr. They are intentionally bounded, credential-scrubbed
labels before printing, and do not include raw child output.
`--progress` is incompatible with `--pass-through`, which already streams raw
child output.

A tracked OpenCode run that sits at "no events yet" while it is still running was
once attributed to OpenCode buffering stdout until completion, observed against
v1.17.17. That did not reproduce against the emit pattern OpenCode's runner uses,
and the retest ran against a standalone Bun rather than the shipped binary, so
treat the cause as unsettled and check `--print-logs` stderr before concluding
the child is stuck.

## A succeeded Run's report says it is still waiting (`degraded`)

A Run reads `succeeded` but its completion report ends with "Waiting on the full
gate", "Monitor armed; waiting for both suites to finish", or "I'll commit when it
finishes". The child ended its turn with its job unfinished. A headless child
(`claude -p` and the other tracked engines) ends when the model stops talking, and
background Bash tasks and Monitors die with the session, so nothing was left to
wake it. Delegate records such a Run as `succeeded` (its edits are real and
adoptable) but marks it `degraded: true` with a `degradedReason`, a `degraded=...`
warning, and `degradedEvidence` saying what it saw. `wait`, `snapshot`, `runs`, the
launch envelope, `run-output --completion-report`, and a workflow's `agent_meta()`
all carry the flag; see [Degraded runs](cli-reference.md#degraded-runs-the-child-ended-its-turn-mid-job)
for the two reasons.

What to do: treat the unfinished job as not done. Run it yourself (or `followup` a
resumable Run and tell it to run the job in the foreground), then accept the work.
Do not read `succeeded` as "the gate passed".

Claude work Runs are launched with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and
`--disallowedTools Monitor` so the child cannot background a job in the first
place (plus `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` at the run's
`--timeout`, at least two hours, so a long foreground gate is not cut off at Claude
Code's 2 and 10 minutes), and every tracked work or safe prompt tells the child that
ending its turn ends the Run. If you set `claude.disableBackgroundTasks` to `false`, or the child
found another way (a shell `&` or `nohup` inside a foreground command), only the
after-the-fact detection remains. A false flag on a finished Run means its final
message was short, not shaped like a report, and matched a waiting phrase; the
`degradedEvidence` line quotes the phrase. Report the wording as a papercut.

## Need one-hop output instead of a tracked run

Use `call` mode when you just need a prompt answered and do not want Delegate to
resolve the repo, create `.delegate/runs`, inject completion-report framing, or
require a later `snapshot`/`run-output` lookup:

```bash
delegate --json codex call "Summarize this context."
delegate --json droid call --model reviewer --prompt-file prompt.md
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

## Bubblewrap safe backend refused

For eligible runs (non-Cursor safe mode on a Git workspace), the opt-in bwrap
backend never falls back to a copy once selected; Cursor and non-Git workspaces
use the copy path with a warning. Its errors identify the failed check:

- `bwrap_unavailable`: the host is not Linux, `bwrap` is missing or its
  production probe failed, or linked-worktree Git metadata was unavailable.
- `bwrap_symlink_leak`: an untracked symlink could expose a gitignored or host
  path.
- `bwrap_mask_overflow`: gitignore parity required more than 2000 masks.
- `bwrap_submodules_unsupported`: a submodule is initialized or could not be
  inspected.
- `bwrap_bind_missing` or `bwrap_bind_conflict`: a configured path is absent,
  or a writable bind covers or sits inside the read-only workspace.
- `bwrap_launch_failed`: the final mount plan could not be preflighted.

Fix the named condition, or explicitly select the copy backend for that run:

```bash
DELEGATE_SAFE_BACKEND=copy delegate codex safe "Review only. Do not edit."
```

`--pass-through` is also refused under bwrap because it bypasses the tracked
launcher that applies the boundary.

## Python older than 3.11

Delegate needs Python 3.11 or newer. `bin/delegate.py` and the `delegate_agent`
package check the interpreter first and exit `2` with a message naming the
version they found, instead of failing with an `ImportError` from inside the
package. On macOS a bare `python3` can resolve to Xcode's 3.9 when Homebrew is
not first on `PATH`.

Run it with a newer interpreter (`python3.12 bin/delegate.py ...`). The
profile-aware launcher shim (`bin/delegate-profile-shim`) picks a 3.11+
interpreter itself: `python3`, then `python3.14` down to `python3.11`, then
Homebrew's, then `/usr/local/bin/python3`. Set `DELEGATE_PYTHON=/path/to/python`
to choose one explicitly; an override older than 3.11 is refused rather than
bypassed. When none is found the shim exits `2`.

## Completion notification degraded

`--notify` uses the optional `post` CLI after terminal run state is persisted.
A send failure does not change the run result. Inspect only the notification
fields in the manifest:

```bash
jq '{notify, warnings}' .delegate/runs/<runId>/manifest.json
```

Stable reasons are `post_not_found`, `post_launch_failed`, `post_timeout`,
`post_failed`, and `notify_hook_failed`. Fix `post` availability or the target,
then use `--notify` on a later launch or `resume`; Delegate does not retry a
degraded send automatically.

A `room:<name>` ping is a workspace-room send. `post` never delivers a
room fan-out to the participant that sent it, and the sender is whichever
participant `post` resolves for the launching session. So `--notify` naming your
own room reaches the room's other participants but not the launching session
itself; the send still succeeds. Only an explicit `post send --to
participant:<id>` reaches the sender, and `--notify` has no such target. The argv
Delegate builds is checked against the installed `post` by
`tests/test_real_binary_contracts.py`.

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
raw child stdout/stderr streaming. Normal tracked runs already return bounded
parent-facing summaries.

Use inspection commands instead:

```bash
delegate snapshot <alias-or-runId>
delegate run-output <alias-or-runId>
delegate run-output <alias-or-runId> --completion-report
delegate run-output <alias-or-runId> --stderr --tail 100
delegate run-output <alias-or-runId> --stdout --tail 80 --max-chars 20000
```

Non-raw stdout/stderr output is bounded by both line tail and character cap.
Use `--raw` only when you intentionally need the full retained stream; it is incompatible
with `--tail` and `--max-chars`, may print very large output, and includes
`rawOutputBytes` in JSON metadata so callers can see how much raw output was
returned.

### OMP output compaction and the (opt-in) output cap

Tracked runs have no output cap by default: a verbose child is never killed or
truncated for its size, and `stdout.log` keeps growing. Two OMP shapes are still
compacted so the log stays readable. Delegate keeps the first 64 KiB of
recognized stripped `thinking_delta` records, then omits only that diagnostic
shape, and it shrinks an oversized `args` or `partialResult` on a
`tool_execution_update` record (OMP repeats the whole `task` sub-agent context on
every update) to a `delegateCompacted` stub with the original size, a head, and a
tail. A `delegate.capture` line in raw stdout (thinking only) and a result warning
disclose the compaction; `stdoutCapture` in the result or snapshot reports byte
counts, omitted and compacted records, limits, and a transport digest. These
counters cover the final attempt, not all retries combined. `--raw` cannot
recover omitted thinking or the compacted update text; `tool_execution_start` and
`tool_execution_end` still hold the full arguments and result.

To opt into a limit, set `<engine>.trackedStreamMaxBytes` to a positive byte
count. Only then does a run stop with `output_limit_exceeded` (the message names
the key to raise or set to `null`), and OMP's 16 MiB per-record and 256 MiB
transport ceilings apply. Check `stdoutCapture.limitKind` to distinguish a
retained-output, record, or transport limit. Call mode keeps its separate 16 MiB
caps. A capped, failed, or timed-out run still quotes the child's last
substantive text in its completion report.

## Parsing `events.jsonl` nested JSON

Tracked Runs mirror retained child stdout lines into `.delegate/runs/<runId>/events.jsonl`
as `stream.line` records, up to 500 lines followed by a
`stream.lines_truncated` marker. The 500-line limit applies to this mirror only:
the child keeps running, and `stdout.log` (flushed on every write) keeps the full
stream, so a stale `events.jsonl` is not a stalled child. Lines longer than 500 characters are clipped
with a `…` sentinel and marked `truncated: true` /
`textChars: <original length>`.
Clipped lines that contained nested JSON are no longer valid JSON payloads, so
skip them when reconstructing structured child events; plaintext lines that
never held JSON are skipped the same way:

```bash
jq -r 'select(.kind == "stream.line" and (.truncated != true)) | .text | fromjson? // empty' \
  .delegate/runs/<runId>/events.jsonl
```

Prefer `delegate snapshot` / `run-output` for parent-facing summaries. Use the
raw event log only for diagnostics, and treat it as sensitive: retained event
text is not redacted. The private `state.json` record also holds bounded
`recentEvents` diagnostics that can contain raw child text. Public snapshot
output is redacted by default; raw record files are not safe to share.

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

## `unknown_handle` for a run that exists

Runs are recorded per workspace under `<workspace>/.delegate`, so a handle only
resolves in the workspace you are in (`--cwd`, or the current directory). When it
misses, Delegate checks a roster of the other workspaces it has launched in
(`~/.delegate/registries.json`) and tells you where the run lives.

- A run ID found in one other workspace: `snapshot` and `run-output` read it
  directly and report `resolutionKind: "cross_registry"` with `resolvedWorkspace`.
  `wait`, `cancel`, `resume`, `followup`, and `worktree show` fail with the
  workspace and the exact command, for example
  `delegate --cwd /path/to/workspace wait del_...`; run that.
- A numbered alias such as `codex-1` is unique only inside one workspace, so it is
  never resolved for you. The error lists every workspace that has it; rerun with
  `--cwd` for the right one, or use the run ID.
- Nothing listed: the run was launched in a workspace that predates the roster or
  whose `.delegate` directory is gone. Pass `--cwd` for that workspace yourself.
  Workflow IDs behave the same way (`workflow_not_found`).

## Scratch space keeps growing

Safe-mode and isolated runs get a neutral scratch directory under
`~/.delegate/run-scratch/` (or `/var/tmp/delegate-<uid>/run-scratch/`), a sidecar
per mail-push engine home, and a compact temp directory under
`/var/tmp/dlg-<uid>/`. They survive failure so you can inspect them, but they
are given back once the run is terminal and older than
`tracking.retention.scratchDays` (default 3) whenever a workspace command runs
the ambient retention pass. To see or force it now:

```bash
delegate runs reclaim --dry-run          # per-run sizes, removes nothing
delegate runs reclaim --older-than 0     # every finished run, now
```

Running and stale runs are never touched, and the run record stays; `snapshot`
shows `scratchReclaimedAt` and `scratchReclaimedBytes`. A run listed under
`errors` was refused because its recorded path no longer matches the owned path
or an entry is owned by another user; the scratch is left in place for you to
inspect. A run listed as `budget_exhausted` (with `budgetExhausted: true`) was
large enough to run into the ambient pass's 20-second budget; part of its scratch
is already gone, it is not marked yet, and the next workspace command finishes it
(or run `delegate runs reclaim`, which has no time limit). The ambient pass is
skipped when `tracking.retention.enabled` is `false`; `delegate runs reclaim`
still works.

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

Files the launch seeded from a dirty source and the ledger paths in
`worktrees.retirementIgnoreGlobs` (`.beads/**`, `.papercuts.jsonl`) do not count
as uncommitted edits, so they never cause this refusal. If the refusal names a
live run (`run_active`, `run_not_terminal`, `process_group_alive`,
`worktree_leased`, `nested_run_active`, or `nested_registry_unreadable`),
`--force` will not override it; wait for the run, or pass `--kill-live` to remove
the worktree out from under it.
If it says `nested_worktrees_block_remove`, runs launched with `--cwd <this
worktree>` left dirty or unmerged worktrees of their own: each is listed in
`nestedWorktrees` with the exact command to remove it by name; the parent's
`--force` never reaches them. Clean, merged ones are removed automatically along
with the parent. `nested_registry_unreadable` (a nested Registry that cannot be
read or locked) and `nested_worktree_remove_failed` (a nested removal failed;
the parent is kept) name the path or nested result to fix. `worktree remove
--group NAME` and handles match only the Registry of the workspace the command
runs in (the error names it as
`registryRoot`); pass `--cwd` for the workspace that launched the run. Edits to those ledger paths are not lost on
removal: they are copied to `<Registry>/salvage/<worktree>-<timestamp>/` first,
and the result names that path.
A pooled path with no run record is removed with `worktree reap --path <path>
--older-than 0 --yes --force`.

## CI does not have child runtimes

That is expected. Required tests do not need real Cursor, Droid, Codex, Claude,
Grok, Devin, OpenCode, Pi, Oh My Pi, or Kimi binaries:

```bash
python3 -m compileall -q src tests bin
python3 -m pytest -q
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
the macOS `sun_path` limit for socket-creating tools. Current releases place
scratch under a shorter neutral global root rather than the workspace registry.
Do not override `TMPDIR` in a safe run: an arbitrary replacement is outside the
write grant. If the neutral path still exceeds a tool's socket limit, shorten
the user-home path or report the exact socket path so Delegate can reduce its
owned prefix without broadening write access.
