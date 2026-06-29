# Security model

Delegate is a launcher and run recorder for other agent runtimes. It improves consistency and provides some isolation patterns, but it is not a complete sandbox.

## What Delegate controls

Delegate controls:

- Which child argv is built for Cursor, Droid, Codex, Claude, or Kimi.
- Whether the child is launched in `safe` or `work` mode.
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

- Cursor safe, Droid safe, Codex safe, Claude safe, and Kimi safe run in an isolated throwaway workspace by default, with your current working tree mirrored into that copy (see [What safe review can and cannot see](#what-safe-review-can-and-cannot-see) below).
- Cursor safe also writes a read-oriented `.cursor/cli.json` in the isolated workspace only.
- Codex safe uses `--ask-for-approval never exec --sandbox read-only`.
- Claude safe uses `claude -p` with stdin prompt transport, `--permission-mode plan`, `--strict-mcp-config`, Read/Grep/Glob, and selected read-only Bash tools. Delegate does not currently prove that Claude Code hooks, plugins, user settings, or other non-MCP customization surfaces are disabled.
- Droid safe uses Delegate's read-only safety prompt, does not add Droid work-mode unsafe flags, and uses the isolated temporary workspace as a defense-in-depth boundary.
- Kimi safe uses Delegate's read-only safety prompt and does not enable Kimi `--plan`. Kimi prompt mode auto-approves tool actions, so there is no runtime read-only enforcement for Kimi safe; the isolated temporary workspace is the effective boundary and the safety prompt is advisory.
- Explicit `--isolation none` is rejected for Cursor, Claude, Droid, and Kimi safe mode because it would remove the isolation/config boundary those safe contracts rely on. Codex safe may opt out of Delegate workspace isolation because the Codex read-only sandbox remains active.

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
- Kimi work uses edit-capable prompt mode. Delegate does not emit `--yolo` because Kimi rejects combining `--yolo` with `--prompt`.

#### Claude bypass scope

`describe`'s `policyFieldSupport` marks Claude as supporting `bypassApprovalsAndSandbox`, but the Claude harness honors that field only when set at `policy.harness.claude.work.bypassApprovalsAndSandbox`. Unlike Codex, the global `policy.work` scope and the `external-sandbox` profile do not grant Claude bypass. This is deliberate: it prevents a Codex-oriented global profile from silently broadening Claude Code permissions.

Delegate never auto-commits, pushes, merges, deploys, or publishes work-mode changes.

## Reasoning-effort boundary

`--reasoning-effort LEVEL` and JSON `reasoningEffort` request model thinking depth only. They do not change:

- Delegate `safe` or `work` mode.
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

Cursor safe, Droid safe, Codex safe, Claude safe, and Kimi safe normally run in a temporary Git worktree or directory copy, with uncommitted tracked edits and untracked, non-ignored files synced from the source working tree (gitignored paths excluded). This protects the source checkout from ordinary relative-path edits made inside the execution workspace. Delegate may still write `.delegate/` metadata in the source workspace for tracked runs.

Git repositories with commits use a detached temporary Git worktree. Non-Git directories use a temporary directory copy. Git repositories with no commits fall back to a temporary directory copy because Git cannot create a detached worktree from an unborn `HEAD`; Delegate reports that fallback in run metadata.

Delegate preserves internal symlinks during directory-copy and safe-worktree snapshot sync. Symlinks that resolve outside the source workspace are replaced with inert placeholder files inside the isolated workspace, and Delegate reports a warning that lists the relative paths it blocked. The placeholder does not include the external target path or target contents.

This protects the temporary safe workspace from accidental outside-file exposure through source-tree symlinks. It is not a full host sandbox: a child runtime may still read absolute paths, use credentials, call external tools, or perform network operations according to its own permissions.

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
