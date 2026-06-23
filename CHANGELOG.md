# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.5.0]: https://github.com/treygoff24/delegate-agent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/treygoff24/delegate-agent/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/treygoff24/delegate-agent/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/treygoff24/delegate-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/treygoff24/delegate-agent/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/treygoff24/delegate-agent/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/treygoff24/delegate-agent/releases/tag/v0.1.3
