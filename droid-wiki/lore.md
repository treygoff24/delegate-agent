# Lore

This page summarizes Delegate Agent history from git commits, release tags, and tracked files.

## Era 1: Foundation and tracking, May 2026

- 2026-05-20: The project began with commit `65aa1a2`, “Create delegate agent foundation.” Surviving foundation files include `README.md`, `bin/delegate.py`, `src/delegate_agent/cli.py`, `config.example.json`, and early tests under `tests/`.
- 2026-05-20: Isolated Cursor safe review mode landed in commit `8558b2f`, establishing safe review as an early design axis.
- 2026-05-21: Run tracking, snapshots, retention, and config layering landed in commit `aced8f8`. The surviving modules include `src/delegate_agent/run_registry.py`, `src/delegate_agent/runner.py`, `src/delegate_agent/retention.py`, and `src/delegate_agent/rendering.py`.

## Era 2: Codex and policy expansion, May 2026

- 2026-05-22: Codex planning and policy work shaped `src/delegate_agent/config.py` and the policy fields later documented in `docs/security-model.md`.
- 2026-05-22: Codex command parsing and argv construction expanded `src/delegate_agent/cli.py` beyond Cursor and Droid.
- 2026-05-22: Generic safe isolation replaced Cursor-only isolation, with planning code now centered in `src/delegate_agent/isolation.py`.

## Era 3: Persistent worktrees, May 2026

- 2026-05-25: Worktree isolation implementation added durable worktree behavior covered by `src/delegate_agent/isolation.py` and `tests/test_delegate_isolation.py`.
- 2026-05-25: Persistent worktree management landed, anchored by `src/delegate_agent/worktree_mgmt.py` and `tests/test_delegate_worktree_mgmt.py`.
- 2026-05-25: Worktree execution, removal, cleanup rendering, record payloads, garbage collection reloads, and edge cases were split across `src/delegate_agent/worktree_execution.py`, `src/delegate_agent/worktree_mgmt.py`, and `src/delegate_agent/worktree_commands.py`.

## Era 4: Open-source and security hardening, June 2026

- 2026-06-02: Public docs such as `docs/agent-setup.md`, `docs/cli-reference.md`, `docs/configuration.md`, `docs/security-model.md`, `docs/troubleshooting.md`, and `docs/worktrees.md` were added.
- 2026-06-02: Secret redaction and ReDoS hardening landed, with display redaction now isolated in `src/delegate_agent/redaction.py`.
- 2026-06-02: Per-subcommand help landed through `src/delegate_agent/command_help.py`.
- 2026-06-02: Safe-mode policy began rejecting sandbox and hook bypasses through validation in `src/delegate_agent/config.py`.

## Era 5: Release train, June 2026

- 2026-06-05: `v0.1.3` shipped Codex stream events, completion-report recovery, bounded recovery, and display-side redaction.
- 2026-06-08: `v0.1.4` fixed inherited stdin hangs for child processes, centered in `src/delegate_agent/runner.py`.
- 2026-06-09: `v0.2.0` shipped provider-aware reasoning effort in `src/delegate_agent/reasoning.py`.
- 2026-06-12: `v0.3.0` added the Kimi Code harness.
- 2026-06-15: `v0.4.0` made safe isolation stricter for Cursor, Droid, and Kimi.
- 2026-06-18: `v0.5.0` added Claude Code headless support with config in `src/delegate_agent/config.py` and stream parsing in `src/delegate_agent/harness_events.py`.

## Longest-standing features

The CLI entry point `bin/delegate.py`, main orchestration module `src/delegate_agent/cli.py`, project framing in `README.md`, JSON examples under `examples/`, and early parser/execution tests under `tests/` all trace back to May 2026.

## Deprecated or replaced features

Cursor-only safe isolation was replaced by generic safe isolation in May 2026. Child processes inheriting Delegate stdin were replaced with explicit stdin handling in June 2026. Safe-mode `--isolation none` for Cursor, Droid, and Kimi was replaced by required real isolation in `v0.4.0`.

## Growth trajectory

Delegate grew from a compact CLI into a multi-runtime launcher with tracking, reasoning effort, Kimi and Claude adapters, and persistent worktree lifecycle management. See [by the numbers](by-the-numbers.md) for the current size snapshot.
