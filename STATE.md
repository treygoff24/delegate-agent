# STATE — delegate-agent

Updated: 2026-09-05 (devbox installed and promoted with explicit approval)

- Installed CLI: 0.30.0, reviewed application source `67abbc1`; doctor reports
  `executionMode: installed` and `promotionMatchesRuntime: true`. See `dlg-133`.
- Payload is versioned under `~/.delegate/releases/`; the bootstrap resolves to
  that immutable root. `~/.delegate/src` is a compatibility link. Installation
  and rollback rules: [docs/live-runtime.md](docs/live-runtime.md).
- Old payload, bootstrap, and promotion stamp are retained. No active Delegate
  process or supervisor was present at cutover; four live config files and
  1,368 existing pin files were unchanged. Do not prune retained worktrees.
- Installed checks passed: actual Codex read-only call; hermetic outer-shim
  routing, resumable followup argv, no-write dry-run, overview/reject help,
  scripts above 512 KiB accepted and above 1 MiB refused. OMP auth unverified.
- Source refactor `dlg-1vj` is complete. Full gates at `9aaa817`: unittest
  2,820 tests / 15 skips; pytest 2,805 passed / 15 skips; parity, compileall,
  Ruff 0.15.15, 79-module wheel contracts and native Codex scratch probe passed.
- Global Delegate/workflow skills are updated and synced to Forgejo; Codex and
  Claude resolve the same canonical files. See `dlg-133` for deployment receipts.
- Forgejo is the shipping remote. GitHub release/push remains separately gated
  (`dlg-swn`); this install did not publish a new package version.
- Open work and human decisions: `bd ready`. Older sitreps remain in Git history.
