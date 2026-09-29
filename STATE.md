# STATE — delegate-agent

Updated: 2026-09-29 (overnight friction work merged to `main`; see CHANGELOG [Unreleased])

## Where things stand

- `main` carries the papercuts work from the 2026-09-28 report: write guard for work lanes (default on for Linux bubblewrap, opt-in Seatbelt on macOS), turn-end detection (`degraded` runs), output cap off by default with partial output kept on failure, provider errors as data with known-bad lane refusal and one auto-resume, Codex and Claude work runs resumable by default, OMP explicit provider pinned, workflow pinned-vs-live runtime with opt-in `--repin`, `--force` never overriding a live lease, scratch reclaim and cross-registry run lookup, a docs-vs-parser conformance test, and the five small bugs.
- Each area was built on its own branch, reviewed by an outside model (at most two rounds, then the coordinator reviewed and fixed), full-gated at its branch head, and merged into `papercuts/integration`, which was full-gated again before the fast-forward.
- Moving `main` makes estate-sync install it on the Mac.
- Report and diagnoses: `docs/audits/2026-09-28-papercuts/REPORT.md` and `appendix/`.
- 2026-09-28 ledger sweep (`docs/audits/2026-09-28-papercuts/ledger-sweep.json`): 305 open delegate cuts classified as 136 fixed (131 resolved in their ledgers with the fixing commit named; 5 in an archived ledger left alone), 54 not fixed, 92 not delegate, 23 unsure. The sweep commit's body said 106 not delegate and 30 unsure; the file is right.
- Overnight 2026-09-28/29 (epic `dlg-erz`): the 54 not-fixed cuts became eight clusters, each built by a Sonnet lane, reviewed by Sol high (at most two rounds, then the coordinator reviewed and fixed), and merged through `overnight/integration`: early-stop detection (`ended_announcing_next_step`, `ended_awaiting_input`), Codex free-form objects and wrapped arrays, unread coordinator mail at run end, one fresh rerun for transient safe-mode failures plus the effort-aware stall window, Claude model-typo preflight and `<engine>.enabled`, six CLI surface traps, workflow status why/next (pause, soft park, timeouts), and nested-worktree removal safety.
- Later the same night: live runs show the tool call they are waiting on (`pendingTool`, with `current` reading `waiting on tool <name> for 13m`) and stop showing a provider error the harness has already moved past; a followup that dies instantly with no output points at `delegate resume`; setup-output tails mask recorded secrets to a fixpoint around generic redaction; every harness caps its pending-tool map; the Cursor family warning names what was typed. The live post contract test caught post 0.9.0 dropping `--anyway` from its help, so channel `--notify` sends without it and retries with it only on `crossed_send` (post's own skill text still mentions `--anyway`).
- Then: `runs`/`ps` show how long a running Run has left (`deadlineAt` recorded when the runner's timeout clock starts, `remainingSeconds`, `12m left` in text); the Claude typo check honors provider variables from `--env`/`--env-file` (read once, an empty value clears the launching shell's); the Cursor auth hint's call-mode profile pass-through is tested. A workflow-watchdog test that flaked about one run in twenty turned out to be a real race: parallel Runs launched together could fail with `unsafe_scratch_directory` when both created the shared scratch root; fixed, with a deterministic test.
- Coordinator calls in the third rounds, easy to revisit: a work-mode broker refusal is never auto-rerun (post-exit "no output" cannot prove no side effects); an unreadable nested Registry refuses even with `--kill-live`; the Claude typo refusal is limited to family-plus-version shapes and skipped under Bedrock/Vertex/Foundry/gateway env; the work-mode prompt carries a "nobody can approve during a Run" clause.

## Open loops

- Live Mac config change awaiting Trey's "install" word: `policy.harness.codex.work.bypassApprovalsAndSandbox: false` (puts Codex's own sandbox back for work lanes), optionally `isolation.writeGuard.macosSeatbelt: true`. Codex under Seatbelt with its own sandbox bypassed is not live-tested.
- The write guard re-opens `~/.ai-profiles` for any lane started through an `estate-*` launcher, because the launcher writes account, token, and session state there on every launch. On the estate that covers most engines, so those lanes can write every profile's files; `~/.ssh`, the installed runtime, and `~/Code` stay protected. Live-checked 2026-09-28 on the Mac (Seatbelt) and the devbox agent user (bubblewrap).
- Known write-guard limits (documented in `docs/security-model.md`): a protected path that is itself a symlink is protected at its target while the link stays replaceable; an existing hard link to a protected file is the same inode; a second profile nested deeper than three levels is not found.
- The blocked-dependency model rotation Trey asked about lives in the `writing-plans` skill, not this repo.
- `dlg-ptm` (Claude preflight on non-object workflow schema roots) is another session's work.
- Overnight deferrals, as beads: a running agent's deadline in `delegate runs`; the Cursor auth hint's `execute_call` profile pass-through is untested; the Claude typo check does not see `--env` provider variables (they resolve after it).
- Deferred items stay as beads (`bd ready`): workflow resume adopting live children, one lane-environment record, strict option grammar, registry option applicability, near-duplicate cut warning, watchdog loop detection, bounded assistant text.
- The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile`; aligning them is a linux-devbox change.

## Earlier records

Everything before 2026-09-28 (describe DSL, bwrap fixture, devbox promotion, GPT-6 rows, gate counts) is in `docs/handoffs/2026-09-25-state-record.md`, plus `CHANGELOG.md` and git history.
