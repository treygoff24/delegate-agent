# Delegate papercuts fixes (2026-09-28): what a harness built on Delegate must handle

Audience: agents building Loom, or anything else that launches `delegate` and reads its output. This covers every behavior change from the 2026-09-28 papercuts work: new defaults, new fields, new error codes, and the write guard. `CHANGELOG.md` [Unreleased] has the same facts entry by entry; this document groups them by what your code has to do about them.

## Status

- The work is on `main` (2026-09-28, full test suite green: 4673 passed). Moving `main` installed it on the Mac. The devbox agent user installs it at its next daily estate-sync, which pulls `main` and promotes it.
- A machine that has not picked it up yet still behaves like 0.31.0. Detect the contracts below at runtime (field present or absent) rather than by version number.
- Diagnosis behind every change: `docs/audits/2026-09-28-papercuts/REPORT.md` and its `appendix/`.

## The changes most likely to break an integration

1. **Codex and Claude `work` runs save their native session by default.** `--resumable` is no longer needed for `delegate followup`. Opt out per launch with `--no-resumable` (input JSON `resumable: false`), or per engine with `codex.resumable` / `claude.resumable: false`. Session files now accumulate in the harness's own store (`CODEX_HOME/sessions`, Claude's `projects/`), outside `.delegate/`, outside redaction, and outside Delegate's pruning. A succeeded, resumable persistent-worktree run keeps its worktree (`worktreeRetained: "resumable_session"`), so a fan-out leaves one worktree per run until `worktree prune --merged`, `worktree gc`, or `worktree remove`. Safe, call, `--pass-through`, other engines, and workflow `agent()` children (unless `resumable=True`) are unchanged.
2. **The tracked output cap is off by default.** `<engine>.trackedStreamMaxBytes` defaults to `null`; a verbose run is never stopped as `output_limit_exceeded` unless you set a cap. `stdout.log` keeps the whole stream and is flushed on every write. Runaway streams are the stall watchdog's job (`runaway_output`).
3. **New exit code 4: `lane_known_bad`.** A launch can be refused in milliseconds, before any worktree or child exists, because an earlier run on the same lane failed with a persistent provider error. Handle exit 4 as "do not retry this lane now"; the payload says why and for how long. `--force-launch` (global) overrides.
4. **A `succeeded` run can carry `degraded: true`.** Status stays `succeeded` and `wait` still exits 0, but the child ended its turn with work unfinished. Check `degraded` before accepting a result.
5. **A failed run can be continued automatically once.** A Codex or Claude work run that dies on a transient provider drop is resumed in the same session as a new run linked by `followupOf`. The final result you get is the continuation's; its envelope carries `autoResume`.
6. **Claude work runs have background tasks disabled.** Delegate sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, adds `--disallowedTools Monitor`, and raises `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS` to the run's `--timeout` (at least two hours). A profile `env` or workspace `--env` cannot override these.
7. **A typed OMP `provider/model` id is pinned.** A literal `provider/model` behaves as `--continuity-mode pinned`: Delegate turns off OMP's cross-provider fallback, and a different provider being served fails the run as `model_continuity_paused`. Aliases (keys of `omp.models`) stay `fungible`. A bare OMP name that is neither an alias nor a `provider/model` fails with `invalid_alias` once a discovered catalog exists.
8. **`--force` no longer removes a worktree a live run holds.** `worktree remove`, `prune`, and `reap` refuse live owners even with `--force`; only the new `--kill-live` overrides.
9. **Work lanes run under a write guard** on Linux by default (bubblewrap). On the Mac it is opt-in. See the write guard section; it changes what a lane can write and adds fields to the manifest.
10. **`resume` and `followup` refuse their own options in the prompt text** (`delegate followup x fix it --dry-run`) with `option_after_handle`. Put `--` before a prompt that must contain such a token.

## Exit codes and error codes

