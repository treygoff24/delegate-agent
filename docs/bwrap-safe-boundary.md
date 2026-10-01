# Safe-mode bubblewrap boundary (Linux)

The agent-facing summary of the experimental `isolation.safeBackend` / `DELEGATE_SAFE_BACKEND=bwrap` boundary, moved word for word out of `AGENTS.md`. Read it before changing safe-mode isolation, `isolation.bwrapBinds`, or the bwrap argv builder; the user-facing detail is in `security-model.md` and `configuration.md`.

- Safe mode optionally runs zero-copy on Linux behind an experimental bubblewrap
  backend (`isolation.safeBackend` / `DELEGATE_SAFE_BACKEND=bwrap`): the workspace
  is read-only-bound with gitignore-parity masks, and every fail-closed condition
  (bubblewrap unavailable, a leaking untracked symlink, mask overflow, a missing
  or workspace-intersecting `isolation.bwrapBinds` entry, an initialized submodule)
  aborts the run instead of falling back to the copy backend. Inside the
  boundary `$HOME` and `/tmp` are tmpfs (emitted before every HOME-relative
  ro-bind, since bwrap mounts in argv order); the workspace `.delegate/`
  registry is masked and only the current run's scratch is rw-bound on top;
  only the selected engine's home override (`CODEX_HOME` / `CLAUDE_CONFIG_DIR`)
  is writable; the engine executable and its resolved target are ro-bound as
  files when they live outside the core roots; a linked worktree's common git dir is ro-bound; and
  `--pass-through` is refused because it execs outside the tracked launcher.
  Site launch surfaces are declared via `isolation.bwrapBinds`.
