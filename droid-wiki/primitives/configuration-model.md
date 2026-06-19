# Configuration model

Active contributors: Trey

## Purpose

The configuration model is the merged JSON object that controls runtime binaries, model aliases, defaults, policy, tracking, retention, isolation, and worktrees.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Config model implementation. |
| `config.example.json` | Public example. |
| `tests/test_delegate_validation.py` | Validation coverage. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `embedded defaults` | `src/delegate_agent/config.py` | Base config. |
| `user config` | `src/delegate_agent/config.py` | `~/.delegate/config.json`. |
| `workspace config` | `src/delegate_agent/config.py` | Workspace `.delegate/config.json`. |
| `DELEGATE_CONFIG` | `src/delegate_agent/config.py` | Environment override. |

## How it works

`load_config()` loads lower-precedence layers first and overlays higher-precedence layers. `validate_config()` rejects malformed sections and unsafe combinations. `src/delegate_agent/cli.py` exposes active config through `describe` and `models`.

## Integration points

See [configuration and policy](../features/configuration-and-policy.md) and [configuration reference](../reference/configuration.md).

## Entry points for modification

Change defaults, merge behavior, or validation in `src/delegate_agent/config.py`. Keep docs and validation tests aligned.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/config.py` | Config model implementation. |
| `config.example.json` | Public example. |
| `tests/test_delegate_validation.py` | Validation coverage. |
