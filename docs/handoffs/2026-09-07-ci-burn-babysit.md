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

## Failure modes seen
(none yet)

## Rulings
(none yet)

## Re-arm
Monitor: poll `delegate --json runs --group ci-burn` every 30s, emit on status change. Fallback ScheduleWakeup 1500s.
