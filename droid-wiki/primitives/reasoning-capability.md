# Reasoning capability

Active contributors: Trey

## Purpose

A reasoning capability records whether a runtime/model pair supports a requested effort label and how Delegate should transport that request.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capability command output. |
| `tests/test_reasoning_capabilities.py` | Capability tests. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `ReasoningCapability` | `src/delegate_agent/reasoning.py` | Harness, model, effort, supported efforts, default, transport, and source. |
| `ReasoningDeclaration` | `src/delegate_agent/reasoning.py` | Config/cache/bundled declaration. |
| `TRANSPORT_CODEX_CONFIG` | `src/delegate_agent/reasoning.py` | Codex config override transport. |
| `TRANSPORT_CLAUDE_EFFORT_FLAG` | `src/delegate_agent/reasoning.py` | Claude native effort transport. |

## How it works

For Codex and Droid, Delegate looks up declarations in config, then cache, then bundled fallback. Cursor uses `cursor.reasoningEffortModels`. Claude and Grok use static native labels. Kimi is unsupported in v1.

## Integration points

See [reasoning effort](../features/reasoning-effort.md) for user-facing behavior.

## Entry points for modification

Change declarations and validation in `src/delegate_agent/reasoning.py`, command output in `src/delegate_agent/capability_commands.py`, and tests in `tests/test_reasoning_capabilities.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capability command output. |
| `tests/test_reasoning_capabilities.py` | Capability tests. |
