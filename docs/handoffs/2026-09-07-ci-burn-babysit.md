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

## Gate
- Quiet full gate on main at 09658a7 (code = 2064fab): 3294 passed, 15 skipped, GATE PASS ruff 0.15.15 (log runs/gate-main.log). Re-gate after the two review fixes merge.

## Rulings
- Ruling: extended dlg-278.1's write boundary to workflow_attempts.py via followup rather than a new bead — same defect, same lane context; cost if wrong: a slightly larger diff to review in one merge.

## Re-arm
Monitor: poll `delegate --json runs --group ci-burn` every 30s, emit on status change. Fallback ScheduleWakeup 1500s.
