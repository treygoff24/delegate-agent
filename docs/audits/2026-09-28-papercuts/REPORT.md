# aDelegate papercuts report

2026-09-28. Diagnosis only; nothing was fixed. Covers the `delegate` CLI at 0.31.0 (Mac and devbox agent cell run the same version).

## Bottom line

The papercut ledger overstates the current pain and understates the danger.

- **Overstates:** roughly half of the on-topic cuts no longer reproduce. They were fixed in code, often after the cut was filed, and the ledger never learned. About 89 of the 752 cuts came from one benchmark session whose own shim blocked delegate; that was never a delegate defect.
- **Understates:** the worst problems are not the loudest cuts. A work lane can write anywhere its user can write, on both machines. That is how one lane deleted most of the devbox agent's home directory on 2026-09-24. Nothing in delegate stops the next one.

The recurring pain traces to about ten underlying causes, and most of them are "two copies of one truth with nothing checking they agree" or "we learn about a failure only after it has cost a lane." The four changes I would make first:

1. **A write boundary for work lanes** (HOME read-only except declared paths). This is the only fix for the blocker-severity incident.
2. **A structured provider error plus a memory of dead lanes.** One change removes most of the "generic failure," "no preflight," and "fan-out of ten cells all failed the same way" cuts.
3. **Stop treating "the child's turn ended" as "the Run finished."** Sixteen cuts in the last week are lanes that ended their turn waiting on a background job and reported success with the work half done.
4. **Stop killing a finished lane at the output cap, and keep partial output on every failure path.**

There are also five small bugs that are live right now with one-line fixes (section 4), and a ledger cleanup (section 5).

## 1. What was collected

- Ledgers scanned: 99 on the Mac, 53 on the devbox agent cell (`ssh devagent`, user trey-agent). The Mac's `--all` view dedupes by cut id.
- Filter: any cut tagged delegate or whose text names delegate. **752 cuts** (521 from the Mac, 231 more that exist only on the devbox). 363 are open, 335 resolved, 54 archived.
- Six Sonnet agents clustered them; 67 were off-topic (another tool, or the English word). That left **685 on-topic cuts** in about 90 first-pass themes, which I grouped into ten families. Ten agents (Opus on workflow supervision and lane isolation, Sonnet on the rest) then read the code, checked whether each cause is still live at 0.31.0, and wrote a diagnosis per family. Those are the appendices.
- I re-checked five of the agents' concrete claims myself (section 4 and the HOME finding). All held.

Caveats: the agents read code and ran read-only commands; they were not allowed to launch lanes, so mechanisms marked "inferred" in the appendices are unproven. Cuts are agent-written and sometimes misattribute cause. Several fixes live in other repos (below).

## 2. The underlying causes

Ordered by what I would act on first. "Live" means still reproducible in the 0.31.0 code, per the agents' reading plus my spot-checks.

### A. Work lanes have no write boundary on the host  (blocker; live on both machines)

Delegate isolates a lane's *workspace*, not the *machine*. In work mode HOME is real and writable, every sibling checkout is writable, and the environment is copied from the caller (I read `child_environment` in `profiles.py`: it copies all of `os.environ`). The estate config sets `policy.profile: "external-sandbox"`, which turns off the engines' own sandboxes on the assumption that an outside sandbox exists. None does. The only mode that hides HOME (bwrap safe mode) is opt-in, Linux-only, safe-mode-only, and not enabled on either machine. Each incident got a narrow patch (an `ask` stub, outside-cwd detection) rather than a boundary.

Related, same shape: "do not commit" instructions are prompt-only unless the lane uses a worktree, so Devin lanes committed four times despite being told not to. Delegate also cannot stop a child running `bd` writes.

