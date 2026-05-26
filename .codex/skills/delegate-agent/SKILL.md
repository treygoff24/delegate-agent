---
name: delegate-agent
description: Use the local delegate CLI to hand bounded execution tasks to Cursor Composer 2.5, Droid BYOK models, or OpenAI Codex CLI. Cursor/Codex safe run isolated read-only review in a detached workspace copy; work mode can edit. Git repos or ordinary workspaces. Worktree isolation keeps edit-capable agent runs separate from the source checkout.
---

# Delegate Agent

Use `delegate` when Trey asks you to use Cursor Composer, Droid, BYOK models, OpenAI Codex CLI, cheap execution agents, or scoped execution workers. Delegate works in both traditional Git repos and ordinary directories such as Dropbox document/policy workspaces.

## Model aliases and routing guidance

Delegate model availability is config-driven. Before relying on an alias, prefer checking:

- `delegate --json models` — list available models/config per engine, including Codex binary/defaultModel/profile
- `delegate --json describe` — full config dump with mode mappings, safe notes, supported policy fields, and effective Codex policy

### Cursor

- `delegate cursor work` — Cursor Composer 2.5 implementation in the real workspace.
  - Strengths: fastest and cheapest broad implementation lane; strong for backend cleanups, SQL migrations, mechanical touch-list work, UI cleanups, and repo-wide edits.
  - Weaknesses/watchouts: can add defensive-noise code or touch adjacent files; always inspect diffs after work mode.
  - Runtime notes: Cursor CLI is configured for unrestricted execution; Delegate passes `--trust --approve-mcps --force` in work mode. Global Cursor MCPs/skills are available when configured.

- `delegate cursor safe` — Cursor Composer read-only review/investigation in an isolated temporary workspace copy.
  - Strengths: good for diff/regression review, architecture investigation, and "tell me what changed" tasks without touching the source tree.
  - Weaknesses/watchouts: safe mode is for review/investigation, not implementation.

### Droid / BYOK aliases

- `delegate droid glm ...` — OpenCode Go GLM 5.1.
  - Strengths: high-quality implementation and review-fix work; good at clean abstractions and data-driven changes; has historically been more likely to add tests proactively.
  - Weaknesses/watchouts: often slow on medium lanes; use when quality is worth wall-clock time.

- `delegate droid kimi ...` — OpenCode Go Kimi K2.6.
  - Strengths: useful as an alternate implementation/review lane when you want model diversity.
  - Weaknesses/watchouts: less calibrated in this setup than the most-used aliases; review diffs carefully.

- `delegate droid qwen ...` — OpenRouter Qwen 3.7 Max.
  - Strengths: strong general coding/reasoning lane; useful for implementation, review, and second-opinion work where Qwen-style reasoning is desired.
  - Weaknesses/watchouts: OpenRouter routing/cost/latency can vary; review outputs carefully, especially for large edits.

- `delegate droid minimax ...` — OpenCode Go MiniMax M2.7.
  - Strengths: cheap read-only or lightweight investigation option; useful for quick extra coverage.
  - Weaknesses/watchouts: treat as backup-tier for substantial implementation; inspect conclusions and diffs carefully.

- `delegate droid mimo ...` — OpenCode Go MiMo V2.5.
  - Strengths: useful as an additional implementation/review lane for diversity.
  - Weaknesses/watchouts: less calibrated; avoid assigning high-risk solo ownership without review.

- `delegate droid "mimo pro" ...` — OpenCode Go MiMo V2.5 Pro.
  - Strengths: stronger MiMo option for implementation/review lanes where you want to compare against other models.
  - Weaknesses/watchouts: less proven than Cursor/GLM/Grok/Gemini in this setup; inspect diffs carefully.

- `delegate droid grok ...` — xAI Grok 4.3.
  - Strengths: good for classifier/schema work, typed refactors, `satisfies` pins, and explicit extraction/refactor tasks.
  - Weaknesses/watchouts: can be expensive on small fixes due to large input/cache-read consumption; has had spotty a11y lint readiness.

- `delegate droid gemini ...` — Gemini 3.5 Flash.
  - Strengths: useful across implementation, cleanup, review-fix, and investigation tasks; good for model-diverse coverage.
  - Weaknesses/watchouts: review carefully for overconfident or shallow fixes on complex repo-specific behavior.

