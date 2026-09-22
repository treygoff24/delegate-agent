# STATE — delegate-agent

Updated: 2026-09-22 (GPT-6 Sol/Luna + Claude Opus 5.5 bump; no new public release)

- `908abd6` moves the bundled Codex rows to the GPT-6 line and the bundled
  Claude row to `claude-opus-5-5`. `gpt-6-sol` and `gpt-6-luna` replace the
  `gpt-5.6-sol`/`gpt-5.6-luna` rows, `gpt-5.6-terra` is dropped outright, and
  the `pi`/`omp` bundled selectors follow to `openai-codex/gpt-6-sol`. Verified
  against live `codex debug models` (codex-cli 0.156.0) on **both** the work and
  personal profiles: Sol is `low`–`ultra` at a `medium` default, Luna is
  `low`–`max` at `medium`. `claude-opus-5-5` is real per Cursor's catalog, which
  carries the full `claude-opus-5-5-*` ladder; the Claude CLI exposes no
  non-interactive enumeration, so the bundled row is the advisory source.
- Gate at that revision: 3,398 passed, 15 skipped, 2,576 subtests, compileall
  clean, pinned Ruff check and format clean. One earlier full-gate run showed
  `test_structured_retry_timeout_exhaustion_reaps_every_workspace` failing
  `'relaunch' != 'resume'`; it passed in isolation and did not recur on the
  authoritative run. Treated as a pre-existing flake, not a regression — its
  file is untouched by this change and the assertion has no coupling to model
  ids. Worth a look if it reappears.
- Devbox live config bumped alongside (backup `config.json.bak-20260922-gpt6`):
  `codex.defaultModel` and the `sol`/`luna` aliases point at the GPT-6 ids, the
  `terra` alias is deleted, and the stale `gpt-5.6-*` rows are removed from
  `reasoning.capabilities.codex`. **Ruling:** no GPT-6 rows were added back to
  that config block. Fresh discovery carries exact evidence for both models and
  the bundled table now covers them as fallback, so a config row would only pin
  a default that can drift from the vendor's. `daybreak` keeps its config row.
  Effort source for sol/luna/astra now reads `discovery` rather than `config`.
- Devbox runtime promoted to
  `~/.delegate/releases/908abd6f48120313-70b116ee08d5d9e4`,
  `promotionMatchesRuntime: true`, no active supervisors, both profile
  discovery caches refreshed through the installed command. Live smokes served
  `gpt-6-sol` and `gpt-6-luna` (token echo), and a tracked `safe` run recorded
  `sol -> gpt-6-sol` with zero fallback hops and no continuity violation. Codex
  reports no served-model id (`servedModelSource: unavailable`), which is a
  harness limitation and matches the prior run's record.
- Effort ladders are honored end to end on the new runtime: `sol` at `ultra`
  forwards `model_reasoning_effort="ultra"`, and `luna` at `ultra` fails closed
  naming `low, medium, high, xhigh, max`.
- **The Mac is not bumped.** Its `~/.delegate/config.json` still carries the
  `gpt-5.6-*` ids and the `terra` alias, and its runtime predates `908abd6`.
  The same config edit plus `delegate-promote-checkout` is owed there.
- **Open for Trey: the Terra image lane.** The work-a global CLAUDE.md routes
  image generation to "a Terra lane via `delegate` (work mode) using its native
  `image_gen`". That alias no longer exists here. The Codex catalog exposes no
  `image_gen` gating on any model — Terra's row carried no image capability the
  GPT-6 rows lack — so re-pointing the lane at Sol or Luna is likely all that is
  needed, but that is unverified and the decision is his.
- `delegate doctor` reports one pre-existing warning unrelated to this change:
  `codex.profile 'delegate'` layers a `delegate.config.toml` that does not
  exist, so every codex run resolves no overlay.

## Previous record (2026-09-21)

- `642b9d6` bumps the bundled Grok declarations to 4.7. Cursor's 4.7 ladder
  drops the `cursor-` prefix (`grok-4.7-xhigh`, `grok-4.7-xhigh-fast`); the
  Grok CLI defaults to `grok-4.7`. Gate at that revision: 3,395 passed, 18
  skipped, compileall and pinned Ruff clean. Live config on the Mac and the
  devbox (`trey-agent`) now defaults cursor to `grok-4.7-xhigh-fast` and grok
  to `grok-4.7` (backups `config.json.bak-20260921-grok47`); smokes served
  `grok-4.7-build` (Mac Grok Build) and `Grok 4.7 256K Extra High Fast`
  (devbox Cursor).
- Mac runtime promoted to `~/.delegate/releases/642b9d642bd44240-81e4fb5efaa28bc1`,
  `promotionMatchesRuntime: true`, Grok discovery refreshed. Mac Cursor
  discovery is empty because the Cursor seat was logged out earlier that day
  (`cursorw login` re-seats it; then `delegate capabilities refresh`).
- Devbox runtime promoted the same evening after Trey ruled the warrant
  `wf_7816dc6fd0b9` and atlas `wf_7b3ed1ff49af` supervisors stale; both were
  ended with `delegate workflow kill` (no in-flight children), then
  `delegate-promote-checkout` installed
  `~/.delegate/releases/1955e0e21f315de5-81e4fb5efaa28bc1` (same runtime
  digest as the Mac's; only STATE.md differs). `promotionMatchesRuntime:
  true`, no active supervisors, work-profile discovery carries the full
  Cursor 4.7 ladder and both Grok 4.7 rows. The beads DB on both machines
  refuses writes pending a v53→v66 schema migration (designated-migrator
  decision for Trey), so this note is the ledger.

## Previous record (2026-09-17)

- Source repair: `3b4fffb` (bead `dlg-nqv`). Pinned OMP comparison now uses
  the event's separate provider/model fields without collapsing repeated
  namespace segments. Exact slash-free bare pins remain supported; ambiguous
  slash-bearing bare IDs require full provider qualification. Genuine provider
  and model switches still pause. Catalog warnings now recommend the persistent
  `capabilities refresh`, not the non-persistent `models --live` projection.
- Final gate at that source revision: 3,343 passed, 15 skipped, 2,574 subtests;
  compileall, pinned Ruff check, and Ruff format check passed. Native and
  cross-family review completed. Run subprocess-bearing tests with the project
  venv before mise shims on PATH: one intermediate gate failed when mise's
  automatic interpreter-install output polluted a launcher's stderr assertion.
  The assertion was retained; the concrete-interpreter rerun passed.
- Installed on the devbox with Trey's explicit approval:
  `~/.delegate/releases/3b4fffb5c8956e7a-6fe90b0d718e7bcc`.
  Doctor confirms `promotionMatchesRuntime: true`. The previous immutable
  `8cada28b3c4e6db6-35c4d931e9adf486` payload is retained; no active run was
  killed or overwritten. Outer launcher and config hashes are unchanged.
  Both profile discovery caches were refreshed through the installed command.
- A live configured OMP alias returned the exact smoke token under pinned
  continuity, with zero fallback hops, no violation, and no warnings. Honor
  the alias's supported thinking setting; do not override it with an effort
  that fresh discovery says the model does not support.
- This is a local source/runtime repair, not a PyPI or GitHub publication.

## Previous release record (2026-09-14)

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
