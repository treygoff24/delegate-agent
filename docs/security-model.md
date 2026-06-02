# Security model

Delegate is a launcher and run recorder for other agent runtimes. It improves consistency and provides some isolation patterns, but it is not a complete sandbox.

## What Delegate controls

Delegate controls:

- Which child argv is built for Cursor, Droid, or Codex.
- Whether the child is launched in `safe` or `work` mode.
- Whether the execution workspace is the source checkout, a temporary isolated workspace, or a persistent Git worktree.
- Prompt framing that tells sub-agents to review available skills and, in safe mode, avoid edits.
- Local run metadata under `.delegate/` for tracked runs.

Delegate does not control:

- The child runtime's implementation.
- The credentials, files, or network access available to the child process outside Delegate's execution workspace.
- Provider-side model behavior.
- Absolute-path writes, shell commands, or external side effects a child runtime is allowed to perform by its own policy.

## Mode boundaries

### Safe mode

Safe mode is for review and investigation.

- Cursor safe and Codex safe run in an isolated temporary workspace by default.
- Cursor safe also writes a read-oriented `.cursor/cli.json` in the isolated workspace only.
- Codex safe uses `--ask-for-approval never exec --sandbox read-only`.
- Droid safe runs in the real workspace using Droid defaults; Delegate does not add Droid work-mode unsafe flags.

Safe mode is not a proof of zero side effects. Treat it as a defensive default plus prompt/runtime policy. A runtime could still read available files, use configured credentials, or perform actions allowed by its own permissions.

### Work mode

Work mode is edit-capable. Use it only for bounded tasks in workspaces you trust.

- Cursor work runs with edit-enabling Cursor flags.
- Droid work adds Droid's unsafe skip flag for non-interactive edits.
- Codex work uses the configured Codex policy and sandbox settings.

Delegate never auto-commits, pushes, merges, deploys, or publishes work-mode changes.

## Isolation boundaries

### Temporary safe isolation

Cursor safe and Codex safe normally run in a temporary Git worktree or directory copy. This protects the source checkout from ordinary relative-path edits made inside the execution workspace. Delegate may still write `.delegate/` metadata in the source workspace for tracked runs.

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

## Output and redaction

Newly-created `.delegate/` registry directories and files are made owner-only on POSIX systems.

`snapshot` and `run-output` redact secret-like strings by default, including authorization headers, bearer/basic tokens, JWT-like strings, and common `token=` / `api_key=` / `password=` key-values. Redaction is a last-resort safety feature, not a substitute for keeping secrets out of prompts and logs. Raw local logs and child runtime state can still contain secrets. `--no-redact` intentionally disables display-side redaction.

`--pass-through` streams raw child output and is incompatible with JSON mode. Use it only when raw child streaming is required.

## Recommended safe usage

1. Prefer `safe` mode for review and investigation.
2. Use `--json dry-run ...` before a new automation path.
3. Use persistent worktree isolation for edit-capable delegated work when you want source-checkout protection.
4. Review diffs before merging or cherry-picking child work.
5. Keep runtime credentials scoped and revocable.
6. Treat child runtimes as powerful local processes.

Report vulnerabilities through the process in [SECURITY.md](../SECURITY.md).
