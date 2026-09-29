# STATE — delegate-agent

Updated: 2026-09-28 (papercuts fixes shipped to `main`; see CHANGELOG [Unreleased])

## Where things stand

- `main` carries the papercuts work from the 2026-09-28 report: write guard for work lanes (default on for Linux bubblewrap, opt-in Seatbelt on macOS), turn-end detection (`degraded` runs), output cap off by default with partial output kept on failure, provider errors as data with known-bad lane refusal and one auto-resume, Codex and Claude work runs resumable by default, OMP explicit provider pinned, workflow pinned-vs-live runtime with opt-in `--repin`, `--force` never overriding a live lease, scratch reclaim and cross-registry run lookup, a docs-vs-parser conformance test, and the five small bugs.
- Each area was built on its own branch, reviewed by an outside model (at most two rounds, then the coordinator reviewed and fixed), full-gated at its branch head, and merged into `papercuts/integration`, which was full-gated again before the fast-forward.
- Moving `main` makes estate-sync install it on the Mac.
- Report and diagnoses: `docs/audits/2026-09-28-papercuts/REPORT.md` and `appendix/`.

## Open loops

- Live Mac config change awaiting Trey's "install" word: `policy.harness.codex.work.bypassApprovalsAndSandbox: false` (puts Codex's own sandbox back for work lanes), optionally `isolation.writeGuard.macosSeatbelt: true`. Codex under Seatbelt with its own sandbox bypassed is not live-tested.
- The write guard re-opens `~/.ai-profiles` for any lane started through an `estate-*` launcher, because the launcher writes account, token, and session state there on every launch. On the estate that covers most engines, so those lanes can write every profile's files; `~/.ssh`, the installed runtime, and `~/Code` stay protected. Live-checked 2026-09-28 on the Mac (Seatbelt) and the devbox agent user (bubblewrap).
- Known write-guard limits (documented in `docs/security-model.md`): a protected path that is itself a symlink is protected at its target while the link stays replaceable; an existing hard link to a protected file is the same inode; a second profile nested deeper than three levels is not found.
- The blocked-dependency model rotation Trey asked about lives in the `writing-plans` skill, not this repo.
- `dlg-ptm` (Claude preflight on non-object workflow schema roots) is another session's work.
- Deferred items stay as beads (`bd ready`): workflow resume adopting live children, one lane-environment record, strict option grammar, registry option applicability, near-duplicate cut warning, watchdog loop detection, bounded assistant text.
- The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile`; aligning them is a linux-devbox change.

## Earlier records

Everything before 2026-09-28 (describe DSL, bwrap fixture, devbox promotion, GPT-6 rows, gate counts) is in `docs/handoffs/2026-09-25-state-record.md`, plus `CHANGELOG.md` and git history.
