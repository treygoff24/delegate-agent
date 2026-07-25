# Reasoning capability

Active contributors: Trey

## Purpose

A reasoning capability records whether a runtime/model pair supports a requested effort label and how Delegate should transport that request.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capability command output. |
| `src/delegate_agent/harness_discovery.py` | Profile-scoped discovered model and effort evidence. |
| `tests/test_reasoning_capabilities.py` | Capability tests. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `ReasoningCapability` | `src/delegate_agent/reasoning.py` | Harness, model, effort, supported efforts, default, transport, and source. |
| `ReasoningDeclaration` | `src/delegate_agent/reasoning.py` | Config/cache/bundled declaration. |
| `TRANSPORT_CODEX_CONFIG` | `src/delegate_agent/reasoning.py` | Codex config override transport. |
| `TRANSPORT_CLAUDE_EFFORT_FLAG` | `src/delegate_agent/reasoning.py` | Claude native effort transport. |

## How it works

Exact model declarations resolve from manual config, profile discovery, the
read-only legacy workspace cache, then bundled fallback. Cursor uses explicit
`reasoningEffortModels` or corroborated discovered route families. Claude and Pi
can use harness-wide evidence without claiming model-exact support. OpenCode and
Oh My Pi validate exact discovered model menus when present and otherwise keep
their documented compatibility paths. Grok combines exact model declarations
with a harness-wide native transport fallback. Kimi may advertise effort
metadata, but Kimi and Devin expose no Delegate effort transport.

## Integration points

See [reasoning effort](../features/reasoning-effort.md) for user-facing behavior.

## Entry points for modification

Change declarations and validation in `src/delegate_agent/reasoning.py`, command output in `src/delegate_agent/capability_commands.py`, and tests in `tests/test_reasoning_capabilities.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capability command output. |
| `src/delegate_agent/harness_discovery.py` | Normalized discovery evidence and cache. |
| `tests/test_reasoning_capabilities.py` | Capability tests. |
