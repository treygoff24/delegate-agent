# STATE — delegate-agent

Updated: 2026-09-01 (evening; watchdog deletion shipped)

- **Engine at main 3c4054e, dogfooding live.** Today: watchdog rewritten by
  deletion (1a74bec, net −235 lines) — kills only on positive evidence
  (twice-confirmed ENOENT or terminal status); heartbeat file/writer,
  staleness window, watchdogTimeoutSeconds knob + env override all removed.
  Root-cause reports: docs/evidence/runkillers/ (three sol xhigh diagnoses).
  Full gate 2611 green; smoke: 8s journal-silent workflow survives.
- **Installed runtime** (~/.delegate/src) promoted to 1a74bec, stamped in
  ~/.delegate/last-promotion.json (digest 45b5c8b7…), smoked.
- **writing-plans is fully live** at 779585f (repo pushed, skill live-linked,
  install --verify green): receipt history, durable settled transitions,
  H1/H2/H3 review-machinery fixes, 1 MiB script cap match in this repo.
  Merge-receipt erasure (dlg-4v1) closed fixed-upstream there.
- **Work ledger is beads** (`bd ready`). Run-killer board is clear. Open
  follow-ups: dlg-cn8 (wire delegate doctor/promote CLI), dlg-80z
  (already_integrated retry guard, P3), dlg-1bg/dlg-oub/dlg-50p (polish),
  dlg-87d (blocked external). Human decisions labeled `human`: dlg-507
  (burst capacity), dlg-adl (ops-knob freeze — demoted, watchdog half moot;
  design E in docs/evidence/runkillers/2026-09-01-adl-config-freeze.md).
  dlg-swn GitHub push stays GATED on Trey.
- **Next:** add the fix set to this week's dogfooding runs; wp-zq7 in
  writing-plans (restore-chain adversarial pass) is the top engine follow-up.
- **Worktrees:** .worktrees/plan-burndown* kept (hold run records cited by
  bead close-reasons) — prune only with Trey's ok.
