# Resume: backlog burndown, 2026-08-27 — DISCHARGED, plus a scope cut

Session context: full backlog burndown of delegate-agent (Waves 1–3 + W2-P), orchestrated from Claude/Opus with Luna xhigh implementation lanes, Cursor Grok review lanes, Terra fix lanes, and Opus native review gates. Paused mid-Wave-3 for the subscription reset; resumed and closed the same day. **This handoff is discharged — nothing here is waiting to be picked up.**

## Shipped to main and pushed to Forgejo (tip d274dca)

- **Wave 1** (supervisor/registry/watchdog contract fixes) — merged, gate PASS. ENGINE.W1 discharged by hq.
- **Wave 2** (launch pinning dlg-44w.5/.10, discovery honesty dlg-zv4/x6y, Devin read-only boundary dlg-smc) — merged `f6cf3aa`, gate PASS 2481. Launch-pinning is live: immutable pin.json plus a content-addressed runtime/persona snapshot outside worktrees.dataHome, with pinless resume G-compat tested.
- **Wave 3** (W3-O structured-output tolerance and observability dlg-44w.6/.7a/.7b, W3-X lane-health advisory dlg-44w.3, W3-A named soft-park admission dlg-44w.8/.9) — merged `0f97bd8`. The Cursor review's nine findings were fixed by the Terra lane and closed as dlg-44w.12.
- **Three fixes found and shipped during closeout** — see the next section.

Wave 3 was verified at coordinator level rather than on the lane's self-report: all five code-bearing fixes spot-checked on the diff, independent gate on the integration branch (PASS 2503, parity ok), merged, gate on main. **The Opus review gate was skipped on Trey's explicit instruction** ("skip the opus review pass, drive this to completion yourself").

## Closeout fixes (this session, after Wave 3)

- `e11ce27` — **shared ledgers no longer block worktree retirement.** `.beads/` and `.papercuts.jsonl` are written by every agent as a matter of course, so a lane that filed one papercut stranded its worktree forever. That was 82 of 188 pooled trees, holding no work at all. Retirement dirt now discounts `worktrees.retirementIgnoreGlobs`.
- `0cf29a5` — **prune must not delete a live lane** (dlg-44w.4). `live_attachments_for_path` only sees resume-style attachments, so a worktree's own running owner was invisible. Selection now refuses non-terminal owning runs and live process groups, with distinct skip reasons; `--force` overrides; auto-prune inherits the guard.
- `c73edf5` — **dry run can no longer hang forever** (dlg-8oc). Root-caused from a live 38-minute hang; evidence in `docs/evidence/dlg-8oc-dryrun-hang/`. The script now receives a `dry_run` global, dry-run item threads are daemons, and `workflows.dryRunTimeoutSeconds` (default 300) fails with `dry_run_timeout` instead of hanging.

Final state: gate PASS 2510 / 15 skipped, ruff 0.15.15, parity ok 2525.

## Worktree pool sweep

188 pooled worktrees audited. Only 20 held commits existing nowhere else; 82 were dirty *solely* from ledger bookkeeping. **76 were deleted** (the strict filter spared six that turned out to hold unique commits, and skipped one live lane). Every branch was preserved, and the ledger diffs were harvested and checked against the source ledgers first — all 7 unique records were already upstream, so nothing was at risk. Pool: 188 → ~106. Sweep evidence: `docs/evidence/worktree-sweep-2026-08-27/`.

## Scope cut (Trey, 2026-08-27): "we are massively overbuilding — cut shit and ship shit"

- **W2-P worktree deletion safety is CUT, not parked.** The r2 design (816 lines, two Opus review rounds) is preserved on branch `fix/w2-worktree-safety` at commit `d758391` — that is the drawer; the worktree holding it is disposable. The design solved "make deletion safe enough to be aggressive," but deletion was already conservative to a fault: the real defect was that the dirty check counted our own instrumentation as user work, which `e11ce27` fixes in a few lines. dlg-3w5 (explicit reap verb) belongs to this cut.
- **Burst-capacity policy (dlg-507) is CUT.** Trey selected the configurable soft cap when asked, and that remains the design of record if it is ever built — but the bead cites no actual incident, and the Workflow layer already caps itself, so nothing is being built now.
- **CP1 heal of `wf_b43032f7fee5` is WITHDRAWN for good.** hq closed hq-z41 following Trey's ceremony-teardown ruling; the run stays untouched under its lock and nothing depends on it. The CP1 recovery being built and fixture-proven stands on its own.
- **Auto-prune stays ON.** An earlier recommendation to disable it was wrong and Trey pushed back correctly: the defect was prune's selection predicate, not the idea of automatic cleanup. `0cf29a5` fixes the predicate.

## Still open

1. **The installed runtime is stale and none of this is live.** `~/.delegate/src/delegate_agent` is a hand-copied tree dated 2026-08-26 that matches no commit exactly (closest `fdc3aae`, 29 files differing). Everything from late Wave 1 onward is missing. There is no deploy script in this repo — `workflow_pinning.promote()` only *stamps* a promotion, and its own docstring says the mechanics belong to hq tooling. **Trey is routing the deploy to Mac Fable.** Note the ordering wrinkle: launch pinning is what protects a running supervisor from an upgrade underneath it, and pinning ships *in* this batch, so this is the last unprotected deploy. At closeout there were zero active supervisors.
2. **Devin creds** — the read-only boundary shipped fail-fast with honest docs; the live write/exec proof is still parked. Trey said he would log into Devin on the devbox later.
3. **Two P1 beads became ready** when this session's closures unblocked them, and neither has been looked at: `dlg-f8n` (workflow `_supervise` busy-loops at 99% CPU when its workload vanishes) and `dlg-h19` (workflow test harness leaks supervisor process groups past teardown).
4. **~50 pooled worktrees have no run record at all**, so they retain as `record_missing` and nothing will ever clean them. Filed as `dlg-dzr`. This is the one real remaining hole in the retirement story, and it is small.
5. **13 stale burndown worktrees** under `~/Code/burndown-worktrees/` (waves 1–3, all merged) plus their `fix/*` and `integration/*` branches. Left in place deliberately — removing them was not authorized this session.

## Environment notes

- Lane briefs at `~/ship-lanes-2026-08-26/` — `/tmp` does not survive reboot; these are the durable copies.
- Cross-cell porch is live: `#front-porch` (union-synced Mac/trey-cell/fc-cell via the ADR-016 bridge) and `#v2-hardening` (engineering coordination with hq). hq (`hq-7f`) drives the parallel ENGINE monolith-split project, NOT this burndown — don't conflate them.
