# Reasoning effort

Active contributors: Trey

## Purpose

Reasoning effort lets callers request provider-specific model thinking depth without changing safety settings.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capabilities command. |
| `src/delegate_agent/cli.py` | Effort resolution and provider emission. |
| `src/delegate_agent/config.py` | Effort config validation. |
| `tests/test_reasoning_capabilities.py` | Reasoning tests. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `ReasoningCapability` | `src/delegate_agent/reasoning.py` | Resolved support for one harness, model, and effort. |
| `BUNDLED_REASONING_CAPABILITIES` | `src/delegate_agent/reasoning.py` | Fallback declarations. |
| `resolve_reasoning_capability()` | `src/delegate_agent/reasoning.py` | Validates Codex, Droid, and Cursor support. |
| `resolve_claude_native_effort()` | `src/delegate_agent/reasoning.py` | Validates Claude native labels. |

## How it works

Codex emits a config override, Droid emits `--reasoning-effort`, Cursor maps effort to configured models, Claude emits native `--effort`, OpenCode passes effort through as `--variant` without model validation, and Kimi is unsupported in v1. Config declarations win over cache declarations, which win over bundled fallback data.

## Integration points

`delegate --json capabilities` reports support. Dry-run payloads and snapshots include requested and resolved effort metadata. Provider config is explained in [configuration and policy](configuration-and-policy.md).

## Entry points for modification

Update bundled declarations and validation in `src/delegate_agent/reasoning.py`, provider emission in `src/delegate_agent/cli.py`, and validation in `src/delegate_agent/config.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capabilities command. |
| `src/delegate_agent/cli.py` | Effort resolution and provider emission. |
| `src/delegate_agent/config.py` | Effort config validation. |
| `tests/test_reasoning_capabilities.py` | Reasoning tests. |
