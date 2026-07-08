# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Delegate Workflows: a Python DSL supervisor for multi-agent fan-out, durable
  journaling, nested workflow calls, schema-validated `agent()` results,
  approval gates, resume, kill, saved workflows, and workflow discovery in
  `describe`/help/docs.
- Slash pass-through: launch prompts that intentionally start with a harness
  slash command can be sent verbatim when the target mode's safety boundary does
  not depend on Delegate's prompt preamble. `--pass-through` also suppresses the
  skill preamble and completion-report suffix while preserving safe-mode
  boundaries.
- Added `devin` as a first-class engine for Cognition's Devin CLI across
  `safe`, `work`, and `call` modes. Devin uses prompt-file transport,
  config-driven model selection (`devin.defaultModel`, default `swe-1.7`),
  read-only safe/call enforcement through a Delegate-generated
  `--agent-config`, and `--permission-mode dangerous` for work/default-call
  print-mode runs.
## [0.11.0] - 2026-07-05

Profile-guard calibration fix for issue #9: a shell carrying `AI_PROFILE=work|personal` with no matching `~/.delegate/config.<profile>.json` no longer presents a half-configured install as a total CLI outage.

### Added

- `delegate config sync-profiles` materializes missing `~/.delegate/config.<profile>.json` overlays (`work`, `personal`) from the validated base config. It never clobbers an overlay you already edited, validates the merged effective config before any write, and writes each overlay with private-file permissions. `delegate config init` now writes the same overlays alongside the base config. Both share one path convention (`config.profile_config_path`, `config.PROFILE_CONFIG_NAMES`) so the writer and the guard cannot disagree on overlay naming.

### Fixed

- `AI_PROFILE=work|personal` with a missing or unreadable profile overlay no longer hard-blocks every command, including the read-only diagnostics you would reach for to debug it. Recognized-but-configless profiles now block only launch and mutation commands — with remediation text that points at `delegate config sync-profiles` and the `env -u AI_PROFILE` / `DELEGATE_CONFIG=` bypasses — while read-only diagnostics (`profiles`, `runs`, `run-output`, `snapshot`, cached `capabilities`, `worktree show`/`list`, `describe`, `models`, `help`, `version`) pass with a stderr warning. An unrecognized non-empty `AI_PROFILE` now warns and continues on the base account instead of silently falling through.

### Security

- The fail-closed profile-crossover guarantee is now enforced inside the Python CLI (`delegate_agent.cli:main` via the new `delegate_agent.profile_guard`), classifying from the real parsed command rather than positional argv guessing. This closes a gap where the guarantee lived only in the optional shell shim: the pip console script, `python -m delegate_agent.cli`, and `bin/delegate.py` all reach `main` with no shim in front, and previously fell through to the base account on a missing overlay. The guard no-ops when `DELEGATE_CONFIG` is already exported (shim precedence) so the two layers compose without a double check. The tracked `bin/delegate-profile-shim` template applies the same check before Python starts, as an additional early gate; it scans all args for `capabilities refresh` so a mutation is never misclassified as a read-only probe.

### Packaging

- Published to PyPI as `delegate-agent-cli`: PyPI's separator-stripped name-similarity rule blocked the shorter `delegate-agent` (an existing `DelegateAgent` project collides). The installed console script is still `delegate`; only the `pip install` name changed.
- Hardened two tests (`test_execution_argv_and_prompt.py`, `test_wait_cancel_commands.py`) that raced child-process teardown against the isolation and cleanup assertions they were checking, which showed up as intermittent failures on the publish gate.

## [0.10.0] - 2026-07-04

Usage-audit fix wave: 82 sessions and 1,241 delegate invocations from one week of agent usage were mined for friction and failure modes, and the whole Tier 1–3 backlog was built across four decorrelated implementation waves plus live acceptance.

### Added

