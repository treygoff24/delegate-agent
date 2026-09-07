# STATE — delegate-agent

Updated: 2026-09-07 (simplification installed for local dogfood)

- Source work is on `feat/negative-diff-simplification`, tracked by `dlg-w7i`.
  The full audit is implemented: shared command policy, single mutable run record,
  targeted recovery, bounded maintenance, shared workflow lifecycle and worktree
  safety policy. Production source is net 301 lines smaller than baseline.
- Validation: 3,046 passed / 15 skipped / 2,241 subtests in the pinned parallel
  suite; compileall and both Ruff gates passed. A repo-local live child passed.
  Plan and code reviews, fixes, measurements and limits:
  [simplification results](docs/reviews/2026-09-07-simplification-results.md).
- Breaking changes: old workflow replay formats are refused; Droid models use
  `--model`; default discovery is compact and `--full` expands it. New runs do
  not write snapshot.json. Do not mix old and new writers in one run registry.
- Installed source `a270465` in a new versioned payload; doctor verifies parity.
  Live resumable launch, native followup and pinned workflow passed. Previous
  payload, outer shim and config are retained. Installation task: `dlg-4hu`.
  Installation and rollback: [live runtime](docs/live-runtime.md).
- Retained worktrees are preserved; Beads export changes remain unstaged.
  Unrelated Claude duplicate-final-text bug: `dlg-y5c`.
- GitHub push is authorized without a release, but held for the public-history
  decision recorded in `dlg-4hu`. Forgejo remains current. See `bd ready`.