| What | Meaning | What to do |
| --- | --- | --- |
| exit `4`, `lane_known_bad` | Launch refused before any child started; the lane is marked known-bad. | Pick another lane or wait for `expiresAt`; `--force-launch` to override. |
| `model_continuity_paused` | A pinned run (now including typed OMP ids and ungrouped `call`s) was served by another model or provider. | Treat as a failed run; the message names what was served. |
| `invalid_alias` (OMP) | A bare OMP name that is not an alias or `provider/model`. | Use an alias or a full `provider/model`. |
| `option_after_handle` | A `resume`/`followup` option appeared inside the prompt text. | Move the option before the prompt, or add `--`. |
| `session-missing` | `followup` on a run with no recorded native session. | Use `delegate resume <handle>` (the error says so). |
| `session_expired` (`failureKind: session_lost`) | The session was saved but cannot be found, usually a launch under a different account than the one holding it. | `delegate resume <handle>`. |
| `repin_children_running` | `workflow resume --repin` while a child is running or starting. | Wait, then repin. |
| `write_guard_unavailable` | Guard backend missing or failed preflight, with `isolation.writeGuard.onUnavailable: refuse`. | Fix bubblewrap/Seatbelt or use `warn`. |
| `ledger_salvage_failed` | A worktree removal could not save changed ledger files first. | Removal was refused; nothing was lost. |
| `run_active`, `run_not_terminal`, `process_group_alive`, `worktree_leased`, `nested_run_active`, `nested_registry_unreadable` | Worktree removal refused because something live owns it. | `--kill-live` is the only override. |
| `invalid_provider_errors_config`, `invalid_isolation_config` | Unknown key or wrong type in the new config sections. | Fix the config. |

New `failureKind` values: `lane_known_bad`, `provider_exhausted` (workflow only), `session_lost`.

## New fields on envelopes, run state, and snapshots

All are absent when they do not apply, so test for presence.

- `providerError` on a failed run: `status`, `providerCode`, `message` (bounded, redacted, emails masked), `engine`, `signature`, `class` (`persistent`, `transient`, `unknown`), `scope` (`lane` or `request`), `hint`. Branch on `signature`, never on message prose. The signature table is in `docs/troubleshooting.md`. One engine-keyed table decides it, status first, then provider code, then text. Plain throttling (a bare HTTP 429) is recorded as `rate_limited` but does not set the run's failure reason.
- `lane_known_bad` refusal payload: `signature`, `class`, `hint`, `lane` (engine, provider, model, account, an 8-hex `credential` tag), `expiresAt`, `secondsLeft`, `markedRunId`.
- `degraded` (only when true), `degradedReason` (`ended_waiting_on_background_work` or `background_work_unfinished_at_exit`), `degradedEvidence` (bounded lines), plus a `degraded=...` warning. Shown on the launch envelope, `state.json`, `wait` (JSON and a `degraded:` text line), `snapshot`, `runs`/`ps` (`[degraded]` prefix in text), the completion-report view, and for workflow children the `agent_child` journal event and `agent_meta()`.
- `autoResume` on an automatic continuation: `automatic`, `attempt`, `of`, `trigger`; or `attempted: false` with a `reason` when the continuation could not be built. Linked to the first run by `followupOf`.
- `servedModel` and `servedProvider`: what the child's stream says actually answered, beside `modelResolved` (the request) and inside `modelProvenance`.
- `writeGuard` in the manifest and in `--dry-run`: `status` (`enforced`, `partial`, `planned`, `off`, `unavailable`), `backend` (`bwrap`, `seatbelt`, `codex-native-sandbox`, or null), `protected`, `writable` (each `{path, reason}`), and when present `refused` and `unbound`.
- `resolutionKind: "cross_registry"` and `resolvedWorkspace` when `snapshot`/`run-output`/workflow reads found an id in another workspace (see below).
- `scratchReclaimedAt`, `scratchReclaimedBytes` on runs whose scratch was reclaimed.
- `worktreeRetained: "resumable_session"`, `worktreeSalvagePath`, and on removals `salvagePath`, `salvagedPaths`, `salvageRemovedPaths`.
- Work summaries record `fileInspectionStatus` beside `commitInspectionStatus` (`verified` or `unverified`); `noChanges` is true only when both are verified and empty.
- Failed, timed-out, or capped runs' completion reports quote the child's last substantive text under "Partial output recovered before the run stopped. This is not a completion report". Never treat that block as the child's report. Call-mode timeouts return `diagnostics` with `partialText`, `stdoutTail`, `stderrTail`.
- `delegate doctor` adds `knownBadLanes` and `authHealth`.

## Provider errors, known-bad lanes, and auto-resume