- `delegate wait` blocks on one or more runs until they reach a terminal state, using effective status so a dead child is a terminal failure rather than a hang. Supports bare-harness and `harness:model` latest selectors, `--group`, an optional timeout, and completion-report output; exits 0 (all succeeded), 1 (any failed/cancelled), or 124 (timeout). Replaces the ~100 hand-rolled polling loops agents wrote in the audit week.
- `delegate cancel` signals a run's recorded process group (SIGTERM → 5s grace judged by group liveness → SIGKILL) with layered safety: workspace-scoped resolution, terminal/stale refusal, `pid`/`pgid <= 1` guards, and a `ps`-lstart start-identity check that refuses to signal a pid older than the run (soft-degrades when `ps` is unavailable). A `cancelRequested` marker is stamped under the registry lock before any signal so the runner finalizer can never record a cancelled run as succeeded.
- `cancelled` is a first-class terminal status with normalized per-harness `terminalEvent`/`terminalStatus` mapping (e.g. Grok `stopReason: Cancelled` overrides an exit-0 success). Cancelled and failed runs without a child report now get a synthesized completion report (status, failure reason, redacted stderr tail, harness-appropriate remediation) so `run-output --completion-report` never dead-ends.
- Always-numbered aliases (`codex-1`, `cursor-2`, …). Bare harness names become latest-run selectors and `harness:modelAlias` selectors resolve the newest matching run; envelopes and text banners expose `requestedHandle`/`resolvedHandle`/`resolutionKind`, and generated follow-up commands always use the concrete numbered alias. `run-output` gains `--latest`.
- `resultQuality` classification on tracked runs (`ok` / `housekeeping_noop` / `empty` / `suspect_short` / `no_assistant_text`), computed at finalization and preferred from stored state at read time — closing the hole where an on-disk Droid "Plan is up-to-date." report surfaced as a clean success. Envelopes always carry `completionReportWritten` and `completionReportSource` (`child` / `delegate_synthesized` / `stdout_recovery`); auth failures classify as `auth_failed` from stderr-only patterns.
- `--include-dirty` on worktree work launches syncs uncommitted tracked changes and untracked non-ignored files through the same primitives as the safe-mode snapshot, replacing the stash-launch-pop dance; sync failures tear the worktree down before any child launches.
- `--group NAME` tags launches and selects on `runs`, `wait`, and `worktree remove`/`prune`.
- A per-run scratch `TMPDIR` is exported to safe and isolated runs after profile env; the codex lane is granted it via `--add-dir`.
- `did-you-mean` suggestions on unknown handles, a `list` → `runs` alias, corrected-command text on flag-order errors, and copy-paste command forms on invalid-mode errors.

### Changed

- `--forbid-commit` now implies `--isolation worktree` on both the CLI and JSON paths.
- Work-mode `noChanges` and quality warnings ride the top-level `warnings` array (including the runs table). Text `run-output` discloses char truncation like JSON, and `--tail`/`--max-chars` without a stream selection is now rejected instead of silently ignored.
- `describe` full output is a strict superset of `--summary`, both derived from `COMMAND_SPECS`.

### Fixed

- Registry first-init race fixed with locked init and unique atomic temp names; `write_json_atomic` cleans up temp files on failure. Run-latest tie-breaks use an explicit `registrationOrdinal` stamped under the registry lock (in-memory insertion order degrades through `save_index` on reload).
- Plaintext progress advances every line instead of freezing on the first; `call` mode returns a redacted `stderrTail` on failure and an empty-text warning instead of raw event noise; `droid call` prevalidates model aliases before creating the workspace and cleans up on every failure path.

### Security

- The native boundary read empirically reproduced a leak inherited from the safe-mode mirror: an untracked symlink with an absolute target pointing at the repo's own gitignored secret was recreated verbatim — readable and writable-through in an edit-capable worktree. The shared sync now recreates untracked symlinks only when the link is relative, resolves inside the repo, and the target is not gitignored (batched `git check-ignore -z --stdin`, failing closed to placeholders on unexpected exit codes), hardening safe mode and `--include-dirty` together. The hardlink variant cannot be closed by path-based exclusion and is documented as a caveat.

## [0.9.0] - 2026-07-01

### Added

