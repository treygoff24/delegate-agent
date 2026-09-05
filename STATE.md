# STATE — delegate-agent

Updated: 2026-09-05 (source improvement work in progress; not a live promotion)

- Source fixes and refactors are tracked by `dlg-1vj`. Final verification and
  CLI/JSON launch normalization are still in progress; do not treat this as a
  release or completion receipt.
- On this development host, checkout doctor reports
  `executionMode: checkout-or-pinned` and `promotionMatchesRuntime: false`.
  Source changes have not been installed into the live runtime. Verify the
  executing and installed artifact identities separately before debugging or
  promoting; the September 1 receipt below is historical, not current parity.
- The native Codex scratch probe passed on Linux with Codex 0.153.4: scratch and
  temporary-file writes succeed; source, metadata, symlink/hardlink escape, and
  network probes are denied. This is an offline sandbox check, not a provider run.
- Retain existing worktrees and rollback payloads. GitHub publication and live
  runtime promotion remain separate authorization boundaries.

## Historical receipt: September 1

- **Engine at main db50f3d; installed runtime (~/.delegate/src) promoted to
  db50f3d** with a real stamp (`delegate promote` at 23:34Z, `delegate doctor`
  → `promotionMatchesRuntime: true`). Rollback trees: `~/.delegate/src.prev-pre-db50f3d`
  (pre-tonight) and `~/.delegate/src.prev-1a74bec`.
- **Tonight's lane (bead dlg-cn8, closed):** `delegate doctor` / `delegate
  promote` wired into the CLI (docs/live-runtime.md "Promotion ritual",
  docs/cli-reference.md "Runtime doctor and promotion"); Claude
  `--output-schema` accepted in safe/work modes; workflow schema subset now
  mirrors Claude's `--json-schema` preflight. Five Sol xhigh review rounds,
  round 5 SHIP: docs/reviews/2026-09-01-doctor-promote-sol/. CHANGELOG
  Unreleased carries the entries. Papercuts pc2_02c321c1 and pc2_105033d7
  resolved.
- **Earlier today:** Stack Upgrade lane (dlg-nek, #stack-upgrade) — typed
  terminal receipts, model provenance, `--continuity-mode`, launch_cwd
  (2721a7b, 9dbfe06, 9379aad, fa5c1d0); watchdog rewritten by deletion
  (1a74bec; docs/evidence/runkillers/).
- **writing-plans is fully live** at 779585f; dlg-4v1 closed fixed-upstream.
- **Work ledger is beads** (`bd ready`). Open follow-ups: dlg-80z
  (already_integrated retry guard, P3), dlg-1bg/dlg-oub/dlg-50p (polish),
  dlg-87d (blocked external), dlg-3w5 (pointer repaired to bead + a1e2601).
  Human decisions labeled `human`: dlg-507 (burst capacity), dlg-adl
  (ops-knob freeze; docs/evidence/runkillers/2026-09-01-adl-config-freeze.md).
  dlg-swn GitHub push stays GATED on Trey — main is ~313 commits ahead of the
  GitHub mirror.
- **Known gap:** doctor's launcher check covers `~/.delegate/bin/delegate.py`
  only; the outer `~/.local/bin/delegate` profile shim is not hashed.
- **Next:** dogfood `delegate doctor` in the promotion ritual (hq tooling
  should call `promote` after every rsync); wp-zq7 in writing-plans
  (restore-chain adversarial pass) is the top engine follow-up.
- **Worktrees:** .worktrees/plan-burndown* kept (hold run records cited by
  bead close-reasons) — prune only with Trey's ok.
