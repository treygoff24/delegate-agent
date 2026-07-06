# Runtime harnesses

Active contributors: Trey

## Purpose

Runtime harnesses turn a normalized Delegate request into a concrete child command for Cursor Agent, Factory Droid, OpenAI Codex, Claude Code, xAI Grok Build, or Kimi Code.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Per-engine request parts and argv builders. |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and redaction placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared skill-review and completion-report prompt text. |
| `src/delegate_agent/config.py` | Provider config and policy validation. |
| `src/delegate_agent/reasoning.py` | Reasoning-effort capability resolution. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `Request` | `src/delegate_agent/cli.py` | Full execution request passed to the runner. |
| `EngineBuildInput` | `src/delegate_agent/cli.py` | Input for provider request builders. |
| `EngineRequestParts` | `src/delegate_agent/cli.py` | Provider-specific argv and prompt-transport output. |
| `PROMPT_TRANSPORT_STDIN` | `src/delegate_agent/prompt_transport.py` | Prompt sent to child stdin. |
| `PROMPT_TRANSPORT_FILE` | `src/delegate_agent/prompt_transport.py` | Prompt written to a private file. |

## How it works

Cursor and Kimi use argv prompt transport with public redaction placeholders. Droid and Grok use private prompt files. Codex and Claude use stdin. Safe/work flags are provider-specific: Codex safe uses a read-only sandbox, Claude safe uses plan permission mode and limited tools, Grok safe uses read-only sandbox and permission controls, Droid work uses `--skip-permissions-unsafe`, and Cursor work uses edit-enabling flags.

## Integration points

[Safe and work modes](../features/safe-and-work-modes.md) explains mode semantics. [Reasoning effort](../features/reasoning-effort.md) explains effort validation and transport. [Tracked execution](tracked-execution.md) explains how the built request is launched and recorded.

## Entry points for modification

Add or change runtime flags in the relevant `build_*_argv()` function in `src/delegate_agent/cli.py`. Change prompt transport constants in `src/delegate_agent/prompt_transport.py` and update `src/delegate_agent/runner.py` if launch behavior changes.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/cli.py` | Per-engine request parts and argv builders. |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and redaction placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared skill-review and completion-report prompt text. |
| `src/delegate_agent/config.py` | Provider config and policy validation. |
| `src/delegate_agent/reasoning.py` | Reasoning-effort capability resolution. |

## Related pages

- [Prompt transport and redaction](../features/prompt-transport-and-redaction.md)