- Stateless `call` mode for every engine (`delegate <engine> call "prompt"`, plus `droid MODEL call`). Call mode sends a one-hop prompt to a model and returns its output without a project tree, run registry, snapshot, or completion report — the generic "call a model, get the answer back" path for agents that don't need repo context. It runs in an empty temporary cwd that is always cleaned up, even on build-time failure.
- Call mode is **write-capable by default**, inheriting work-level harness permissions (the equivalent of `work` mode minus a repo). A new `--read-only` flag (and `readOnly` JSON input field) opts into the LLM-as-judge/grader contract: it drops the child to each engine's `safe`-mode read-only capability and prepends a neutralizing preamble telling the model there is nothing to inspect or mutate. `--read-only` applies only to `call` and is rejected with `safe`/`work`. Pairs with Codex `--output-schema` for structured verdicts. See `examples/task.judge.json`.
- Call JSON output includes `textChars` and `textTruncated` so callers can detect when bounded assistant text kept only the head and tail of a large response. Human-mode `dry-run` now prints `warning:` lines.

### Security

- Call mode is not a security sandbox. The effective harness policy resolves through the work/safe tiers (default call keeps work-tier web-search/network access; `--read-only` call gets the safe policy), and work-mode approval/sandbox bypass never leaks into call mode. On engines without a native read-only sandbox (Cursor, Droid, Kimi) the neutralizing preamble is the only restriction under `--read-only`; `docs/security-model.md` documents the boundary.

## [0.8.1] - 2026-07-01

### Fixed

- Launch and `dry-run` commands now accept `--json` in the launch option tail before inline prompt text begins, so agent callers can append it after flags such as `--prompt-file` without tripping the misplaced-global guard. A later `--json` after prompt text still fails closed as ambiguous flag-like prompt text.

## [0.8.0] - 2026-06-29

### Added

- First-class `delegate grok {safe,work}` for xAI Grok Build CLI: prompt-file transport, tracked `streaming-json` snapshots, safe isolation required, worktree `--cwd` rewrite, harness-scoped bypass at `policy.harness.grok.work.bypassApprovalsAndSandbox`, and Grok `--effort` reasoning mapping. Safe mode pairs Delegate's isolated worktree copy with Grok's kernel-enforced `--sandbox read-only` profile, and the streaming `error` event is surfaced into the snapshot. Grok `--output-schema` is unsupported in this release because Grok `--json-schema` forces final JSON output, which breaks tracked streaming snapshots.
- `delegate config init` command to write an editable starter config from an installed package, so users no longer need a source checkout just to copy `config.example.json`.

### Changed

- WSL setup is now documented explicitly: install Python/Git/child CLIs inside WSL, prefer `/home/<user>/...`, and convert Windows paths with `wslpath -u`.

### Fixed

- Windows-style paths now fail with actionable WSL guidance instead of turning into confusing POSIX relative paths, and WSL runs fail loudly when `git` resolves to Windows `git.exe`. Workspaces under `/mnt/<drive>` now emit a warning about WSL filesystem performance and private-file semantics.

## [0.7.0] - 2026-06-29

### Added

- Profile-aware auth and environment switching. A new top-level `profiles` config block (`detectFrom`, `default`, `definitions.<name>.env`) lets one session run under a chosen credential/environment profile and have every spawned harness inherit it. The active profile is detected from an environment variable (`profiles.detectFrom`, e.g. `DELEGATE_PROFILE`/`AI_PROFILE`) or pinned explicitly with the new global `--auth-profile NAME` flag. Delegate resolves the profile once per request and injects its env into every child across tracked, pass-through, safe-isolation, and persistent-worktree paths. Profile `env` holds non-secret routing pointers only — secret-shaped keys are rejected at config load with `secret_in_profile_env`.
- `delegate profiles` command (with `--json`): read-only introspection of the resolved profile, its source (`flag`, a detection variable name, or `default`), and the non-secret env keys it injects. It never mutates config.
- `codex.fallbackProfile`: when a Codex run hits a classified usage limit on a clean work-mode baseline with no tool events, Delegate retries once under the fallback profile's account (same env, `CODEX_HOME` swapped). A fallback that resolves to the same account is a no-op. Completions record `codexAuthFallback` metadata.
- Codex `--output-schema FILE` flag and `outputSchema` run-input field for structured final output. Codex-only; OpenAI enforces the JSON Schema on Codex's final message. Relative paths resolve against the launch cwd and are locked absolute before isolation. When set, the completion-report prompt injection is suppressed so the schema owns the final message. Other engines reject it with `unsupported_output_schema`, and `delegate --json describe` advertises `engineCapabilities.<engine>.outputSchema` for feature detection.
- `delegate --json <command> --help` payloads now include `globalOptions` and `unsupportedGlobalOptions`, so agents can discover which global options (such as `--auth-profile`) apply to a command without parsing usage strings.

