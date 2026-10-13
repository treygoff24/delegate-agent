# STATE — delegate-agent

The live sitrep for the `delegate` dev repo: what is pending, the standing rulings, and how the moving parts work today.
History is preserved word for word in `docs/handoffs/state-history-through-2026-10-01.md` (older: `docs/handoffs/2026-09-25-state-record.md`, `CHANGELOG.md`, git history).
Change the section a fact belongs to rather than appending a story; a finished change becomes one line under Recent, and when Recent passes about ten lines the oldest moves to the history file. Rulings carry who ruled and when.

## Pending

- Waiting on Trey: a live Mac config change awaiting his "install" word: `policy.harness.codex.work.bypassApprovalsAndSandbox: false`, optionally `isolation.writeGuard.macosSeatbelt: true`. Codex under Seatbelt with its own sandbox bypassed is not live-tested.
- Not built, Trey's call: a real `--detach`.
- Follow-ups on task `dlg-2o9`: OMP live probes can't fire on omp 18.4.3, `--help` imports runner, the OMP flood test needs its own timeout, auto-resume `binding_not_active` ordering.
- Test-speed measurement limits and native-Mac verification: `docs/audits/2026-09-29-test-speed/REPORT.md`.
- The estate launcher and `bin/delegate-profile-shim` disagree on `DELEGATE_CONFIG` versus `--auth-profile` (a linux-devbox change).
- Other deferred items are beads (`bd ready`).

## Standing rulings

- **Default work write guard** (Trey, 2026-09-29): it protects credentials/profile state and the installed Delegate runtime only. `codeRoot` defaults to `null`; sibling checkouts stay writable unless explicitly protected, and the protect-list prompt no longer restricts work to the initial checkout. Incident and resolution: `docs/issues/2026-09-30-fix-verifier-review-checkout-read-only.md`.
- **Test-value audit** shipped (Trey: "ship it", 2026-09-29): 4,720 to 4,522 test definitions, about 150 weak assertions tightened and red-proved.
- **Coordinator calls from the overnight rounds**, easy to revisit: a work-mode broker refusal is never auto-rerun; an unreadable nested Registry refuses even with `--kill-live`; the Claude typo refusal covers only family-plus-version shapes and skips under Bedrock/Vertex/Foundry/gateway env; work prompts say nobody can approve during a Run.

## How it works now

- **Gate.** `scripts/gate.sh --fast` (about 9s) and `scripts/gate.sh` (full regression) share explicit workers, low priority, and a cross-worktree lock; Linux uses a two-core quota. Last full main gate: 4,736 tests, 25 skips, 3,936 subtests, all static checks (2026-10-12, release 0.33.0). Mutation proofs: set `PYTHONDONTWRITEBYTECODE=1`, because a same-size edit restored within the same second leaves a stale `.pyc` Python trusts.
- **Write guard.** It re-opens `~/.ai-profiles` for lanes started through an `estate-*` launcher; `~/.ssh` and the installed runtime stay protected. Protecting `~/Code` is opt-in. Existing pinned workflows need the new runtime to pick up the new default. Known limits: `docs/security-model.md`.
- **Installed runtime.** 0.33.0 is installed on the Mac (`~/.delegate`) and the devbox cell (`/home/trey/.delegate`), each with a pre-update backup under its `backups/`; the devbox host has no install. The `muse` engine needs the `muse` binary, present on the Mac only.
- **Worktree reap on Linux.** A hidden process with our real and effective uid/gid is noted, not blocking; other ids still block. Best-effort by design (module docstring of `src/delegate_agent/worktree_procs.py`); the final saved-id tweak for set-gid ssh-agent was not outside-reviewed.
- **Papercuts and overnight clusters** (epic `dlg-erz`, closed) are in `main`: `CHANGELOG.md` [Unreleased]; `docs/audits/2026-09-28-papercuts/REPORT.md`; `docs/handoffs/2026-09-28-papercuts-fixes-for-integrators.md`.
- **Skills.** `delegate-agent` and `delegate-workflows` are short and current (pool `~/.agents/skill-library`). Routing (Trey, 2026-10-12): Muse Wonder Full stands equal to Opus max/Sol max in heavy rotation; Muse Spark stands equal to Sonnet as a default worker; `muse` engine lanes are Mac-only until `muse` exists on the devbox.

## Recent

- 2026-10-12 Muse (Meta) is the eleventh engine (`delegate muse safe|work|call`); released as 0.33.0 to GitHub and PyPI, installed on Mac and devbox cell. Plan: `docs/plans/2026-10-12-muse-engine.md` (gitignored). Open follow-ups `dlg-n32r` (MCP safe probe), `dlg-36qv` (auth/lost-session fixtures), `dlg-ehgd` (image input).
- 2026-09-29 Default work write guard narrowed (Trey); 130 focused tests, full gate passed.
- 2026-09-29 Test-speed work complete; full-suite median 357.755s to 235.908s at the same budget.
- 2026-09-29 Test-value audit landed; two workflow bugs fixed (`failureReason` read, `retries=-1`), dead seams removed.
- 2026-09-29 Linux `worktree reap` fixed after the devbox gate exposed it.
