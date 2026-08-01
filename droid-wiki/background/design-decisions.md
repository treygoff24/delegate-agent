# Design decisions

## Stdlib-only runtime

`pyproject.toml` declares no runtime dependencies. Delegate integrates with provider CLIs through process launch rather than SDK imports.

## Safe/work is a mode boundary

`src/delegate_agent/cli.py` and `src/delegate_agent/config.py` keep `safe` and `work` separate from reasoning effort. `src/delegate_agent/reasoning.py` records effort as model-thinking metadata only.

## Local registry instead of remote service

`src/delegate_agent/run_registry.py` stores run metadata under `.delegate/` in the source workspace. That makes `runs`, `snapshot`, and `run-output` available without running a service.

## No commit, push, merge, deploy, or publish commands

`README.md` and `CONTRIBUTING.md` state this boundary explicitly. Delegate can create child work in persistent worktrees, but humans or parent orchestrators handle integration.

## Prompt transport is explicit

Prompt transport lives in `src/delegate_agent/prompt_transport.py` and is recorded in manifests. Codex, Claude, and OpenCode use stdin; Droid, Grok, and Devin use private prompt files; Cursor, Oh My Pi, and Kimi use argv with public redaction placeholders.

See [security](../security.md) for boundary details.
