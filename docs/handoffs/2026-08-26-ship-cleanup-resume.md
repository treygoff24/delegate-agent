# Resume: ship-cleanup run, 2026-08-26 (paused for devbox power cycle)

Session context: full in-flight cleanup + ship of delegate-agent, orchestrated from Claude with Luna xhigh implementation lanes and Cursor Grok review lanes.

## Shipped to main (pushed to Forgejo, tip 7cdac4a)

- Branch sweep: all stale/superseded branches deleted local + origin; repo is main + `feature/followup-r2` only.
- Beads repaired: DB reinitialized from `.beads/issues.jsonl` (neither remote had Dolt data; `--discard-remote` discarded nothing). bd works again.
- Gate fix: `scripts/gate.sh` runs `uv run --extra dev` and asserts the resolved ruff equals the pyproject pin (fresh worktrees silently resolved unpinned ruff before). `workflows/runtime.py` reformatted under the pin.
- `structured-retry-resume` merged (9ab3ca8): resume-in-place structured retries, durable cleanup-descriptor ownership on every terminal path. Two Cursor review rounds + re-verify; branch deleted local + origin.
- Run-lifecycle cleanup merged (7cdac4a), dlg-7te CLOSED: completion-time worktree retirement (seeded-dirt content digests, all porcelain lines classified) + child process-group kill on every terminal path. Deletion safety confirmed by adversarial review. Fix 3 (supervisor watchdog) split into its own P1 bead.

## Mid-flight: feature/followup-r2 — ONE fix round from merge

- Branch tip fd33a76, checked out in the delegate worktree `~/Code/delegate-worktrees/e06d04efc0d7/codex-20260826T164404Z_38dbe1` (only dirt: seeded `.beads/issues.jsonl`, ignorable).
- The feature passed two review rounds + fixes, then was rebased onto 7cdac4a (gate PASS, 2419 tests). The FINAL pre-merge Cursor review found two cross-feature MAJORs:
  1. Worktree retirement is not coordinated with structured retries / resumable sessions — a schema retry (or later followup) can find its worktree already retired.
  2. Schema-retry children launched into a prior worktree carry no worktree attachment, so a later `followup()` re-enters the SOURCE tree while the native session lives in the worktree.
- The fix brief (with coordinator rulings baked in) is at `~/ship-lanes-2026-08-26/followup-fixes2.md`. The Luna lane running it was cancelled 44s in for the power cycle — relaunch it verbatim:

```sh
delegate --group ship --cwd ~/Code/delegate-worktrees/e06d04efc0d7/codex-20260826T164404Z_38dbe1 \
  codex work --model luna --reasoning-effort xhigh \
  --prompt-file ~/ship-lanes-2026-08-26/followup-fixes2.md
```

- After it lands: coordinator spot-check of both fixes → focused Cursor re-verify scoped to the two MAJORs (launch from the worktree's own workspace to dodge registry-lock contention, see dlg beads below) → merge to main → full gate → push → delete branch + retire the worktree.

## Rulings made this run (also in bead comments)

1. Resumable (`--resumable`) persistent-worktree runs are RETAINED at completion (reason `resumable_session`); reclamation belongs to the reap verb (dlg-3w5). Cost if wrong: worktree re-accumulation for resumable-heavy workloads.
2. Structured-retry supervisors hold a live attachment on the worktree until the retry loop is terminal; retirement applies to the post-loop state.
3. Attached resume runs never retire a previously retained tree (owner-record-only hook) — accepted for dlg-7te; orphan reclamation is dlg-3w5.
4. safe + `--resumable` is rejected at parse time (a captured session with no re-entry path is a lie).

## New bugs filed this run (all from dogfooding the fleet)

- dlg-uxk (P3): mail tests mkdtemp at parents[3] of the test file — 30 bogus failures in shallow checkouts.
- dlg-yjo (P2): registry finalize lock timeout (hardcoded 30s, run_registry.py:64) reports completed children as failed.
- P1 (see bd list): worktree pytest runs flock the SOURCE repo's `.delegate/.registry.lock` for the whole suite — the actual lock holder behind dlg-yjo's crashes.
- P1: supervisor watchdog (dlg-7te fix 3) — `workflow _supervise` must self-terminate.

## Environment notes

- Lane briefs backed up from /tmp to `~/ship-lanes-2026-08-26/` (14 files) — /tmp does not survive the reboot.
- Registry-contention workaround used all session: launch review/fix lanes with `--cwd` at the lane's own worktree, never the shared source checkout, until the two lock beads are fixed.