- **Which failures mark a lane:** only persistent, lane-scoped signatures (rejected credentials, no credit, usage limit, no model access, missing API key). Transient, request-scoped, and unclassified failures never mark, nor do endings Delegate itself decided (timeout, stall, cap, cancel).
- **What a lane is:** one engine on one provider, model, and account, plus a salted hash of the credentials the child would see. A bad key on one account never refuses another account on the same model. No key value is stored or shown.
- **Marker lifetime:** `providerErrors.knownBadLaneMinutes` (default 15; `0` disables). A success clears the marker. `--dry-run` never refuses. Markers live in `~/.delegate/state/lane-health/`.
- **Auto-resume:** only Codex and Claude work runs that saved a session and died on `stream_disconnected` or `provider_unavailable`. It happens once, and a drop in the continuation is final. It never applies to safe/call, `--pass-through`, structured-output runs, sessionless runs, or temporary worktrees. `providerErrors.autoResume: false` opts out.
- **Workflows:** with `on_failure="typed"`, `agent()` can return `lane_known_bad` or `provider_exhausted`. `AgentFailure.provider_error` carries the child's `providerError`, and `capabilities["providerOutcomes"]` advertises it. When the first `providerErrors.stageStopAfter` (default 3) results of a `phase()` on one lane all fail with the same persistent lane-scoped signature, the rest of that stage's calls on that lane are skipped (`stage_lane_stopped`, `agent_lane_skipped` journal rows), and a call with an engine fallback chain moves to its next engine. Workflows never pass `--force-launch`.
- **Auth health:** `delegate capabilities refresh` runs read-only probes (`estate-cursor status`, `estate-omp usage`) and records `ok`, `logged_out`, `limit_reached`, `partial`, or `unknown`. `unknown` never refuses a launch. `DELEGATE_AUTH_PROBES=off` skips them.

## Turn-end detection (degraded runs)

A headless Claude child ends when its model stops; background Bash and Monitors die with it. Two changes follow:

- **Prevention.** Background tasks are disabled for Claude work runs (item 6 above), and every framed `work` and `safe` prompt carries a two-sentence rule: ending the turn ends the run, and long jobs run in the foreground and finish first.
- **Detection.** The final message is checked for "waiting on my own unfinished work" (any engine; runs with `--output-schema` are not text-checked). Claude's `background_tasks_changed` event is read at exit. Either sets `degraded`. Waiting on the requester or on a third party is not flagged.

Nothing fails automatically. If your harness must not accept unfinished work, it checks `degraded` itself.

## The write guard

Work mode runs the child with the caller's own filesystem rights; one confused lane deleted most of the devbox agent home on 2026-09-24. The guard is a protect-list, not an allowlist. Named irreplaceable paths become read-only for the child; everything else stays writable. Reads, network, and credentials already in the environment are untouched.

