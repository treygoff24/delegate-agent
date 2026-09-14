# STATE — delegate-agent

Updated: 2026-09-14 (stale-cluster fix and supervisor signal relay hardening released as 0.31.0)

- `main` = the 0.31.0 release commit (code through `2915e00`; version bump,
  CHANGELOG date, this file, handoff, and bead closes on top). Story, rulings,
  verification record, and the live dogfood results:
  [handoff](docs/handoffs/2026-09-14-stale-cluster-relay-hardening.md). Beads
  `dlg-m5w` and `dlg-cbz` are closed.
- Installed runtime: `~/.delegate/releases/27174af5269284df-1428ff3827b708eb`
  (= `27174af`, the last reviewed code commit; `2915e00` is test-only),
  `promotionMatchesRuntime: true`, previous payload
  `4b03b67e375ba6ae-6d2bad0f500840cd` retained. Discovery refreshed for work
  and personal from the installed command. Ritual: [live runtime](docs/live-runtime.md).
- Release 0.31.0: Forgejo `main` pushed; GitHub `main` (filtered history, see
  the [publishing checklist](docs/publishing-checklist.md)) and Forgejo
  `publish/main` receive the same filtered commits; tag `v0.31.0` and the
  GitHub release publish to PyPI through `publish.yml`. `v0.30.0` was never
  tagged (CHANGELOG 0.30.0 dated 2026-08-22; its compare link dangles), so the
  0.31.0 compare link spans `v0.29.1...v0.31.0`.
- CI: `dlg-278.5` (3.11 teardown flake, fix `8b79949`) and parent `dlg-278`
  close once the 0.31.0 publish's CI run is green on 3.11.
- Deferred from Astra review (nonblocking): the gate-preservation branch's
  aggregate `cancelled` list; summarized multi-child refusal details; the
  drain-before-publish ordering has no binding test. Relay arming's residue
  against asynchronous exceptions is documented in `_SignalRelay.start()` and
  accepted (Trey, 2026-09-14: stop the review loop after round 12).
- Open for Trey: `dlg-507` burst-capacity decision; `dlg-swn` GitHub OSS push
  gate; Devin behavioral probe; dirty `delegate/*` worktrees under
  `~/Code/delegate-worktrees/e06d04efc0d7/` and merged `lane/*` branches await a
  deletion ruling. `dlg-87d` stays blocked on hq-q34.3 Phase 1.
- Working rule adopted this run: never edit the checkout while a gate runs
  (gate 7 on `e7a1acc` failed on a mid-mutation import).
