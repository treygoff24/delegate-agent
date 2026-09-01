# STATE — delegate-agent

Updated: 2026-09-01 (night; Stack Upgrade lane WS-2/WS-3/WS-5 shipped after the OOM reboot)

- **Engine at main c408142.** Stack Upgrade Directive lane (bead dlg-nek,
  channel #stack-upgrade): typed terminal receipts (`terminalState` +
  `terminalRecord`, nine ratified values in `terminal_states.py`, seam
  verbatim with writing-plans), model provenance (requested/resolved/served,
  bounded fallback hops, sticky turn) and `--continuity-mode
  pinned|fungible|panel` with pinned-pause notice + handoff checkpoint
  (2721a7b, 9dbfe06); operator cancel overrides provider receipts on all
  three write paths via `apply_operator_cancel_override`. WS-5: workspace
  identity stays the git root while `Request.launch_cwd` drives spawn cwd,
  engine `--cd/--workspace` argv, `WORKSPACE_ROOT`, and dry-run
  `executionCwd` (9379aad, fa5c1d0). Three Grok review rounds
  (.delegate/runs grok-1/3/4) adjudicated on the channel. Gate: see the
  final lane report on #stack-upgrade for the c408142 run.
- **Installed runtime (~/.delegate/src) promoted to c408142** (Trey's call,
  2026-09-01 20:40): atomic rename swap, prior tree kept at
  `~/.delegate/src.prev-1a74bec` as rollback; `.installed-rev` marks the
  revision. Three plain runs were live during the swap (already-imported
  code unaffected; the atlasos workflow ran on its pin).
- Earlier today: watchdog rewritten by deletion (1a74bec, net −235 lines) —
  kills only on positive evidence; root-cause reports in
  docs/evidence/runkillers/.

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
