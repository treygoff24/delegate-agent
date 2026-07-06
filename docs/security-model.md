# Security model

Delegate is a launcher and run recorder for other agent runtimes. It improves consistency and provides some isolation patterns, but it is not a complete sandbox.

## What Delegate controls

Delegate controls:

- Which child argv is built for Cursor, Droid, Codex, Claude, Grok, or Kimi.
- Whether the child is launched in `safe`, `work`, or stateless `call` mode.
- Whether a requested reasoning effort is translated into the supported child-runtime mechanism for the resolved harness/model.
- Whether the execution workspace is the source checkout, a temporary isolated workspace, or a persistent Git worktree.
- Prompt framing that tells sub-agents to review available skills and, in safe mode, avoid edits.
- Local run metadata under `.delegate/` for tracked runs.

Delegate does not control:

- The child runtime's implementation.
- The credentials, files, or network access available to the child process outside Delegate's execution workspace.
- Provider-side model behavior.
- Whether a provider interprets a reasoning-effort label as faster, slower, cheaper, or more expensive than expected.
- Absolute-path writes, shell commands, or external side effects a child runtime is allowed to perform by its own policy.

## Mode boundaries

### Safe mode

Safe mode is for review and investigation.

- Cursor safe, Droid safe, Codex safe, Claude safe, Grok safe, and Kimi safe run in an isolated throwaway workspace by default, with your current working tree mirrored into that copy (see [What safe review can and cannot see](#what-safe-review-can-and-cannot-see) below).
- Cursor safe also writes a read-oriented `.cursor/cli.json` in the isolated workspace only.
- Codex safe uses `--ask-for-approval never exec --sandbox read-only`.
- Claude safe uses `claude -p` with stdin prompt transport, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools. Delegate does not currently prove that Claude Code hooks, plugins, user settings, or other non-MCP customization surfaces are disabled.
- Droid safe uses Delegate's read-only safety prompt, does not add Droid work-mode unsafe flags, and uses the isolated temporary workspace as a defense-in-depth boundary.
- Kimi safe uses Delegate's read-only safety prompt and does not enable Kimi `--plan`. Kimi prompt mode auto-approves tool actions, so there is no runtime read-only enforcement for Kimi safe; the isolated temporary workspace is the effective boundary and the safety prompt is advisory.
- Grok safe uses Delegate's read-only safety prompt plus Grok `--sandbox read-only` and `--permission-mode dontAsk`. Delegate does not use Grok `plan` mode for safe review. Prompts are delivered via Grok `--prompt-file`.
- Explicit `--isolation none` is normalized to `auto` with a warning for Cursor, Claude, Grok, Droid, and Kimi safe mode because it would remove the isolation/config boundary those safe contracts rely on. Codex safe may opt out of Delegate workspace isolation because the Codex read-only sandbox remains active.

Safe mode is not a proof of zero side effects. Treat it as a defensive default plus prompt/runtime policy. A runtime could still read available files, use configured credentials, load its own customizations, or perform actions allowed by its own permissions.

#### What safe review can and cannot see

Safe mode reviews your **current working tree** — uncommitted tracked edits and untracked, non-ignored files are mirrored into an isolated throwaway copy (only gitignored paths are excluded), so you can review local changes without committing first or pasting a diff.

| Visible in the review copy | Not synced |
| --- | --- |
| Uncommitted tracked edits (vs `HEAD`) | Gitignored paths (`.env`, build artifacts, local secrets) |
| Untracked, non-ignored files | |

**Gitignored files are not synced** by design — that keeps secrets and build junk out of the throwaway copy. If a review needs an ignored file, commit it elsewhere, copy it in explicitly, or paste the relevant content into the prompt.

**Edge case:** a change staged then reverted in the working tree is not captured. Sync uses `git diff HEAD` (HEAD↔working tree), not the index, so index-only staging history can be invisible to the reviewer.

### Work mode

Work mode is edit-capable. Use it only for bounded tasks in workspaces you trust.

- Cursor work runs with edit-enabling Cursor flags.
- Droid work adds Droid's unsafe skip flag for non-interactive edits.
- Codex work uses the configured Codex policy and sandbox settings.
- Claude work uses `claude.workPermissionMode`; Delegate policy can explicitly map `policy.harness.claude.work.bypassApprovalsAndSandbox` to Claude `--permission-mode bypassPermissions`.
- Grok work uses `grok.workPermissionMode` and `grok.workSandbox`; Delegate policy can explicitly map `policy.harness.grok.work.bypassApprovalsAndSandbox` to Grok `--permission-mode bypassPermissions`.
- Kimi work uses edit-capable prompt mode. Delegate does not emit `--yolo` because Kimi rejects combining `--yolo` with `--prompt`.

#### Claude bypass scope

`describe`'s `policyFieldSupport` marks Claude as supporting `bypassApprovalsAndSandbox`, but the Claude harness honors that field only when set at `policy.harness.claude.work.bypassApprovalsAndSandbox`. Unlike Codex, the global `policy.work` scope and the `external-sandbox` profile do not grant Claude bypass. This is deliberate: it prevents a Codex-oriented global profile from silently broadening Claude Code permissions.

Grok bypass follows the same harness-scoped pattern at `policy.harness.grok.work.bypassApprovalsAndSandbox`.

Delegate never auto-commits, pushes, merges, deploys, or publishes work-mode changes.

### Call mode

Call mode is a stateless one-hop model call. It runs the child in an empty
temporary cwd, captures assistant text when available, deletes the temporary cwd,
and does not write a run registry entry, snapshot, or completion report. It also
does not inject safe/work skill framing.

Call mode is **write-capable by default** — it inherits work-level harness
permissions (sandbox/approval settings), just without a project tree to act on.
Pass `--read-only` to drop the child to read-only capability matching each
engine's `safe`-mode restriction; that variant also prepends a neutralizing
preamble telling the model there is nothing to inspect or mutate, which is the
intended contract for LLM-as-judge and grader use. `--read-only` applies only to
`call`.

Call mode is not a security sandbox. Even with `--read-only`, the child runtime
may still use configured credentials, network access, absolute paths, and
harness-native settings available to that process; on engines without a native
read-only sandbox (Cursor, Droid, Kimi) the preamble is the only restriction.
Use `safe` or `work` instead when the child should see the project tree or when
you need registry inspection.

## Reasoning-effort boundary

`--reasoning-effort LEVEL` and JSON `reasoningEffort` request model thinking depth only. They do not change:

- Delegate `safe`, `work`, or `call` mode.
- Temporary or persistent workspace isolation.
- Codex sandbox or approval policy.
- Claude permission mode.
- Droid unsafe/edit flags.
- Cursor force or MCP approval flags.
- Kimi prompt-mode approval behavior.
- Network access, credentials, or edit capability.

Unsupported effort/model combinations fail before launch. Treat higher effort as a possible latency/cost change, not as a safety control.

## Isolation boundaries

### Temporary safe isolation

Cursor safe, Droid safe, Codex safe, Claude safe, Grok safe, and Kimi safe normally run in a temporary Git worktree or directory copy, with uncommitted tracked edits and untracked, non-ignored files synced from the source working tree (gitignored paths excluded). This protects the source checkout from ordinary relative-path edits made inside the execution workspace. Delegate may still write `.delegate/` metadata in the source workspace for tracked runs.

Git repositories with commits use a detached temporary Git worktree. Non-Git directories use a temporary directory copy. Git repositories with no commits fall back to a temporary directory copy because Git cannot create a detached worktree from an unborn `HEAD`; Delegate reports that fallback in run metadata.

Delegate recreates an untracked symlink during snapshot sync (directory-copy, safe-worktree, and `--include-dirty`) only when all three hold: the link is relative, it resolves inside the source workspace, and its target is not gitignored. Any symlink failing those checks — an absolute target, an escape outside the tree, or a link pointing at a gitignored secret inside the repo — is replaced with an inert placeholder file, and the check fails closed to a placeholder on any ambiguity (for example an unexpected `git check-ignore` exit code). Delegate reports a warning listing the symlink paths it blocked; the placeholder contains neither the target path nor the target contents.

This closes a leak where an untracked symlink whose absolute target pointed at the repo's own gitignored secret would otherwise be recreated verbatim inside an edit-capable worktree, exposing that secret read/write to the child. It does not defend against hardlinks, which are indistinguishable from ordinary files by path and are documented as an out-of-scope caveat. It is not a full host sandbox either: a child runtime may still read absolute paths, use credentials, call external tools, or perform network operations according to its own permissions.

### Persistent worktree isolation

`--isolation worktree` with `work` mode creates a preserved Git worktree and local branch. The child edits that worktree, not the source checkout. The orchestrator can inspect and integrate the diff later.

Persistent worktree isolation is not a security sandbox. It does not prevent:

- Use of secrets available in environment variables, config files, credential stores, or runtime sessions.
- Network access allowed by the child runtime and host environment.
- Writes to absolute paths outside the worktree.
- Actions taken through authenticated tools, MCP servers, browser sessions, or external CLIs.

## Config and secret hygiene

- Keep real config in `~/.delegate/config.json`, a private `DELEGATE_CONFIG` path, or ignored workspace-local `.delegate/config.json`.
- Do not commit provider API keys, tokens, private model IDs that should not be public, local logs, or `.delegate/runs/` data.
- Keep `config.example.json` placeholder-only.
- Run secret and path scans before publishing.
- Profiles never store secrets. `profiles.definitions.*.env` is for non-secret routing pointers only (for example `CODEX_HOME`); secret-shaped keys are rejected at config load with `secret_in_profile_env`. Enforcement is by key name, so do not embed a credential in an innocuously named value or interpolate one via `$VAR` — keep real credentials in shell env or harness-native key stores. Resolved profile env is injected into child processes; only profile *names* are persisted in run state, and `delegate profiles` / dry-run echo env values through the same best-effort credential scrubbing as other surfaces.

### AI_PROFILE account-crossover guard

Some installs run every harness launch inside a shell that sets
`AI_PROFILE=work` or `AI_PROFILE=personal` to route which `~/.delegate/config.<profile>.json`
overlay (and which credential-bearing `keys.zsh`) applies. The failure mode
this guards against: a shell in `AI_PROFILE=work` with no `config.work.json`
yet must never silently fall back to launching on the base/ambient account —
that is a billing and credential crossover, not a cosmetic bug.

The guarantee is enforced in **two places**, both fail-closed by default:

1. **`delegate_agent.cli:main`** (`src/delegate_agent/profile_guard.py`). This
   runs inside the Python CLI itself, immediately after argv parsing and before
   any config load, workspace resolution, or child launch. It applies no
   matter how `delegate` is invoked: the installed pip console script,
   `python -m delegate_agent.cli`, or `bin/delegate.py`.
2. **`bin/delegate-profile-shim`**, a shell shim template some installs put in
   front of the Python entrypoint. It applies the same check even earlier,
   before Python starts, as defense in depth.

Both layers agree on the same rule: when `DELEGATE_CONFIG` is unset and
`AI_PROFILE` is exactly `work` or `personal`, and the matching overlay config
is missing or unreadable, launch and mutation commands (any engine, `run`,
`dry-run`, `wait`, `cancel`, `config`, `worktree remove`/`prune`/`gc`,
`capabilities refresh`) are refused. Read-only diagnostics (`profiles`,
`runs`, `run-output`, `snapshot`, cached `capabilities`, `describe`, `models`,
`worktree show`/`list`) still run, with a stderr warning that the check would
otherwise fail closed. Once `DELEGATE_CONFIG` is set — by the shim, after it
validates the overlay, or directly by a caller — the Python-layer guard does
not re-check `AI_PROFILE`; it treats an explicit `DELEGATE_CONFIG` as already
having answered the question.

An `AI_PROFILE` value that is set, non-empty, and not exactly `work` or
`personal` (a typo or an unrelated convention) is not a recognized profile, so
none of the above applies: both layers print a warning that Delegate is
running on the base account and proceed normally rather than failing closed —
there is no `config.<profile>.json` naming convention to check an unknown
name against.

## Output and redaction

Newly-created `.delegate/` registry directories and files are made owner-only on POSIX systems.

Delegate output may contain secrets the child runtime emitted. Credential scrubbing is best-effort defense-in-depth across run-output, snapshot, heartbeat, and discovery surfaces (`describe`/`models`). It catches recognizable shapes such as authorization headers, bearer/basic tokens, JWT-like strings, connection-string passwords, and common `token=` / `api_key=` / `password=` key-values. It does not guarantee removal of every secret (for example, a literal secret split across separate argv elements in config may still appear in discovery output). The real boundary for safe review is safe-mode isolation, not output scrubbing.

`snapshot` and `run-output` apply the same credential scrubbing by default. Raw local logs and child runtime state can still contain secrets. `--no-redact` intentionally disables display-side redaction on run-output and snapshot.

`--pass-through` streams raw child output and is incompatible with JSON mode. Use it only when raw child streaming is required.

## Recommended safe usage

1. Prefer `safe` mode for review and investigation.
2. Use `--json dry-run ...` before a new automation path.
3. Use persistent worktree isolation for edit-capable delegated work when you want source-checkout protection.
4. Review diffs before merging or cherry-picking child work.
5. Keep runtime credentials scoped and revocable.
6. Treat child runtimes as powerful local processes.

Report vulnerabilities through the process in [SECURITY.md](../SECURITY.md).
