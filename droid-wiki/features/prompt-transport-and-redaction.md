# Prompt transport and redaction

Active contributors: Trey

## Purpose

Prompt transport controls how Delegate sends task text to child runtimes. Redaction controls how Delegate displays potentially sensitive data in public argv, snapshots, and run-output.

## Directory layout

| File | Purpose |
| --- | --- |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared prompt additions. |
| `src/delegate_agent/cli.py` | Prompt resolution and runtime transport selection. |
| `src/delegate_agent/runner.py` | Stdin and prompt-file delivery. |
| `src/delegate_agent/redaction.py` | Display redaction. |

## Key abstractions

| Name | File | Role |
| --- | --- | --- |
| `PROMPT_TRANSPORT_ARGV` | `src/delegate_agent/prompt_transport.py` | Prompt is in child argv. |
| `PROMPT_TRANSPORT_FILE` | `src/delegate_agent/prompt_transport.py` | Prompt is written to a private file. |
| `PROMPT_TRANSPORT_STDIN` | `src/delegate_agent/prompt_transport.py` | Prompt is sent to child stdin. |
| `redact_text()` | `src/delegate_agent/redaction.py` | Masks common secret-like strings. |

## How it works

Cursor and Kimi use argv prompt transport with redacted public argv. Droid and Grok use private prompt files. Codex and Claude use stdin. Display redaction masks common secret shapes in snapshots and run-output by default.

## Integration points

Runtime-specific transport is built in [runtime harnesses](../systems/runtime-harnesses.md). Output redaction is used by [run inspection](run-inspection.md).

## Entry points for modification

Change transport labels in `src/delegate_agent/prompt_transport.py`, provider transport use in `src/delegate_agent/cli.py`, launch handling in `src/delegate_agent/runner.py`, and masking rules in `src/delegate_agent/redaction.py`.

## Key source files

| File | Purpose |
| --- | --- |
| `src/delegate_agent/prompt_transport.py` | Prompt transport constants and placeholders. |
| `src/delegate_agent/prompt_instructions.py` | Shared prompt additions. |
| `src/delegate_agent/cli.py` | Prompt resolution and runtime transport selection. |
| `src/delegate_agent/runner.py` | Stdin and prompt-file delivery. |
| `src/delegate_agent/redaction.py` | Display redaction. |