- **Recommended fix:** a work write guard. Launch work lanes with HOME read-only, then re-open the execution root, the git common directory, run scratch, the engine's own home, and a declared list of writable home paths. On Linux, reuse the existing bwrap plan code; on the Mac, a Seatbelt write profile. A lane running `rm -rf ~` or writing into a sibling checkout then fails loudly instead of succeeding. Ship a fake-engine test that runs `rm -rf "$HOME/canary"` and asserts the canary survives (it does not today).
- **Piggyback:** with the guard in place, `--forbid-commit` can become real by pointing git hooks at a refusing directory (works in every isolation mode), and the git dir can go read-only for those lanes.
- **Alternative and why not:** a scratch HOME for the child is simpler on paper but breaks engine auth, git identity, toolchains, and nested delegate, and HOME is part of the workflow pin identity. A brief clause ("use a temp HOME in tests") is the band-aid the cuts propose; it is prompt-level and fails the same way the "do not commit" instruction did.
- **Not covered:** signals to other same-user processes; writes outside HOME such as `/opt/homebrew`.
- Detail: appendix 05.

### B. Failures are identified by regex, not recorded as data, and forgotten  (live)

A lane that dies before producing any output (Cursor "Authentication required", OMP "No API key found", Kimi 402, Codex websocket close, broker 403) falls through as a generic `exit_nonzero` with the message "Child command failed." The agent ran the classifier on the literal strings from the cuts: none classify. Only the Codex usage-limit case is remembered afterward, so the tenth launch pays what the first did, and workflows fan out ten cells that all fail for the same reason. Separately, "which lanes work in this realm?" has no answer: `binaryMissing` only checks PATH, so a shim that cannot run counts as present.

- **Recommended fix (one change, four consumers):** put a bounded, redacted `providerError` (status, provider code, message) on the stream accumulator for every engine. Classify by status first, regexes only as a fallback on the terminal error. Add an engine-keyed signature table (persistent vs transient, with a hint). Generalize the existing Codex-only failover memory into a per-lane "known bad until" marker so the next launch refuses fast and names the fix, with `--force-launch` to override. Let `delegate capabilities refresh` record per-lane auth health using probes that already exist (`estate-cursor status`, `estate-omp usage`). In workflows, close a stage's gate when its first several results share one persistent signature.
- **Constraint:** a deferred ruling says no probe on every launch. This design respects it: the marker is learned from a failure, and the probe is opt-in.
- **Also here (small):** the Cursor served-model check rebuilds labels from strings and rejects "Grok 4.7 256K Extra High" because a display-name field is declared but never populated; OMP's own fallback chains cause silent cross-provider swaps and delegate defaults to warn-only for them.
- Sequencing: this touches the same file as the blocked profile-waterfall work (dlg-87d).
- Detail: appendices 01, 02.

### C. The end of a child's turn is treated as the end of the Run  (live; 16 cuts in 09-23 to 09-26)

Headless Claude and OMP end when the model stops. Background shell tasks and monitors die with the session. Delegate marks the Run succeeded, the lane says "Monitor armed; waiting for both suites to finish," and nobody is waiting. The prompt tells children to write a completion report but not that ending the turn ends the Run. By the repo's own heuristic that final message is non-substantive, but the heuristic only affects safe mode and never fails a Run.

