# STATE — delegate-agent

Updated: 2026-09-29 (test-speed improvements validated and merged to main)

## Where things stand

- Source `main` includes the test-speed improvements. This session did not promote an installed CLI runtime.
- `main` carries the 2026-09-28 papercuts work and the overnight clusters (epic `dlg-erz`, closed): write guard, degraded/early-stop detection, provider errors as data with known-bad lanes, unread mail at run end, pending-tool and time-left in `runs`, cross-workspace lookup, workflow `--repin`, and more. Detail: `CHANGELOG.md` [Unreleased]; report: `docs/audits/2026-09-28-papercuts/REPORT.md`; integrator notes: `docs/handoffs/2026-09-28-papercuts-fixes-for-integrators.md`.
- 2026-09-29: the devbox gate exposed Linux-only failures in the overnight worktree work. `worktree reap` refused everything on Linux because its cwd scan blocked on hidden session processes (systemd, ssh-agent, sshd). Fixed: a hidden process with our real and effective uid/gid is noted, not blocking; other ids still block. Best-effort by design (module docstring of `src/delegate_agent/worktree_procs.py`); two outside review rounds, the final saved-id tweak for set-gid ssh-agent unreviewed. Full gate PASS on the devbox.
- 2026-09-29: test-value audit landed (Trey: "ship it"). The suite went from 4,720 to 4,522 test definitions, and about 150 weak assertions were tightened and red-proved. Every removal was checked by a second model family and then by a preservation review. Two workflow bugs were fixed: the failure summary read `failure_reason` instead of `failureReason`, and `retries=-1` launched nothing. Dead seams were removed: the `load_config(cli_overrides=)` layer and six model-list parsers. Full gate: only the pre-existing OMP image-scan failure. Follow-ups are on task `dlg-2o9`: OMP live probes can't fire on omp 18.4.3, `--help` imports runner, the OMP flood test needs its own timeout, and auto-resume `binding_not_active` ordering.
- The `delegate-agent` and `delegate-workflows` skills were rewritten short and brought up to date with everything above (skill pool `~/.agents/skill-library`).
- Coordinator calls from the overnight rounds, easy to revisit: a work-mode broker refusal is never auto-rerun; an unreadable nested Registry refuses even with `--kill-live`; the Claude typo refusal covers only family-plus-version shapes and skips under Bedrock/Vertex/Foundry/gateway env; work prompts say nobody can approve during a Run. A real `--detach` is Trey's call and was not built.

- Test-speed work is complete: `scripts/gate.sh --fast` takes 8.69s; `scripts/gate.sh` runs the full regression. Both share explicit workers, low priority, and a cross-worktree lock; Linux uses a two-core quota. Five interleaved full-suite pairs improved the median from 357.755s to 235.908s (34.06%) at the same four-worker/two-core budget. The final main gate passed 4,691 tests, 17 skips, 3,861 subtests, and all static checks in 234.83s. The image-scan import error and two fixture races are repaired. Measurement details, limits, and native-Mac verification still outstanding: `docs/audits/2026-09-29-test-speed/REPORT.md`.

## Open loops

- Mutation proofs: set `PYTHONDONTWRITEBYTECODE=1`. A same-size edit restored within the same second leaves a stale `.pyc` that Python trusts; this bit the audit's first full gate.
- Live Mac config change awaiting Trey's "install" word: `policy.harness.codex.work.bypassApprovalsAndSandbox: false`, optionally `isolation.writeGuard.macosSeatbelt: true`. Codex under Seatbelt with its own sandbox bypassed is not live-tested.
- The write guard re-opens `~/.ai-profiles` for lanes started through an `estate-*` launcher; `~/.ssh`, the installed runtime, and `~/Code` stay protected. Known limits: `docs/security-model.md`.
- Deferred items are beads (`bd ready`). The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile` (a linux-devbox change).

## Earlier records

Before 2026-09-28: `docs/handoffs/2026-09-25-state-record.md`, `CHANGELOG.md`, and git history.