### Changed

- Safe-mode runs over a dirty Git tree now inject a bounded changed-file note into the prompt before the per-engine transport split, so the synced working-tree state reaches the child regardless of stdin/prompt-file/argv transport. Documentation across help, `describe`, `agent-help`, README, security-model, and the CLI reference now states plainly that safe mode mirrors uncommitted tracked edits and untracked non-ignored files into the isolated copy.
- `--auth-profile` is accepted only where a child auth/env selection actually happens: launches, `dry-run`, `run --input-json`, `delegate profiles`, and `capabilities refresh`. It is rejected for the cached `capabilities` report and for run-inspection, worktree-management, and discovery commands.

### Fixed

- Run retrieval hints (trailer, JSON payload, snapshot, run summary) are now workspace-qualified: the registry is per-workspace, so commands are rendered through `shlex.join` with the source `--cwd`, and the unknown-handle error explains that runs are per-workspace.
- `--forbid-commit` and reasoning-effort/model preflight errors are now actionable — they name the corrective flag or config key (`codex.defaultModel` / `droid.models`) and, in a non-Git workspace, explain that no-commit enforcement requires Git rather than demanding an impossible `--isolation worktree`.
- The codex usage-limit fallback retry now injects the active profile's full env with only `CODEX_HOME` swapped, instead of dropping the profile's other pointers onto a bare environment.
- The internal "codex auth attempt" delimiter is no longer written to a non-Codex run's stderr log when a profile happens to define both a `CODEX_HOME` and a `codex.fallbackProfile`.

## [0.6.0] - 2026-06-23

### Added

- Always-on best-effort credential scrubbing on `describe`/`models` discovery output.
- Heartbeat opt-in via config: `progress.enabled`, `progress.initialDelaySec`, and `progress.intervalSec`, plus `--no-progress` to override config for one launch.
- Foreground launch progress via `--progress`, emitted to stderr so JSON stdout remains machine-readable.
- Persistent worktree `workSummary` metadata, including dirty state, changed file counts, diff stat, and child-created commits. `--forbid-commit` now fails persistent worktree work runs if the child creates commits.
- `run-output --max-chars` for bounded non-raw stdout/stderr sections, plus `rawOutputBytes` metadata for intentional `--raw` reads.

### Changed

- Removed discovery `--redacted` cosmetic masking; `--summary` remains the compact discovery surface.
- `worktree list/show` now distinguish `branchMergedIntoSource` from `mergedIntoSource`; `mergedIntoSource` means the branch is merged and the worktree has no uncommitted changes.
- Kimi help and docs now match actual argv behavior: Delegate uses Kimi prompt mode and does not emit `--yolo` with `--prompt`.

### Fixed

- Heartbeat path scrubbing is URL-safe and covers additional container/CI absolute paths without corrupting `https://…` URLs.
- Global `--json` inference now applies consistently before more subcommands.
- Completion-report recovery now prefers substantive final assistant output over housekeeping/progress output when no explicit completion report exists.
- Worktree handle suggestions are scoped to persistent worktrees, and safe-mode prompts more clearly allow read-only investigation and text-only patch proposals.
- Shared fake-agent test harness output is quiet by default.

## [0.5.0] - 2026-06-18

### Added

- New `claude` engine wrapping Claude Code headless mode (`claude -p`). `delegate claude safe` and `delegate claude work` deliver the prompt on stdin and parse Claude Code's `stream-json` output, the same way the other harnesses are normalized. Requires Claude Code 2.1.x or newer (verified on 2.1.181) for `--effort`, `--permission-mode auto`, and `--no-session-persistence`.

