# Delegate Agent

Delegate Agent is a local CLI that lets an orchestrating human or agent launch bounded work in another agent runtime while preserving a predictable command contract.

## Language

**Delegate Run**:
A single child agent invocation launched through the Delegate CLI. A Delegate Run may be read-only or file-editing, but it remains a bounded local process managed by the selected runtime.
_Avoid_: Job, background job, daemon task

**Run Registry**:
A local, append-friendly record of Delegate Runs created by the CLI so later commands can inspect launched work without reading raw runtime logs. The registry records metadata and normalized activity, but it does not make Delegate into a daemon or background scheduler.
_Avoid_: Database, queue, daemon state

**Snapshot**:
A concise, token-efficient view of one Delegate Run's identity, status, recent activity, and inferred current focus. A Snapshot is for monitoring and completion-report discovery, not for replaying the full session transcript.
_Avoid_: Transcript, raw log tail, full trace

**Completion Report**:
The final worker-authored report produced by a Harness at the end of a Delegate Run. Delegate captures this report from the final assistant output and stores it at a deterministic path in the Run Registry, rather than asking the worker to create its own file.
_Avoid_: Handoff, arbitrary report file

**Run Alias**:
A short project-local handle assigned to a Delegate Run for agent-friendly lookup, usually based on the Harness name. The first active or historical run for a Harness may use the base alias, such as `cursor`; later runs receive deterministic suffixes, such as `cursor-2`. Bare alias lookup is exact and aliases are not reused; "latest" lookup is an explicit query, not the default meaning of a bare alias.
_Avoid_: Nickname, display name, random ID

**Harness**:
The external agent runtime that Delegate launches, such as Droid, Cursor CLI, Factory, or Claude Code. Harnesses own their native execution semantics; Delegate normalizes only the launch contract and inspection surface.
_Avoid_: Provider, backend

**Harness Stream**:
The raw stdout/stderr emitted by a Harness while a Delegate Run is executing. A Harness Stream may be human text, JSON, JSONL, progress logs, tool events, or errors depending on the Harness and selected output mode.
_Avoid_: Parent response, final answer

**Parent-Facing Output**:
The bounded output Delegate returns to the caller that launched the Delegate Run. Parent-Facing Output should remain concise and stable even when Delegate captures a verbose Harness Stream internally.
_Avoid_: Raw stream, transcript dump

## Example dialogue

Developer: "I launched a Droid worker through Delegate. How do I check what it is doing without tailing JSONL?"

Operator: "Use the Run Alias or Delegate Run ID from launch output and ask for a Snapshot. The Snapshot reads the Run Registry and points to the Completion Report without exposing the full harness transcript."

## Orchestrating agents

- Prefer `delegate snapshot <alias>`, `delegate runs`, and `delegate run-output` over reading `.delegate/runs/*/stdout.log` or `events.jsonl` directly.
- Default launch output is bounded; use `--pass-through` only when raw harness streaming is required.
- Raw logs may be gzip-archived under `.delegate/archive/` after the configured age threshold; snapshots and index lookup still work.
- This repository is the development CLI; do not mutate the operator's live runtime at `~/.delegate` or installed shims unless explicitly asked to promote changes.
