# 2026-09-07 ci-burn babysit — seven delegate lanes on the open bug ledger

Operator: Fable session (personal realm). Trey asked to burn free Codex tokens before the subscription reset; then `/babysit`.

## Run state

Group `ci-burn`, launched 20:30Z from `main` d45b522, each `codex work --isolation worktree --resumable --prompt-file /var/tmp/dlg-lanes/briefs/<bead>.full.md`. Envelopes in `/var/tmp/dlg-lanes/runs/<bead>.json|.err`. Worktrees under `~/Code/delegate-worktrees/e06d04efc0d7/codex-20260907T2030*`. Handle→bead mapping is by launch order (confirm from each run's model/diff at completion):

| handle | bead | model | brief |
|---|---|---|---|
| codex-72 | dlg-278.1 macOS pin publish PermissionError | astra high | 278-1-macos |
| codex-73 | dlg-278.2 Linux proc-reaping CI flakes | sol high | 278-2-linux-proc |
| codex-74 | dlg-y5c double-append assistantText | luna medium | y5c-double-append |
| codex-75 | dlg-3em pi fingerprint | luna medium | 3em-pi-fingerprint |
| codex-76 | dlg-cjz.4 mise stderr in shim tests | luna medium | cjz4-mise-stderr |
| codex-77 | dlg-o7i tracked stdout cap | sol medium | o7i-output-cap |
| codex-78 | dlg-j0q gate key stability | sol medium | j0q-gate-key |

CI evidence: GitHub run 34132106612 on main 8043bec, logs at `/var/tmp/dlg-lanes/ci/{macos,linux312}-8043bec.log`.

## Procedure per landed lane

1. `delegate run-output codex-N`; read `LANE_REPORT.md` in the worktree; `git -C <wt> diff main --stat`.
2. Diff review by the operator: touched tests/gates, new mocks, weakened assertions.
3. Merge branch into `main` locally (no squash needed); run `scripts/gate.sh` on main after each merge (or batch when several land close together).
4. Cross-family review of the merged diff: `delegate claude safe --model opus --reasoning-effort high` on an untracked diff file under `review-input/`; Astra final gate at the end over the whole batch.
5. Fix loops via `delegate followup codex-N`.
6. Close beads with `--reason` pointing at the merge commit. Push Forgejo. GitHub push stays gated — ask Trey.

## Landed
- codex-74 = dlg-y5c (mapping confirmed by diff): merged e7c96ef, closed 730d425. Lanes commit `LANE_REPORT.md`; `git rm` it on main after each merge.

- codex-75 = dlg-3em (confirmed by diff): merged dff62e1.

- codex-76 = dlg-cjz.4 (confirmed by diff): merged, test-only.

- codex-78 = dlg-j0q (confirmed): audit found no volatile field; test-only merge.

- codex-72 = dlg-278.1 (Astra, confirmed): merged d8dc170 (workflow_pinning). Astra found a second seal-before-rename site in workflow_attempts.py:172-174; followup launched on codex-72 (envelope /var/tmp/dlg-lanes/runs/278-1-fu.json) with the write boundary extended. Followup e0df7cf merged. Bead stays open until macOS CI runs (needs a gated GitHub push).

- codex-73/80 = dlg-278.2 (confirmed): merged, test-helper-only (ps -ww, COLUMNS=80 pins, watchdog cap). Closed.

- codex-77/81 = dlg-o7i (confirmed): merged. All seven lanes landed; endgame = quiet full gate + Opus cross-family review + Astra final gate + Forgejo push.

## Failure modes seen
- 2026-09-07 ~21:10Z herdr update killed the Claude session and with it the parent processes of codex-73 (dlg-278.2) and codex-77 (dlg-o7i) mid-gate; both marked stale with uncommitted diffs intact. Resumed via `delegate followup codex-73|77 --prompt-file briefs/resume-after-kill.md` (envelopes runs/278-2-fu.json, runs/o7i-fu.json).
- Full `scripts/gate.sh` inside a lane shows 1 teardown error from the linked-worktree registry-lock guard while sibling ci-burn runs are live. Not a code failure; run the full gate on main once the group is quiet.

## Review round 1 (Astra xhigh, safe, over batch diff 57c3796..2064fab)
- Major: workflow_pinning rmtree onerror retries by pathname (symlink-race). Followup on codex-72 (runs/278-1-fix.json).
- Major: runner._tracked_stream_max_bytes reloads config at execute time; carry from launch config. Followup on codex-77 (runs/o7i-fix.json).
- Items 2,3,4,5,7 clean. Opus review still running (runs/review-opus.json).

## Review round 1b (Opus high, safe): verdict SHIP, 9 minor/nit
- #1 multi-text-block dedupe gap, #3 explicit accepts other-harness basename, #6 configuration.md row, #9 watchdog comment → Luna cleanup lane (briefs/opus-minors.md, runs/opus-minors.json).
- #2 raw PermissionError from rmtree path → fold into pinning after Astra fix lands. #4 same as Astra major 2. #5 manifest field resume-table row, #7 AssertionError→RunnerLaunchError → after Sol fix lands (runner.py owned by codex-77).
- #8 publication window 0o700 between rename and seal: accepted (same-uid, forced by macOS rename semantics).

- Astra major 2 fixed: Sol followup 8bbbdc0 merged 431ecdf (limit pinned in Request/RunContext). Opus #5/#7 done by operator in 7fed990. Remaining in flight: Astra rmtree fix (runs/278-1-fix.json), Luna minors (runs/opus-minors.json); then Opus #2 fold-in, re-gate, Forgejo push.

- Astra major 1 fixed: eb2d185 merged 3e2ce06. Opus #2 done by operator 2e6609c. Luna minors merged ffac99f. All review findings closed.

## Gate
- Quiet full gate on main at 09658a7 (code = 2064fab): 3294 passed, 15 skipped, GATE PASS ruff 0.15.15 (log runs/gate-main.log). Final gate at 2e6609c (run as systemd unit dlg-gate-final after the harness memory heuristic reaped two background attempts): 3301 passed, 15 skipped, GATE PASS ruff 0.15.15.

## Outcome
All seven beads landed on main with two review rounds (Astra xhigh: 2 majors, both fixed; Opus high: SHIP, 9 minors, 7 fixed, #8/#9 accepted). Closed: dlg-y5c, dlg-3em, dlg-cjz.4, dlg-j0q, dlg-o7i, dlg-278.2. Open pending macOS/Linux CI on the next gated GitHub push: dlg-278, dlg-278.1. Lane worktrees retired. Pushed to Forgejo.

## Rulings
- Ruling: closed dlg-278.2 on local 20/20 reproduction under COLUMNS=80 PYTHON_CPU_COUNT=4 without a GitHub rerun — the reproducer matches the CI symptom exactly; cost if wrong: one more CI red on the next push.
- Ruling: accepted Opus #8 (0o700 window between rename and seal) as forced by macOS rename semantics, same-uid only; cost if wrong: a same-user process could write into a snapshot during a sub-millisecond window.
- Ruling: Opus #2 fixed as a typed error only, not recovery of unsearchable (0o400) stale dirs — Delegate never writes that mode; cost if wrong: an externally-mangled pin store needs a manual chmod.
- Ruling: extended dlg-278.1's write boundary to workflow_attempts.py via followup rather than a new bead — same defect, same lane context; cost if wrong: a slightly larger diff to review in one merge.

## Post-run (2026-09-08, Trey: push to GitHub, reinstall runtime, update skills)
- GitHub: filtered `main` 5e3510d (= source 4b03b67) pushed once, authorized; mirrored to Forgejo `publish/main`. Verified before push: fast-forward over 8043bec, 0 leaked audit paths, tree identical to the source.
- Runtime: staged `bin`+`src` from 4b03b67 into `~/.delegate/releases/4b03b67e375ba6ae-6d2bad0f500840cd`, switched the `bin/delegate.py` and `src` links atomically, `delegate promote --actor claude --source 4b03b67…`; doctor reports `executionMode: installed`, `promotionMatchesRuntime: true`; config keeps `mail.enabled: true`, `tracking.skillReviewPreamble.enabled: false`. Live smoke: Luna `call` (OK) and a tracked Luna `work` run whose manifest pins `trackedStreamMaxBytes: 16777216`. Discovery re-probed through the installed command: work profile codex/claude/cursor/grok/omp/pi (pi `estate-pi` fingerprints 0.85.1 — dlg-3em fix live); personal profile codex/claude/grok/omp/pi (cursor unauthenticated in the personal realm: `estate-cursor models` asks for `agent login`). One workflow supervisor (fin-model wf_8f73ab8b6bd8) remains pinned to its own runtime, untouched.
- Skills: `delegate-agent` split into a 309-line core plus `references/fleet.md` and `references/runtime.md` (writing-for-agents pass, Trey's request); reviewer ranking updated per Trey (Astra top; Sol/Opus/Cursor Grok 4.6 at xhigh close seconds). skill-library f37a4de, 06a9f5e, synced.
- CI on 5e3510d (run 34176256630): 3.12 and 3.13 green; the 177-failure macOS seal-before-rename class is gone (dlg-278.1 confirmed on macOS). Remaining: `test_mail_push` ×2 on macOS (dlg-278.3, /tmp -> /private/tmp vs resolved codex home) and `test_structured_retry_supervisor_resume_reaps_stale_workspace` KeyError executionCwd on 3.11/3.14/macOS (dlg-278.4, registry record precedes manifest). Both fixed as test-only changes; confirmation needs a second GitHub push (not yet authorized).
- Docs: README cap sentence and CHANGELOG 0.31.0 "CI and runtime hardening" subsection added for the ci-burn landings.

## Re-arm
Monitor: poll `delegate --json runs --group ci-burn` every 30s, emit on status change. Fallback ScheduleWakeup 1500s.