- Claude safe mode runs in a temporary isolated workspace (detached worktree or directory copy) with `--permission-mode plan`, `--strict-mcp-config`, a Read/Grep/Glob tool set, and a read-only Bash allowlist (`git diff/status/show/log`, `rg`, `grep`, `ls`). Safe mode is added to the engines that require real isolation, so `--isolation none` is rejected for it.

- Claude work mode uses `claude.workPermissionMode` (default `auto`). Delegate only emits `--permission-mode bypassPermissions` when `policy.harness.claude.work.bypassApprovalsAndSandbox` is explicitly set; `workPermissionMode` itself rejects `bypassPermissions` so a global sandbox profile can never silently broaden Claude's permissions.

- Reasoning effort for Claude maps directly to Claude Code's native `--effort` (`low`, `medium`, `high`, `xhigh`, `max`), validated before launch and kept independent of the Codex/Droid model-capability cache.

- New `claude` config section (`binary`, `defaultModel`, `defaultReasoningEffort`, `workPermissionMode`, `noSessionPersistence`, `bare`) with validation, and Claude coverage across `describe`, `models`, `reasoning-capabilities`, and `dry-run`. Tool activity is surfaced as `tool.started` / `tool.completed` events parsed from Claude's `tool_use` / `tool_result` content blocks.

## [0.4.0] - 2026-06-15

### Changed

- Safe mode now requires real isolation for Cursor, Droid, and Kimi: `--isolation none` (or `isolation.safe = "none"` in config) is rejected for these engines with `invalid_isolation`, so safe runs always execute in a temporary isolated workspace. **Migration:** remove any `{"isolation": {"safe": "none"}}` override for cursor/droid/kimi and use `auto` or `worktree`. Codex safe still permits `none` because it enforces its own `--sandbox read-only`.

- `delegate droid safe` now defaults to a temporary isolated worktree instead of running in place in the source tree, matching Cursor, Codex, and Kimi safe.

- `delegate snapshot` now surfaces `creationContext` (`sourceHeadOid`, `sourceBranch`, `sourceGitCommonDir`, `plannedExecutionCwd`) for persistent-worktree runs, sourced from the run manifest when the snapshot omits it.

- `run-output` stdout/stderr section `truncated` now reflects whether the tail actually cut content, instead of always reporting `true` for live and archived tails. Parent agents that branch on `truncated` to decide whether to fetch `--raw` should re-check.

### Fixed

- A single corrupt or partially written per-run `state.json` / `manifest.json` no longer aborts whole-registry commands (`runs`, `snapshot`, `run-output`, `worktree list`) or blocks launching new runs. Bulk readers skip the bad file and degrade for that one run, while commands targeting a specific run still fail loud.

- `delegate kimi work` no longer passes `--yolo` together with `--prompt`; current Kimi CLI rejects that combination, and Kimi prompt mode already auto-approves tool actions.

## [0.3.1] - 2026-06-12

### Changed

- Troubleshooting guidance now covers Kimi binary checks, active config layer inspection, safe-mode dry-run limits, pass-through option placement, exact-payload completion-report behavior, and worktree cleanup flags.
- Delegate completion-report instructions now tell child agents to put exact operator-requested payloads, such as bare JSON, after the concise parent-facing report instead of wrapping them inside it.
- Local planning documents under `docs/plans/` are ignored as private working artifacts.

### Fixed

- `missing_binary` errors now include actionable JSON diagnostics (`configPath`, `configKey`, and optional `suggestedBinaryPath`) for configured child runtimes, including persistent worktree preflight and launch paths.
- Launch and dry-run parsing now reject misplaced global completion/pass-through options after the subcommand or mode, matching the documented option placement rules.

## [0.3.0] - 2026-06-12

### Added

- Kimi Code harness (`delegate kimi`). Safe mode runs in an isolated temporary workspace with Delegate's read-only safety prompt; work mode emits Kimi `--yolo` by default for edit-capable prompt-mode runs. Delegate intentionally does not use Kimi `--plan` for safe mode. Kimi prompt mode auto-approves tool actions, so safe mode's effective write boundary is the isolated workspace and the safety prompt is advisory. Model selection uses `kimi.defaultModel` config or the `model` field in JSON run input. Reasoning effort is not supported for Kimi in v1.