- `delegate droid "deepseek v4 pro" ...` — OpenRouter DeepSeek V4 Pro.
  - Strengths: current Pareto-frontier default for cost per completed task: very strong coding/reasoning quality at extremely low cost. Use for complex implementation, bug hunts, review-fix loops, and tasks where a little extra intelligence is worth the slower runtime versus Flash.
  - Weaknesses/watchouts: slower than Flash; still use bounded prompts and inspect diffs after work mode.

- `delegate droid "deepseek v4 flash" ...` — OpenRouter DeepSeek V4 Flash.
  - Strengths: current Pareto-frontier fast lane: much faster than Pro with only a modest intelligence tradeoff, while retaining extremely low cost. Use for investigation, straightforward implementation, cleanup, review-fix, and parallel coverage where speed matters.
  - Weaknesses/watchouts: slightly less capable than Pro on deep architectural reasoning or ambiguous multi-file changes; escalate to Pro when the task needs more careful reasoning.

### Codex harness

- `delegate codex safe` — isolated read-only Codex review/investigation.
  - Strengths: Codex-native review without touching the source tree; same source-workspace protection as Cursor safe, plus Codex `--sandbox read-only`.
  - Weaknesses/watchouts: review/investigation only; not for edits.

- `delegate codex work` — Codex implementation in the real workspace.
  - Strengths: useful when Trey explicitly wants Codex as the delegated implementation lane.
  - Weaknesses/watchouts: runs in the real workspace; inspect diffs afterward.
  - Runtime notes: defaults to `--ask-for-approval never exec --sandbox workspace-write` with sandbox network enabled by policy.
  - Model notes: Delegate does not expose a fixed Codex model alias list in v1. Use `delegate --json models` / `delegate --json describe` to inspect `codex.defaultModel`, `codex.profile`, and effective policy.

Always review outputs carefully when using backup-tier or less-calibrated models. In Git workspaces, review diffs; outside Git, manually inspect changed files or use the host's version history/backups.

## Mode selection

- `<model> work` — file-writing implementation or document/workspace editing in the **real** workspace. Default for most bounded lanes.
- `<model> safe` — read-only analysis in the **real** workspace (Droid) or an **isolated temporary copy** (Cursor). Use for investigation, audits, code review, and "tell me what this does" tasks.
- `delegate cursor safe` — Composer read-only review; prefer when you want Cursor for diff/regression review without touching the tree.
- `delegate codex safe` — Codex read-only review in an isolated temporary copy; use for Codex-native review without touching the tree.
- `delegate codex work` — Codex implementation in the real workspace; inspect diffs afterward.
- `delegate droid minimax safe` — cheapest read-only option among current Droid aliases.

## Safety model

| Command | Where it runs | Power flags |
| --- | --- | --- |
| `delegate cursor safe` | Isolated temp copy (worktree or dir copy) | `-p --trust` only; **no** plan/ask/force/MCP auto-approve |
| `delegate cursor work` | Real workspace | `--approve-mcps --force` |
| `delegate droid * safe` | Real workspace | Droid default read-only; **no** `--auto`, `--use-spec`, or `--skip-permissions-unsafe` |
| `delegate droid * work` | Real workspace | `--skip-permissions-unsafe` |
| `delegate codex safe` | Isolated temp copy (worktree or dir copy) | `--ask-for-approval never exec --sandbox read-only` |
| `delegate codex work` | Real workspace | `--ask-for-approval never exec --sandbox workspace-write`; network config when policy allows |

### Cursor safe — hard boundary vs defense-in-depth

**Hard boundary (trust this):** delegate runs default Cursor Agent against an isolated temporary workspace, not the source `--cwd` tree. Git: detached worktree synced from tracked + untracked snapshot; non-Git: directory copy. The original workspace is not modified.

**Defense-in-depth (isolated copy only, not a substitute for isolation):**
- Prepends read-only review instructions to the prompt.
- Writes `.cursor/cli.json` in the isolated copy (`Read(**)` + read-oriented shell; denies writes/destructive shell and some secret paths).

**Not used:** `--mode=plan`, `--mode=ask`, `--force`, `--approve-mcps`.

**JSON output:** `cwd` = source workspace, `executionCwd` = isolated copy, `isolatedWorkspace: true`.