- **Protected by default** (each only when it exists): `~/.ssh`, `~/.gnupg`, `~/.config/gh`, `~/.config/gcloud`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.netrc`, `~/.git-credentials`, `~/.password-store`, `~/.ai-profiles`, the installed Delegate runtime (`~/.local/bin/delegate`, `~/.delegate/src`, `releases`, `bin`, `config*.json`), and the code root (`~/Code`), so a lane in one checkout cannot write a sibling.
- **Re-opened for the run itself:** the execution root, the git common directory, the run registry, run scratch and compact temp, mail-push homes, and the selected engine's own home (only when it provably holds one profile). Add `--writable PATH` (repeatable, CLI only) or `isolation.writeGuard.writable`.
- **Estate launchers.** A lane whose program name starts with `estate-` (`estate-claude`, `estate-codex`, `estate-omp`, `estate-cursor`, `estate-grok`) gets the whole profiles root re-opened. The reason is that the launcher picks an account, refreshes its token, and writes session, plugin, and lock state there before the engine starts, and with the root read-only it crashed. On the estate that means most lanes can write every profile's files; `~/.ssh`, the runtime, and `~/Code` stay protected. Devin, Kimi, and OpenCode lanes keep `~/.ai-profiles` protected. A follow-up to narrow this is filed.
- **Backends.**
  - **Linux:** bubblewrap, on by default.
  - **macOS:** Seatbelt (`sandbox-exec`), opt-in via `isolation.writeGuard.macosSeatbelt: true`.
  - **Codex with its own sandbox on:** not wrapped. It gets `--add-dir` roots instead.
  - **Codex with its sandbox bypassed:** wrapped like any engine. That combination is not live-tested on the Mac yet.
- **When the backend is missing or fails preflight:** `isolation.writeGuard.onUnavailable` decides. `warn` (the default) launches unguarded with a warning. `refuse` fails with `write_guard_unavailable`. On Linux, one path that will not bind is dropped alone, and status becomes `partial` if it was a protected path.
- **Switches:** `DELEGATE_WRITE_GUARD=off|on` overrides config; `isolation.writeGuard.enabled: false` turns it off. Safe mode and `--pass-through` runs are never guarded.
- **Prompt note:** work prompts gain two sentences naming the protected paths, so a lane reports a blocked write instead of working around it.
- **Known limits:**
  - A protected path that is itself a symlink is protected at its target while the link stays replaceable.
  - An existing hard link to a protected file is the same inode, so writing through it changes the file.
  - A second profile nested more than three levels inside an engine home is not found.
- **Live-checked 2026-09-28:** a Claude lane through `estate-claude` succeeded under Seatbelt on the Mac and under bubblewrap as the devbox agent user. Each lane's write into `~/.ssh` was refused.

## `--forbid-commit`

`--forbid-commit` now works in every isolation mode, including `--isolation none`. It installs run-owned git hooks through `GIT_CONFIG_PARAMETERS`; Codex's environment filter drops variables named with `KEY`, which broke the indexed form. After the child exits, Delegate fails the run if commits remain ahead of the base or if the HEAD reflog shows a commit made since launch. An unreadable reflog counts as unverified and fails the run. The hooks are a tripwire, not a wall.

## Workflows

- **Pinned runtime:** `workflow resume` and `workflow status` say when the pinned runtime differs from the live one (`runtimePin`: `differs`, `pinned`, `live`). A plain resume keeps the pin.
- **`--repin`:** on `workflow resume`, `workflow run --resume`, and `workflow approve`, `--repin` moves the pin to the live runtime. It keeps the journal and step keys, journals `runtime_repinned`, and rolls back byte-for-byte on failure (`runtime_repin_rolled_back`). After a repin, `capabilities` describes the live runtime.
- **Keying check:** `workflow check` warns when a script keys some steps and not others.
- **Resume cancels running children:** a resume cancels any child the previous attempt still had running and relaunches that work. It does not adopt them.
- **Signals:** the supervisor relays `SIGINT` to its children like `SIGTERM` and `SIGHUP`, and journals `supervisor_signalled`.
- **Structured retry fallback:** a structured retry whose session is lost falls back to a fresh relaunch in the same worktree, but only when that worktree is verified untouched.

## Worktrees, scratch, and finding runs

- **One definition of "dirty":** what the lane changed. Files seeded from a dirty source that still match their digest do not count, and neither do ledger files (`.beads/**`, `.papercuts.jsonl`).
- **Ledger salvage:** changed ledger files are copied to `<Registry>/salvage/` before any removal.
- **`reap` on orphans:** `worktree reap --force` can now remove an orphan pool entry Git still links. Every safety check re-runs under locks right before removal.
- **Committed symlinks:** symlinks committed to the repository are no longer replaced with placeholders in persistent worktrees.
- **Scratch reclaim:** a finished run's scratch is reclaimed after `tracking.retention.scratchDays` (default 3) while the run record stays. `delegate runs reclaim [--older-than DAYS] [--dry-run]` does it on demand.
- **Finding runs across workspaces:** an id not in the current workspace is looked up in `~/.delegate/registries.json`, a roster of up to 256 workspaces.
  - `snapshot`, `run-output`, and the workflow read verbs read a match found in exactly one other workspace.
  - `wait`, `cancel`, `resume`, `followup`, `worktree show`, and the workflow write verbs never act across workspaces. They fail and put the exact `delegate --cwd <workspace> ...` command first in `nextActions`.
  - Numbered aliases are never auto-resolved.

## Smaller fixes you may have worked around

- **`--mail-push` on Codex:** passes `--enable hooks`, not `-c hooks=true`, which Codex 0.157 rejects.
- **`--notify` room pings:** work with post 0.9.0. A room ping never reaches its own sender.
- **Old Python:** a Python older than 3.11 exits 2 with a plain message. `bin/delegate-profile-shim` finds a 3.11+ interpreter itself.
- **Model warnings:** Cursor labels with a context-window token (`Grok 4.7 256K Extra High`) are no longer a false `model_substitution`.
- **Usage synopses:** in `--help`, `describe`, and `docs/cli-reference.md` they now match the parser, and a conformance test keeps them matched.
- **OMP compaction:** oversized `tool_execution_update` records are compacted to a stub in capture.
- **Missing-key hint:** now names the provider cleanly (live omp says "No API key found for mistral.").

## What is still open

- Mac config not yet changed, awaiting Trey: putting Codex's own sandbox back for work lanes (`policy.harness.codex.work.bypassApprovalsAndSandbox: false`) and turning on the Mac write guard (`isolation.writeGuard.macosSeatbelt: true`).
- Deferred, filed as beads in this repo:
  - workflow resume adopting live children
  - one resolved lane-environment record
  - strict option-placement grammar
  - option applicability in the registry
  - watchdog loop detection
  - bounded assistant text for long runs
  - narrowing the estate-launcher profiles re-open
- The estate launcher and `bin/delegate-profile-shim` still disagree on `DELEGATE_CONFIG` versus `--auth-profile`. Fixing that is a linux-devbox change.
