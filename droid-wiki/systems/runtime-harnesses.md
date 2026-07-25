# Runtime harnesses

Active contributors: Trey

## Purpose

Runtime harnesses turn a normalized Delegate request into a concrete child command for Cursor Agent, Factory Droid, OpenAI Codex, Claude Code, xAI Grok Build, Devin, OpenCode, Pi, Oh My Pi, or Kimi Code.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/request_build.py` | Per-engine request parts and argv builders. |
| `src/delegate_agent/harness_discovery.py` | Fingerprinted metadata probes, normalized catalogs, profile cache, and selector-drift checks. |
| `src/delegate_agent/model_discovery.py` | Config/cached/live/bundled model catalog projection. |
| `src/delegate_agent/setup_commands.py` | First-run discovery and no-clobber minimal config publication. |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and redaction placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared skill-review and completion-report prompt text. |
| `src/delegate_agent/config.py` | Provider config and policy validation. |
| `src/delegate_agent/reasoning.py` | Reasoning-effort capability resolution. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `Request` | `src/delegate_agent/request_models.py` | Full execution request passed to the runner. |
| `EngineBuildInput` | `src/delegate_agent/request_models.py` | Input for provider request builders. |
| `EngineRequestParts` | `src/delegate_agent/request_models.py` | Provider-specific argv and prompt-transport output. |
| `PROMPT_TRANSPORT_STDIN` | `src/delegate_agent/prompt_transport.py` | Prompt sent to child stdin. |
| `PROMPT_TRANSPORT_FILE` | `src/delegate_agent/prompt_transport.py` | Prompt written to a private file. |

## How it works

Cursor, Oh My Pi, and Kimi use argv prompt transport with public redaction
placeholders. Droid, Grok, and Devin use private prompt files. Codex, Claude,
OpenCode, and Pi use stdin. Safe/work flags are provider-specific: Codex safe
uses a read-only sandbox, Claude safe uses plan permission mode and limited
tools, OpenCode safe uses `--pure` plus injected read-only permissions, Droid
work uses `--skip-permissions-unsafe`, and Cursor work uses edit-enabling flags.

Before request construction, Delegate resolves the active auth profile once and
loads that profile's discovery snapshot. Config model choices remain
authoritative. Exact discovered model/effort evidence can validate a request or
supply a capability-only native default without forcing a model argv. If the
configured executable selector has drifted, only that harness record is ignored
until an explicit setup or capabilities refresh succeeds; ordinary launches do
not probe.

Discovery records use four evidence strengths: exact model menus, corroborated
Cursor `inferred-route` selectors, harness-wide/partial enums, and unknown. The
reasoning resolver preserves that distinction rather than treating every
advertised effort as transportable. Kimi metadata is the clearest example: its
provider catalog may mention effort, but Delegate exposes no Kimi effort flag.

## Integration points

[Safe and work modes](../features/safe-and-work-modes.md) explains mode semantics. [Reasoning effort](../features/reasoning-effort.md) explains effort validation and transport. [Tracked execution](tracked-execution.md) explains how the built request is launched and recorded.

## Entry points for modification

Add or change runtime flags in the relevant builder in
`src/delegate_agent/request_build.py`. Change a metadata command or parser in
`src/delegate_agent/harness_discovery.py`, and keep live model projection in
`src/delegate_agent/model_discovery.py`. Change prompt transport constants in
`src/delegate_agent/prompt_transport.py` and update `src/delegate_agent/runner.py`
if launch behavior changes.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/request_build.py` | Per-engine request parts and argv builders. |
| `src/delegate_agent/harness_discovery.py` | Harness detection, metadata parsers, and profile cache. |
| `src/delegate_agent/model_discovery.py` | User-facing model catalogs and one-off live probes. |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and redaction placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared skill-review and completion-report prompt text. |
| `src/delegate_agent/config.py` | Provider config and policy validation. |
| `src/delegate_agent/reasoning.py` | Reasoning-effort capability resolution. |

## Related pages

- [Prompt transport and redaction](../features/prompt-transport-and-redaction.md)