- **Recommended fix:** prevent and detect. Prevent: set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` for headless Claude children (the flag string exists in the installed binary, but its behavior was not tested, so confirm first), and add a two-sentence "ending your turn ends the Run; run long jobs in the foreground" clause to the tracked-run prompt for every engine. Detect: when a work or safe Run's final message is non-substantive and a background tool use was left unfinished, flag it (a warning first, then a failure kind).
- **Decision needed:** delegate already ruled that a quiet work Run that changed files stays `succeeded` so the work can be adopted. That is exactly the shape of the half-finished lanes. Middle path: keep `succeeded` but set a `degraded` flag that `wait` and workflows can gate on.
- Detail: appendix 01.

### D. The output cap kills a finished lane, and partial output is thrown away  (live)

One byte limit does two jobs: it guards disk and it detects runaways. When exceeded, delegate kills the child, and because the parser reads the same retained bytes, a report or terminal event after the cap is never seen. The run ends `output_limit_exceeded` even if the work was done and valid. Only one record shape from one engine (OMP thinking deltas) is compacted. Separately, only cancelled Runs recover partial assistant text; failed, timed-out, and capped Runs lose it from the report, and OMP clears its text at every turn start. A call-mode timeout also discards buffered output. Token usage for OMP, pi, and opencode is never read from the stream at all.

- **Recommended fix:** past the cap, stop writing to the log, keep feeding the parser, and let the child finish with a truncation warning; keep a large hard ceiling as the kill. Quote the last substantive assistant text in the failed-Run report. Keep text across turns for OMP. Salvage buffered output on call timeout. Ingest OMP usage.
- Detail: appendices 01, 07.

### E. The stall watchdog cannot see loops, and cannot be seen  (live)

The watchdog remembers only the previous tool call, so any loop of two or more distinct calls looks like progress (an agent simulated 60 cycles of fail/no-op/no-op/format and it never tripped). Any in-flight tool counts as alive forever. There is one flat 8-minute idle threshold regardless of model or effort, and workflow `agent()` cannot override it. None of the watchdog's knowledge shows in the Snapshot while a run is live, and `lastActivityAt` advances on any output line, so a looping lane looks healthy. Most of the older watchdog cuts are already fixed by the September watchdog work.

- **Recommended fix:** expose a `watchdog` block in the running Snapshot (last progress, tools in flight, idle seconds, loop suspected). Keep a windowed memory of tool signatures plus a working-tree fingerprint, so a fix-and-retest loop that changes files is not flagged but a no-op cycle is. Bound the in-flight exemption by age, checking process-group activity when exceeded. Add a per-step stall override to workflow `agent()`.
- **Decision needed:** loop detection kills or only warns? Warning ships safely with the visibility change.
- Detail: appendix 07.

### F. Workflows: fixes never reach a running workflow, and every supervisor death costs every live lane  (live)

The watchdog and replay bugs that generated dozens of cuts are fixed (the heartbeat watchdog was deleted on 09-01; stable step keys and gate parking landed on 09-24), and the agent judged that the resume/state model does not need another redesign. Three targeted problems remain:

1. **Pins freeze code for a workflow's life.** Resume silently reuses the old pin, so a fix is invisible until a new workflow starts. Pins containing the old watchdog are still on disk on both machines; several cuts dated after the fix were this. Compiled plans (writing-plans) have the same property.
2. **The supervisor always detaches**, so systemd reaps it. There is only an after-the-fact warning and no foreground mode.
3. **Resume cancels every in-flight child, live ones too**, so each unit mistake burns all running lanes. The docs contradict themselves on this (one line says resume adopts running children, another says it cancels them).

- **Recommended fix:** resume prints the pinned runtime's date and digest when it differs from live, with an opt-in `--repin`; add `workflow run --foreground` and refuse to detach under a reaping KillMode; make resume cancel only dead or unlaunched children and adopt live ones (worth one live test first to confirm an orphaned child records its result). Small: relay SIGINT like SIGTERM, add key and label to follow-up timeout rows, add a `workflow check` warning for unkeyed steps in keyed scripts, fix the docs contradiction.
- Related open work: the systemd/`--unit` issue, the paused-run status message, the journal row labels.
- Detail: appendix 03; contracts side in 04.

### G. Two hand-written copies of one truth, nothing checking they agree  (the recurrence engine)

This is the pattern behind most of the "fixed, then reported again" cuts, in five places:

- **Lane prompt vs result validator** (writing-plans compiler): every rule was found by paying for a full failing lane. Roughly 40 of about 60 workflow-contract cuts no longer reproduce, mostly fixed in the compiler in the last few weeks; the class is still open.
- **Help / docs / the delegate skill vs the parser**: the skill said `models --live` needed no engine, help tiers are named against their size (`--summary` is larger than the default), etc.
- **Option applicability** lives in prose plus late validators, not in the option registry, so `--include-dirty` and `--forbid-commit` look valid under `safe` and then fail.
- **Guess-hint tables** grown one cut at a time (`ps`, `show`, `--body`...).
- **Adapters tested against their own fakes**: mail-push injects a Codex flag the installed Codex rejects; the Post notifier passes a flag the installed Post rejects. Both pass their tests because the test double accepts anything.

- **Recommended fix:** conformance tests, not more patches. (1) Extract every `delegate ...` line from `docs/cli-reference.md` (and the skill when present) and require `parse_cli` to accept it or the line to be on an explicit invalid-example list. (2) Give `OptionSpec` an `applies_to` and generate both help and rejection text from it. (3) One synonym registry consumed by all unknown-token errors, with a generic "see `help <command>`" fallback. (4) Contract tests against the real `codex` and `post` CLIs, skipped when absent. (5) In the compiler, a conformance test that builds a result from each prompt's own rules and requires the validator to accept it, with one mutation per rule.
- Detail: appendices 04, 08.

### H. Followup and resume  (live)

- **Claude followup fails "session_expired" because the estate launcher (outside this repo) re-runs account autoselect on `--resume`** and can land on an account that does not hold the session. Confirmed on the devbox: three failed followups printed `usage autoselect` picking a different account than the source run used. Delegate then mislabels it and tells the caller to "relaunch with --resumable," even though the run was resumable. Fix in the launcher (stay on the account that holds the session), plus a truthful message and a fall back to relaunch in delegate's workflow retry.
- **Followup is opt-in at launch and nothing surfaces or recovers it.** `resume` cannot restore continuity for a run launched without `--resumable`. Recommended: make Codex and Claude work Runs resumable by default, with an opt-out. This changes a storage/privacy default (native session files persist for every work Run), so it is your call.
- **`resume`/`followup` treat everything after the handle as prompt text**, and the only warning prints after a blocking run ends. `followup x fix it --dry-run` still launches for real. Fix: refuse on an exact known flag in the tail unless `--` is used.
- Detail: appendix 09.

### I. Worktrees: two definitions of "dirty," and orphans with no exit  (live)

Live-lane deletion (the August incidents) is mostly fixed by an owner-liveness check and a launch lease. What remains: `prune` and `remove` use raw `git status` while completion cleanup discounts launch-seeded files and the beads/papercuts globs, which is why cleanup of clean-but-synced worktrees dead-ends. A run launched with `--cwd` inside a delegate worktree registers under that worktree, and removing the parent hides the child. `reap --path` skips record-less entries as live even with `--force`, while error messages tell you to use it. `--force` still overrides the live lease. Nothing warns when a process has its cwd inside the target.

Also, in persistent worktrees the dirty-sync step replaces tracked symlinks that point outside the source with placeholder files, which git sees as typechanges; one cut reports a placeholder set committed and pushed to public GitHub (the mechanism is from reading the code; nobody reproduced it).

- **Recommended fix:** make `inspect_worktree` compute effective dirt by default (same call for prune, remove, and retirement); let `reap --path --force --yes` remove a record-less pool entry after the existing validation; limit placeholder substitution to untracked paths the sync itself mirrored.
- **Decision needed:** should `--force` ever override a live lease? Today it reproduces the incident on request. Alternative: a separate `--kill-live`.
- Detail: appendix 06, and 05 for the symlink issue.

### J. Shared runtime, install, and entrypoints  (live)

- The in-repo `install-local` skill still tells agents to `rsync --delete` into `~/.delegate/src`. That path is now a symlink into an in-use release directory, which `docs/live-runtime.md` says never to do. I verified both sides. This is a dangerous instruction sitting in a skill.
- The promote tool has no check that the candidate contains the commits it replaces, which caused one regression.
- `bin/delegate.py` and the tracked profile shim have no Python-version guard. On Xcode's Python 3.9 they die with a raw `ImportError` (I reproduced it). The working fix exists only in the untracked, diverged live launcher, so a fresh install from the template brings the bug back.
- The dev gate is five different commands, and test-environment isolation is a growing list of variables to scrub (one per production variable that leaked into a live lane). The lock guard reads `/proc/locks`, so it does nothing on the Mac. The open bead about six Mac test failures is already fixed by an existing commit and can be closed.
- **Recommended fix:** rewrite or delete the `install-local` skill; add an ancestry check to promote; put a Python 3.11 guard at the top of `bin/delegate.py` and `__init__.py` and port the interpreter picker to the tracked shim; make one gate command the only documented one; turn the test env scrub into an allowlist.
- Detail: appendices 06, 10.

### K. The lane is never told what it can and cannot do  (partly fixed)

The lane's environment is decided in five places (ambient copy, root variables, launch temp and stubs, workspace `--env`/`--setup`, profile overrides) and stated nowhere. Safe-mode prompts say "do not mutate" but not that there is no network, where scratch is, or which files are missing; worktree notes never say gitignored state such as `node_modules` is absent. Most temp-dir cuts are fixed (09-22), but only for safe and isolated runs; `--isolation none` work still inherits the caller's temp dir. Detached launches (systemd) lose the ambient profile and PATH the pin depends on.
- **Recommended fix:** resolve one lane-environment record at launch (execution root, writable roots, HOME status, temp dir, network, excluded state), show it in dry-run, the manifest and `describe`, and render a short block in the lane's prompt. It rides naturally on the write guard (A). For detached launches, the `--unit` wrapper already tracked.
- Detail: appendix 05.

### L. Smaller structural items

- **Placement grammar:** global options are hoisted from anywhere (and eat prompt words: `review the --group flag` sets a group), while launch options after the prompt silently become prompt text with a warning. Recommended: one rule, options recognized only before the first positional or `--`; a flag-shaped token in the tail is an error naming `--`.
- **"Usage errors exit 0"** (eight cuts) is a real exit code 2 hidden by pipes, backgrounding, or systemd. Not a CLI bug; a docs/skill note.
- **Inspection defaults** serve post-mortems: `wait --json` on timeout dumps full snapshots (~28k tokens); `runs show` still gives no useful hint (open bead).
- **Registry is per-workspace**, and a relative `--prompt-file` resolves against the shell cwd rather than `--cwd`.
- **Codex children run `--ephemeral` by default.** The agent's best explanation of the "no thread with id" subagent failure is that a forked ephemeral thread cannot be found. Unproven; `docs/troubleshooting.md` blames Claude Code instead, contradicting every cut, which name Codex. A small paid probe would settle it.
- **Codex overlay warning** (18 cuts, every Codex launch): the base config names a Codex profile `delegate` whose overlay file exists in only one of the two realms. Machine state, not a code bug; clear the setting.
- **Lane liveness resource limits** (a vitest run that spawned about 55 GB of workers): a config default for the test runner is the right level, not runner machinery.

## 3. Not delegate's problem

Misfiled or belonging elsewhere, so the fixes are not delegate's to make:

- **About 89 nested-delegate-refused cuts** (07-19 to 07-22): the benchmark repo's own PATH shim, since fixed there (07-26). A few small delegate-side follow-ups came out of it (the `datetime.UTC` crash is J; exposing run-identity variables in `describe`).
- **Claude account pinning on `--resume`**, launcher wrapper and provisioning: `linux-devbox` (`estate-harness.py`, the devbox `delegate` launcher, dotfiles).
- **writing-plans compiler** (`~/Code/writing-plans`): roughly half of the workflow-contract cuts, plan-state versus journal ("second truth"), close-agent prompts.
- **Herdr installer, `delegate-export` script** (in `wade-litigation-discovery`), Grok's docker-socket sandbox refusal, Codex context-compaction session loss, fleet attribution.
- **Ledger hygiene** itself (next section).

## 4. Live small bugs with one-line fixes (1 to 4 verified by me; 5 is the agent's finding)

1. Mail-push injects `-c hooks=true`. Codex 0.157.1 answers `invalid type: boolean true, expected struct HooksToml`. Use `--enable hooks` or nothing; three tests assert the broken flag.
2. The notifier passes `--allow-self`; the installed Post 0.9.0 rejects it. The tests use a fake `post` that only echoes argv. A notified unattended run completes silently.
3. The `install-local` skill's rsync instruction (J).
4. No Python-version guard on the tracked entrypoints (J).
5. The Codex overlay warning setting (L).

## 5. Ledger hygiene

- "Resolved" has meant "someone wrote a workaround," or "fixed on main but not installed," not "fixed and shipped." Examples the agents found: the headless-background failure recurred after two cuts were marked resolved; watchdog cuts kept arriving after the watchdog was deleted, because workflows were pinned to old code.
- Conversely, many cuts stay open though fixed. Agent counts: about 40 of 95 in the silent-success family; about 40 of 60 in the workflow-contract family; most of the CLI/help family; every worktree cut except a few. The open bead about six Mac test failures is fixed by an existing commit.
- **Recommended:** a one-time sweep closing stale rows with the fixing commit, and a convention that a resolution names a commit and a release. A cheap tool version: close on release rather than on merge.

## 6. Decisions for you

The ones that change what gets built:

1. **Work write guard** (A): on by default for work lanes, or opt-in first? Seatbelt on the Mac, or Linux-only first? Should the cells enable the bwrap safe backend they are already configured for? Should the Mac keep `external-sandbox` when no outside sandbox exists?
2. **Quiet work Runs that changed files** (C): keep `succeeded`, add a `degraded` flag, or fail?
3. **Known-bad lane** (B): refuse a launch, or only warn? My pick is refuse, with an override and a short expiry.
4. **Resumable by default** for Codex and Claude work Runs (H). Storage/privacy default versus fix loops that need one command.
5. **Workflows** (F): opt-in `--repin` on resume? Adopt live children on resume (after one live test)? `--foreground` or the `--unit` wrapper?
6. **Loop detection and the output cap** (D, E): kill or warn; degrade the cap instead of failing.
7. **`prune --force` overriding a live lease** (I).
8. **`install-local` skill**: delete, or wrap the promote tool.
9. **Small ones:** strict flag placement (L; my pick is strict); OMP explicit-provider selectors pinned by default; clear the Codex profile setting; whether delegate should own a child-tool denylist for `bd`; whether work mode should say "no human is available, execute the approved task"; a small paid probe of Codex `--ephemeral` and subagent fork.

## 7. Suggested order (dependencies, not durations)

1. The five small fixes and the ledger sweep. Independent; they also stop new duplicate cuts.
2. B, then the pieces that ride on it (per-lane health in capabilities, workflow gate short-circuit). C's detection can use the same accumulator.
3. D. Independent of B; touches the stream capture and report path.
4. C's prevention (env flag and prompt clause) can land any time; its detection after B's accumulator field exists.
5. A, after decisions 1 and 8. Then the lane-environment record (K) and real `--forbid-commit` build on it.
6. F, starting with pin visibility and foreground mode; adoption after the live test.
7. The conformance tests in G can start now; the docs-parse test stands alone.
8. H: the launcher fix (other repo) lands before resumable-by-default, otherwise more followups hit the account bug.
9. I and E once the above settle.

## 8. Things to know about this run

- One agent (the CLI/help diagnosis) accidentally launched a real Codex work Run while probing flag placement (prompt "hello", no changes, about 57k input tokens on the work profile). The run finished cleanly and the tree is clean. This violated my read-only instruction; it is disclosed here and in the appendix.
- Two agents wrote their reports to a mistyped scratch path; I recovered both. No repo files were changed by any agent.
- Nothing here is committed. The appendices are copies of each agent's report, unedited except for filename.

## Appendices

Each is one family's full diagnosis (root causes with evidence and file:line pointers, still-live status, alternatives, misfiled cuts, stale cuts, open questions). Cut ids are the ledger ids; look one up with `papercuts list --all --status all --limit 100000` and filter, on the Mac or via `ssh devagent`.

| # | Family |
| --- | --- |
| 01 | Silent success, lost output, misclassified failures |
| 02 | Preflight, auth, model and engine health |
| 03 | Workflow supervision and resume |
| 04 | Workflow contracts and compiled plans |
| 05 | Lane isolation and environment |
| 06 | Worktrees, shared runtime, dev gate |
| 07 | Lane liveness and stalls |
| 08 | CLI surface, help, docs, parser |
| 09 | Followup, resume, inspection, launch |
| 10 | Nested delegate, native subagents, coordinator mail |
