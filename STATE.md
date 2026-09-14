# STATE — delegate-agent

Updated: 2026-09-14 (0.31.0 published to PyPI; stale-cluster fix and supervisor signal relay hardening)

- `main` = the 0.31.0 release (runtime code through `27174af`; `2915e00` and
  `e9d8a4e` are test-only; version bump, CHANGELOG date, this file, handoff,
  and bead closes on top). Story, rulings, verification record, live dogfood,
  and the GitHub CI story:
  [handoff](docs/handoffs/2026-09-14-stale-cluster-relay-hardening.md). Beads
  `dlg-m5w`, `dlg-cbz`, `dlg-278.5`, and `dlg-278` are closed.
- Installed runtime: `~/.delegate/releases/27174af5269284df-1428ff3827b708eb`
  (= `27174af`, the last reviewed code commit; `2915e00` is test-only),
  `promotionMatchesRuntime: true`, previous payload
  `4b03b67e375ba6ae-6d2bad0f500840cd` retained. Discovery refreshed for work
  and personal from the installed command. Ritual: [live runtime](docs/live-runtime.md).
- Release 0.31.0 is published: GitHub `main` = `0d95555` (filtered history,
  see the [publishing checklist](docs/publishing-checklist.md); = source
  `e9d8a4e`), Forgejo `publish/main` the same; tag `v0.31.0` (`788b7d5`,
  annotated) and the GitHub release published 2026-09-14T21:27:36Z;
  `publish.yml` run 34898927518 uploaded `delegate_agent_cli-0.31.0` wheel and
  sdist to PyPI (upload 21:28Z, `latest=0.31.0`). CI run 34897626226 is green
  on macOS and 3.11–3.14. `v0.30.0` was never tagged (CHANGELOG 0.30.0 dated
  2026-08-22; its compare link dangles), so the 0.31.0 compare link spans
  `v0.29.1...v0.31.0`.
- CI: the first 0.31.0 run (34893537423) failed on macOS (EPERM from
  `os.killpg` in two new test helpers; one unexplained single occurrence in
  the nested-resume test) and on 3.11 (`dlg-278.5`, three runs in a row);
  `e9d8a4e` fixed the helpers and the 3.11 teardown wait. Watch the next
  macOS run for `test_parallel_nested_resume_keeps_child_replay_keys_byte_identical`;
  its failure message now prints the agent rows.
- Deferred from Astra review (nonblocking): the gate-preservation branch's
  aggregate `cancelled` list; summarized multi-child refusal details; the
  drain-before-publish ordering has no binding test. Relay arming's residue
  against asynchronous exceptions is documented in `_SignalRelay.start()` and
  accepted (Trey, 2026-09-14: stop the review loop after round 12).
- Open for Trey: the Devin behavioral probe (`DELEGATE_DEVIN_BEHAVIOR_TEST`
  against his Devin login; parked 2026-09-14, "we'll come back to it").
  `dlg-87d` stays blocked on hq-q34.3 Phase 1. Trey rulings 2026-09-14:
  `dlg-swn` (GitHub OSS push gate) and `dlg-507` (burst-capacity soft cap)
  closed; the nine merged `lane/*` branches deleted (they were local only);
  the installed runtime stays at `27174af` until the next real code change.
  The `e06d04efc0d7` worktree directory is gone; the `delegate-worktrees`
  entries on disk today belong to agent-memory and dwp-portals sessions.
- Parked idea (Trey, 2026-09-14): `dlg-dct` resident delegate lanes that stay
  alive between turns and wake on mail; `bd list --label idea`. Not to be
  built until Trey reopens it. The `delegate-agent` skill (skill-library
  `main` `7e366ee`) now documents the pattern that exists today:
  `--resumable` + `followup` for persistent context, workspace mail both ways.
- Working rule adopted this run: never edit the checkout while a gate runs
  (gate 7 on `e7a1acc` failed on a mid-mutation import).
