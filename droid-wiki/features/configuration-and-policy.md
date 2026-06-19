# Configuration and policy

Active contributors: Trey

## Purpose

Delegate uses layered JSON configuration to control provider settings, model aliases, default reasoning effort, tracking, retention, isolation, worktrees, and runtime policy.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Config loading, validation, policy, and isolation. |
| `config.example.json` | Public example config. |
| `docs/configuration.md` | User-facing config reference. |
| `src/delegate_agent/cli.py` | Config observability and runtime use. |
| `tests/test_delegate_validation.py` | Validation coverage. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `_EMBEDDED_DEFAULT_CONFIG` | `src/delegate_agent/config.py` | Built-in defaults. |
| `load_config()` | `src/delegate_agent/config.py` | Loads and merges config layers. |
| `validate_config()` | `src/delegate_agent/config.py` | Validates provider and policy sections. |
| `effective_policy()` | `src/delegate_agent/config.py` | Merges profile, mode, and harness policy. |

## How it works

Config precedence is embedded defaults, user config, workspace config, `DELEGATE_CONFIG`, then internal overrides. Provider sections control runtime defaults. Policy uses a profile plus mode and harness overrides. Safe-mode bypass flags are rejected.

## Integration points

Runtime builders consume provider sections in [runtime harnesses](../systems/runtime-harnesses.md). Safe/work boundaries are in [safe and work modes](safe-and-work-modes.md).

## Entry points for modification

Change defaults, loading, validation, and policy semantics in `src/delegate_agent/config.py`. Keep `config.example.json`, `docs/configuration.md`, and validation tests aligned.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Config loading, validation, policy, and isolation. |
| `config.example.json` | Public example config. |
| `docs/configuration.md` | User-facing config reference. |
| `src/delegate_agent/cli.py` | Config observability and runtime use. |
| `tests/test_delegate_validation.py` | Validation coverage. |
