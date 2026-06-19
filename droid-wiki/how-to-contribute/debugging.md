# Debugging

Delegate is easiest to debug by using dry-run and JSON observability first, then inspecting `.delegate/` run records if a real child run was launched.

## Start with dry-run

```bash
python3 bin/delegate.py --json dry-run codex safe "Review only."
```

The payload shows prompt transport, public argv, model, reasoning-effort metadata, workspace kind, and isolation lifecycle.

## Inspect config and capabilities

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json models
python3 bin/delegate.py --json capabilities
```

## Inspect a tracked run

```bash
python3 bin/delegate.py runs --recent
python3 bin/delegate.py snapshot <alias-or-runId>
python3 bin/delegate.py run-output <alias-or-runId>
```

Common failure areas are provider binary config in `config.example.json`, Droid aliases, unsupported reasoning effort in `src/delegate_agent/reasoning.py`, safe isolation validation in `src/delegate_agent/config.py`, and persistent worktree preflight in `src/delegate_agent/worktree_execution.py`.

See [run inspection](../features/run-inspection.md).
