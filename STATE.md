# STATE — delegate-agent

Updated: 2026-09-07 (harness-compat audit shipped, promoted, published)

- `main` = `4c62010` (code at `a4c0b1d`), pushed to Forgejo. The 2026-09-07
  harness-compatibility audit is merged: every confirmed defect across the ten
  harnesses at current releases, structured-output eligibility unified, Cursor
  and omp prompts on stdin, workspace mail on by default with global
  `--no-mail`, skill-review preamble behind `tracking.skillReviewPreamble`
  (default off). Story and decisions:
  [REPORT](docs/audits/2026-09-07-harness-compat/REPORT.md); run log and
  rulings: [handoff](docs/handoffs/2026-09-07-overnight-harness-compat.md).
- Validation: 3,278 passed / 15 skipped at `a4c0b1d` via `scripts/gate.sh`;
  three cross-model reviews triaged in `docs/audits/2026-09-07-harness-compat/REVIEW-*.md`;
  live smoke rows in `SMOKE.md` there. CHANGELOG carries `0.31.0 - Unreleased`;
  no version bump, tag, or release.
- Installed runtime: `~/.delegate/releases/a4c0b1d23e95953f-573c837cc75f4d49`,
  `promotionMatchesRuntime: true`, previous payload retained. Installed config
  sets `mail.enabled: true` and `tracking.skillReviewPreamble.enabled: false`.
  Discovery cache re-probed for codex/claude/cursor/grok/omp; pi's `estate-pi`
  wrapper fails the version fingerprint (launches still work): `dlg-3em`.
  Ritual: [live runtime](docs/live-runtime.md), now with step 7.
- GitHub `main` (`8043bec`) is the *filtered* history — raw audit artifacts
  removed from every commit after `8e2b0d2` — mirrored as Forgejo
  `publish/main`. Forgejo `main` keeps them, so a direct GitHub push is
  non-fast-forward by design; procedure in
  [publishing checklist](docs/publishing-checklist.md). GitHub lags Forgejo by
  the publish-doc and ledger commits only.
- Open for Trey: Devin behavioral probe before widening the 3000.4.x gate;
  cursor safe stays without a harness mode (both read-only modes block the
  shell); eight dirty `delegate/*` worktrees under
  `~/Code/delegate-worktrees/e06d04efc0d7/` and nine merged `lane/*` branches
  await a deletion ruling. Open beads: `dlg-3em`, `dlg-cjz.4`, `dlg-o7i`, `dlg-y5c`.
