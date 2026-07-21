# Reasoning effort

Active contributors: Trey

## Purpose

Reasoning effort lets callers request provider-specific model thinking depth without changing safety settings.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capabilities command. |
| `src/delegate_agent/request_build.py` | Effort resolution and provider emission. |
| `src/delegate_agent/harness_discovery.py` | Profile-scoped discovered evidence. |
| `src/delegate_agent/config.py` | Effort config validation. |
| `tests/test_reasoning_capabilities.py` | Reasoning tests. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `ReasoningCapability` | `src/delegate_agent/reasoning.py` | Resolved support for one harness, model, and effort. |
| `BUNDLED_REASONING_CAPABILITIES` | `src/delegate_agent/reasoning.py` | Fallback declarations. |
| `resolve_reasoning_capability()` | `src/delegate_agent/reasoning.py` | Resolves exact, partial, and fallback evidence. |

## How it works

Codex emits a config override, Droid emits `--reasoning-effort`, Cursor selects
a configured or corroborated discovered model route, Claude and Grok emit
native `--effort`, OpenCode emits `--variant`, and Pi/Oh My Pi emit
`--thinking`. Exact model menus fail closed; harness-partial evidence remains
labeled. Manual config wins over profile discovery, which wins over the legacy
workspace cache and bundled fallback. Kimi and Devin expose no effort transport.

## Integration points

`delegate --json capabilities` reports support. Dry-run payloads and snapshots include requested and resolved effort metadata. Provider config is explained in [configuration and policy](configuration-and-policy.md).

## Entry points for modification

Update bundled declarations and validation in `src/delegate_agent/reasoning.py`,
provider emission in `src/delegate_agent/request_build.py`, discovery adapters
in `src/delegate_agent/harness_discovery.py`, and config validation in
`src/delegate_agent/config.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/reasoning.py` | Capability model and validation. |
| `src/delegate_agent/capability_commands.py` | Capabilities command. |
| `src/delegate_agent/request_build.py` | Effort resolution and provider emission. |
| `src/delegate_agent/harness_discovery.py` | Profile discovery and exact/partial evidence. |
| `src/delegate_agent/config.py` | Effort config validation. |
| `tests/test_reasoning_capabilities.py` | Reasoning tests. |
