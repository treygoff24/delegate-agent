# STATE — delegate-agent

Updated: 2026-09-29 (overnight friction work and the Linux reap fix installed on the Mac and the devbox)

## Where things stand

- `main` = Forgejo `origin/main`, installed on both hosts (`delegate --json doctor`: `promotionMatchesRuntime` true on the Mac and on the devbox agent user). Moving `main` makes estate-sync install it on the Mac; the devbox installs after a pull.
- `main` carries the 2026-09-28 papercuts work and the overnight clusters (epic `dlg-erz`, closed): write guard, degraded/early-stop detection, provider errors as data with known-bad lanes, unread mail at run end, pending-tool and time-left in `runs`, cross-workspace lookup, workflow `--repin`, and more. Detail: `CHANGELOG.md` [Unreleased]; report: `docs/audits/2026-09-28-papercuts/REPORT.md`; integrator notes: `docs/handoffs/2026-09-28-papercuts-fixes-for-integrators.md`.
- 2026-09-29: the devbox gate exposed Linux-only failures in the overnight worktree work. `worktree reap` refused everything on Linux because its cwd scan blocked on hidden session processes (systemd, ssh-agent, sshd). Fixed: a hidden process with our real and effective uid/gid is noted, not blocking; other ids still block. Best-effort by design (module docstring of `src/delegate_agent/worktree_procs.py`); two outside review rounds, the final saved-id tweak for set-gid ssh-agent unreviewed. Full gate PASS on the devbox.
- The `delegate-agent` and `delegate-workflows` skills were rewritten short and brought up to date with everything above (skill pool `~/.agents/skill-library`).
- Coordinator calls from the overnight rounds, easy to revisit: a work-mode broker refusal is never auto-rerun; an unreadable nested Registry refuses even with `--kill-live`; the Claude typo refusal covers only family-plus-version shapes and skips under Bedrock/Vertex/Foundry/gateway env; work prompts say nobody can approve during a Run. A real `--detach` is Trey's call and was not built.

## Open loops

- Full gate is ~20 min on one core; Trey (2026-09-29) asked for targeted tests while iterating and one full run at the end. Speed-up task: `dlg-8e3`.
- Live Mac config change awaiting Trey's "install" word: `policy.harness.codex.work.bypassApprovalsAndSandbox: false`, optionally `isolation.writeGuard.macosSeatbelt: true`. Codex under Seatbelt with its own sandbox bypassed is not live-tested.
- The write guard re-opens `~/.ai-profiles` for lanes started through an `estate-*` launcher; `~/.ssh`, the installed runtime, and `~/Code` stay protected. Known limits: `docs/security-model.md`.
- `dlg-ptm` (Claude preflight on non-object workflow schema roots) is another session's work.
- Deferred items are beads (`bd ready`). The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile` (a linux-devbox change).

## Earlier records

Before 2026-09-28: `docs/handoffs/2026-09-25-state-record.md`, `CHANGELOG.md`, and git history.
