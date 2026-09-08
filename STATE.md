# STATE — delegate-agent

Updated: 2026-09-08 (ci-burn landed, published, promoted; one CI confirmation pending)

- `main` = `80dd9ed` (last code change `8b79949`), pushed to Forgejo. The
  ci-burn batch (macOS pinned-runtime publication, `ps -ww`, per-engine
  `trackedStreamMaxBytes` pinned at build, discovery basename rule, assistant-text
  dedupe) is merged after two review rounds; story, rulings, and CI rounds:
  [handoff](docs/handoffs/2026-09-07-ci-burn-babysit.md). CHANGELOG carries
  `0.31.0 - Unreleased` with a "CI and runtime hardening" subsection; no version
  bump, tag, or release.
- CI: run 34177243555 on GitHub `c905335` (= source `a1ebd33`) is green on
  macOS, 3.12, 3.13, 3.14; 3.11 hit one flake (`test_run_surfaces_check_warnings_at_launch`
  teardown vs detached supervisor). Fix is `8b79949` (test-only), on Forgejo,
  **not yet on GitHub** — needs Trey's per-push authorization. Then close
  `dlg-278.5` and parent `dlg-278`.
- Installed runtime: `~/.delegate/releases/4b03b67e375ba6ae-6d2bad0f500840cd`,
  `promotionMatchesRuntime: true`, previous payload retained. Config keeps
  `mail.enabled: true`, `tracking.skillReviewPreamble.enabled: false`. Discovery
  re-probed both profiles; cursor is unauthenticated in the personal realm only
  (expected). Ritual: [live runtime](docs/live-runtime.md).
- GitHub `main` is the *filtered* history (raw audit artifacts stripped after
  `8e2b0d2`), mirrored as Forgejo `publish/main`; direct push is non-fast-forward
  by design. Procedure: [publishing checklist](docs/publishing-checklist.md).
  Filter on a fresh clone before fetching github refs.
- Skill: `~/.agents/skill-library/delegate-agent` split into core +
  `references/fleet.md` + `references/runtime.md`; reviewer ranking per Trey
  2026-09-08 (Astra top; Sol/Opus/Cursor Grok 4.6 xhigh close seconds).
- Open for Trey: `dlg-507` burst-capacity decision; `dlg-swn` GitHub OSS push
  gate; Devin behavioral probe; dirty `delegate/*` worktrees under
  `~/Code/delegate-worktrees/e06d04efc0d7/` and merged `lane/*` branches await a
  deletion ruling. Follow-up if the 3.11 flake class recurs: sweep the other
  launch-and-return sites in `tests/test_workflow_commands.py`.
