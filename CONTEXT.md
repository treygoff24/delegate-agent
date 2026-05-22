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

**Skill Review Instruction**:
The mandatory prompt prefix Delegate injects into every Delegate Run before the operator prompt. It tells the child agent to review the full list of available skills at task start and load/apply relevant ones. This is an invariant prompt transform, separate from optional completion-report instructions.
_Avoid_: Parent-agent reminder, optional skill boilerplate

**Run Alias**:
A short project-local handle assigned to a Delegate Run for agent-friendly lookup, usually based on the Harness name. The first active or historical run for a Harness may use the base alias, such as `cursor`; later runs receive deterministic suffixes, such as `cursor-2`. Bare alias lookup is exact and aliases are not reused; "latest" lookup is an explicit query, not the default meaning of a bare alias.
_Avoid_: Nickname, display name, random ID

**Harness**:
The external agent runtime that Delegate launches, such as Droid, Cursor CLI, OpenAI Codex CLI, Factory, or Claude Code. Harnesses own their native execution semantics; Delegate normalizes only the launch contract and inspection surface.
_Avoid_: Provider, backend

**Harness Stream**:
The raw stdout/stderr emitted by a Harness while a Delegate Run is executing. A Harness Stream may be human text, JSON, JSONL, progress logs, tool events, or errors depending on the Harness and selected output mode.
_Avoid_: Parent response, final answer

**Parent-Facing Output**:
The bounded output Delegate returns to the caller that launched the Delegate Run. Parent-Facing Output should remain concise and stable even when Delegate captures a verbose Harness Stream internally.
_Avoid_: Raw stream, transcript dump

**Policy Profile**:
A named preset in config (`policy.profile`: `safe`, `trusted-hooks`, `external-sandbox`, or `custom`) that expands default mode-policy fields before explicit overrides are applied.
_Avoid_: Sandbox mode, harness profile

**Effective Policy**:
The merged boolean policy Delegate computes for a specific harness and mode (`safe` or `work`) after profile defaults, explicit `policy.safe` / `policy.work`, and optional `policy.harness.<engine>.<mode>` overrides. Harness argv builders consume only the fields they support.
_Avoid_: Runtime config, argv list

**Harness Policy Override**:
An explicit per-harness, per-mode policy block under `policy.harness.<engine>.<mode>` that wins over profile defaults and global mode policy for that harness only.
_Avoid_: Engine config, profile

**Dangerous Bypass**:
The highest-risk Codex policy tier: `bypassApprovalsAndSandbox` maps to `--dangerously-bypass-approvals-and-sandbox`, disabling Codex approvals and sandboxing. Intended only when Delegate already runs inside an externally isolated environment.
_Avoid_: Unsafe mode, force flag

**Hook Trust Bypass**:
A middle-tier Codex policy control: `bypassHookTrust` maps to `--dangerously-bypass-hook-trust` without disabling sandboxing. The `trusted-hooks` profile enables it for work mode by default.
_Avoid_: Trusted mode, MCP approve

**Network Access**:
Policy control for subprocess/network egress inside a Codex sandbox (for example package installs). For work mode with `workspace-write`, `networkAccess: true` emits `-c sandbox_workspace_write.network_access=true`. Distinct from native web search.
_Avoid_: Internet mode, online

**Native Web Search**:
Policy control for Codex's built-in web search tool (`webSearch: true` adds global `--search`). Separate from sandbox subprocess network access; work mode defaults network on and web search off.
_Avoid_: Search mode, browsing

## Example dialogue

Developer: "I launched a Droid worker through Delegate. How do I check what it is doing without tailing JSONL?"

Operator: "Use the Run Alias or Delegate Run ID from launch output and ask for a Snapshot. The Snapshot reads the Run Registry and points to the Completion Report without exposing the full harness transcript."

## Orchestrating agents

- Prefer `delegate snapshot <alias>`, `delegate runs`, and `delegate run-output` over reading `.delegate/runs/*/stdout.log` or `events.jsonl` directly.
- Default launch output is bounded; use `--pass-through` only when raw harness streaming is required.
- Do not manually repeat skill-loading boilerplate for ordinary runs; Delegate injects the mandatory skill-review instruction for every launched child prompt.
- Raw logs may be gzip-archived under `.delegate/archive/` after the configured age threshold; snapshots and index lookup still work.
- This repository is the development CLI; do not mutate the operator's live runtime at `~/.delegate` or installed shims unless explicitly asked to promote changes.