## [0.2.0] - 2026-06-09

### Added

- Provider-aware `--reasoning-effort LEVEL` for Codex, Droid, and Cursor runs (plus `reasoningEffort` in JSON run input). Values are literal and validated against per-model capability declarations resolved from config (`reasoning.capabilities`), a refreshable workspace cache, or bundled fallback data. Explicit requests fail closed with `unsupported_reasoning_effort`; an engine `defaultReasoningEffort` config default that cannot be satisfied is skipped with a recorded warning instead of failing every run.

- `delegate capabilities` reports the merged reasoning capability matrix; `delegate capabilities refresh` probes `codex debug models` and writes `.delegate/capabilities/reasoning.json` atomically with owner-only permissions. A malformed cache file is ignored at run time and overwritten by the next refresh.

- Prompt text no longer travels in child argv: Codex prompts are delivered via stdin and Droid prompts via a private temp file. Dry-run payloads and manifests show redaction placeholders plus a `promptTransport` field, and stdin delivery failures surface as a run warning on stderr and in the snapshot.

- `run-output --completion-report` now recovers the last interim assistant message for dead Droid runs as well as Cursor runs (marked `synthetic`); Codex recovery still requires a completed final turn.

### Changed

- `run-output --stdout`/`--stderr` without `--tail` or `--raw` defaults to a bounded 80-line tail instead of erroring with `missing_tail`. Text output marks tailed sections (`last N lines; full log B bytes`) and synthetic completion reports in the section header; JSON output carries the equivalent flags.

- `worktree show --latest HARNESS` resolves the most recent persistent worktree for that harness, intentionally ignoring newer non-worktree runs. `worktree list` JSON gains a `summary`; `totalPersistentWorktrees` is registry-wide while `allStatusCounts` is scoped to the `--harness` filter.

- Run listings probe each run's pid once per entry, so `effectiveStatus` and `staleReason` can no longer disagree about a process that exits mid-listing.

- `reasoning.capabilities` config keys are restricted to `codex` and `droid` (Cursor uses `cursor.reasoningEffortModels`), and effort strings reject whitespace, double quotes, and backslashes.

## [0.1.4] - 2026-06-08

### Fixed

- Child agent processes now receive EOF on stdin instead of inheriting Delegate's own
  stdin. This prevents Codex runs launched from orchestrators with open stdin pipes
  from hanging before they emit output.

## [0.1.3] - 2026-06-05

### Added

- Codex streaming events are now parsed by the run tracker. `item.started`, `item.completed`, `turn.started`, and `turn.completed` events surface `agent_message` text and `command_execution` tool activity from `codex` runs in snapshots and completion reports.

- `run-output --completion-report` recovers a completion report from the recorded child stdout stream when `completion-report.md` is absent and the run has finished. Recovered reports are reconstructed with the same event parser used during live tracking, and are marked `synthetic: true` with `source: "stdout.log"` in JSON output. Codex recovery only promotes an `agent_message` once the stream reaches `turn.completed`, so in-progress messages are never treated as a final report.

- Synthetic completion-report recovery is bounded and best-effort. Delegate reads only a limited stdout tail during recovery, including archived stdout, and reports a clean `missing_completion_report` error when no completed final message is available inside that recovery window.

- Display-side redaction now covers common credential shapes such as authorization headers, bearer/basic tokens, JWT-like strings, and common secret key-values in snapshots and run-output views.

- Prompt input from delayed stdin pipes is now accepted when no direct prompt or prompt file is supplied.

### Notes

- Releases before 0.1.3 predate this changelog.

[0.11.0]: https://github.com/treygoff24/delegate-agent/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/treygoff24/delegate-agent/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/treygoff24/delegate-agent/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/treygoff24/delegate-agent/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/treygoff24/delegate-agent/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/treygoff24/delegate-agent/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/treygoff24/delegate-agent/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/treygoff24/delegate-agent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/treygoff24/delegate-agent/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/treygoff24/delegate-agent/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/treygoff24/delegate-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/treygoff24/delegate-agent/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/treygoff24/delegate-agent/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/treygoff24/delegate-agent/releases/tag/v0.1.3