### Cursor work

Runs in the resolved real workspace. Always review diffs (Git) or changed files (non-Git) after completion.

### Droid safe / work

Both run in the real workspace. Safe avoids auto/spec/unsafe flags so prompts stay investigative, not implementation-spec.

### Codex safe / work

`delegate codex safe` uses the same hard isolation boundary as Cursor safe: Delegate rewrites `--cd` to a temporary detached git worktree or directory copy, and JSON output reports `cwd` (source), `executionCwd` (isolated copy), and `isolatedWorkspace: true`. Safe mode is locked to `--sandbox read-only`; there is no `codex.safeSandbox`.

`delegate codex work` runs in the real workspace and defaults to `--sandbox workspace-write` with `sandbox_workspace_write.network_access=true` when effective policy enables `networkAccess` (the default for work). Policy profiles can enable hook-trust bypass (`trusted-hooks`) or full dangerous bypass (`external-sandbox`), but treat `external-sandbox` as high risk and only use it when Delegate is already externally sandboxed.

Codex policy controls are visible with `delegate --json describe`; dry-run a launch with `delegate --json dry-run codex work "..."`.

## Isolation override

Add `--isolation worktree` to keep edit-capable agent runs separate from the
source checkout. Creates a persistent Git worktree under the Delegate data home.

```bash
delegate --isolation worktree cursor work "Implement the fix."
delegate --isolation worktree codex safe "Review in a temp worktree."
```

When a run is worktree-isolated, it returns `branch` + `executionCwd` in its
completion output. The child agent receives a prompt note explaining that it must
work in the isolated worktree and must not touch the source checkout.

See the README and AGENTS.md for the full isolation matrix, worktree lifecycle,
and management command reference.

## Worktree lifecycle for orchestrators

When you spawn a persistent worktree run (`--isolation worktree` + `work` mode):

1. The run returns `branch` and `executionCwd` in completion output.
2. The child agent works inside an isolated worktree. Changes to the source
   checkout are prevented by Git worktree isolation.
3. After the child exits, the worktree and branch are preserved.
4. Use `delegate worktree show <alias>` to inspect state (porcelain status,
   ahead/behind counts).
5. Use `delegate worktree remove <alias>` to clean up. The default refuses
   dirty worktrees and unmerged branches — pass `--discard-uncommitted`
   (data-loss) or `--force-branch` explicitly.
6. Use `delegate worktree prune --merged` for bulk cleanup of merged worktrees.
7. Do **not** delete `~/.delegate/worktrees/` paths directly — this orphans
   registry entries and breaks snapshot/inspection commands.
8. Do **not** manually run `git worktree remove` or `git branch -D` on
   Delegate-managed worktrees.

## Rules

- Keep prompts bounded: task, scope (owned files), verification/review steps, report format.
- Use `--prompt-file` or `delegate --json run --input-json` for long prompts.
- Run from the target workspace, or pass `--cwd` before the subcommand. Inside Git, Delegate resolves to the repo root; outside Git, it uses the directory directly.
- Normal tracked runs return bounded output. After launch, inspect with `delegate snapshot <alias>`, `delegate runs`, and `delegate run-output`; do not pipe launches through `tail` just to suppress output.
- Always review after `work`. In Git workspaces, inspect diffs. In non-Git workspaces, manually inspect changed files and rely on Dropbox/version-history/backups where available. Models occasionally add defensive code or touch adjacent files outside the owned set.
- Do not use delegate for production pushes/deploys unless Trey explicitly asks.
- If Cursor reports auth/model/MCP readiness errors, ask Trey to authenticate Cursor Agent, run the relevant `agent mcp login <name>` flow, or provide documented `CURSOR_API_KEY` readiness.
- For parallel droid runs in the same workspace, the eval harness (`scripts/delegate-eval-runner.ts` in projects that have it) pre-snapshots existing session UUIDs to avoid token-capture collisions. If you build a similar wrapper elsewhere, replicate that pattern.

## Discovery

- `delegate --json models` — list available models/config per engine, including Codex binary/defaultModel/profile
- `delegate --json describe` — full config dump with mode mappings, safe notes, supported policy fields, and effective Codex policy
- `delegate agent-help` — verbose usage
