# Issue: completed runs leak worktrees, child processes, and supervisors

**Filed:** 2026-08-26, from a post-mortem census of the devbox after an overnight multi-lane run.
**Severity:** major — one night of normal delegate usage left 79 GB of dead worktrees, two orphaned dev servers, and a zombie workflow supervisor that survived two days.

## Evidence (devbox, trey cell, morning of 2026-08-26)

- **~80 persistent worktrees, 79 GB**, under `~/Code/delegate-worktrees/7b64c29d2f41/` (one source repo), all from claude/codex/omp work lanes launched over ~36 h. Each carries its own `node_modules`. Every run had completed; nothing referenced them. The owning repo's total footprint was ~148 GB, so dead delegate worktrees were more than half the disk.
- **Orphaned child processes:** a `next dev` server plus its `esbuild --service` helper, spawned by a codex work lane inside `delegate-worktrees/.../codex-20260826T093413Z_90179d`, still running hours after the lane exited. A second identical pair from an earlier lane in another worktree. These accumulate until someone hand-kills them.
- **Zombie supervisor:** `delegate.py --cwd /tmp/tmp1imy554u workflow _supervise wf_5271e26a9241` started 2026-08-24 22:49, still alive on 2026-08-26 with ~7 h of accumulated CPU, cwd in a stale tempdir. Whatever workflow it supervised is long gone.

## Root cause

Delegate knows the run lifecycle (launch → completion payload) but does nothing at end-of-run to retire the resources the run created:

1. Persistent worktrees are never retired. `worktree remove/prune/gc` exist but are manual; `autoPrune` is opt-in config and only triggers as a side effect of someone running `worktree list` — which unattended orchestrators never do. `worktree gc` explicitly "never deletes worktree directories".
2. Child agents can spawn daemons (dev servers, watchers, service processes) that escape the run: delegate does not run the child in its own process group/session and does not kill the group at run end, so grandchildren survive and reparent to init.
3. `workflow _supervise` has no self-termination guard: if the workflow it supervises dies or completes abnormally, the supervisor idles forever.

## Requested fixes

### 1. End-of-run worktree retirement (the big one)

On work-lane completion, when the worktree is safe to retire (`fullyIntegrated: true`, or clean with its `delegate/*` branch preserved in the repo — the branch is the durable artifact), remove the worktree directory. Options, in preference order:

- a `retireWorktreeOnCompletion` config (default **on** for clean/integrated worktrees, always skipping dirty ones with a warning in the completion payload), or
- at minimum: make `autoPrune` run at run-completion time, not only on `worktree list`, and default it on with a conservative `mergedOlderThanDays`.

Dirty or unmerged worktrees must never be deleted silently — report them in the completion payload as `worktreeRetained: <reason>` so an orchestrator can decide.

### 2. Kill the child's process group at run end

Launch child harnesses in their own session/process group (`setsid` / `start_new_session=True`), record the pgid in run metadata, and on run completion (success, failure, timeout, or supervisor exit) send SIGTERM to the group, escalating to SIGKILL after a grace period. A dev server the child started must not outlive the run. Platform note: needed on both macOS and Linux.

### 3. Supervisor watchdog

`workflow _supervise` should exit when: the supervised workflow reaches a terminal state, its state file/registry entry disappears, or a heartbeat from the workflow goes stale past a bounded timeout. A supervisor must not be able to idle for days.

## Acceptance criteria

- A `--isolation worktree` work run that completes with a merged/clean worktree leaves **no directory** under `delegate-worktrees/` (branch still present in the repo); a dirty worktree survives and is named in the completion payload.
- A work run whose child spawns a background `next dev` leaves **no process** from that run's process group alive 30 s after completion.
- A killed/crashed workflow leaves no `_supervise` process alive past the watchdog timeout.
- Existing manual commands (`worktree list/show/remove/prune/gc`) unchanged in behavior apart from new lifecycle hooks.

## Non-goals

- Reaping worktrees/processes left by *other* tools (estate janitor covers that as a backstop).
- Changing safe-mode (`/tmp/delegate-safe-*`) cleanup — already age-gated by the estate janitor.
