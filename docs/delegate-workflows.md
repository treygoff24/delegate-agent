# Delegate Workflows

Delegate Workflows are Python workflow scripts executed by a detached supervisor on top of normal Delegate runs. A workflow gets its own registry at `.delegate/workflows/<wfId>/`; every `agent()` child is an ordinary run tagged with `--group <wfId>`, so `runs`, `snapshot`, `run-output`, and `cancel` keep working on child Runs.

## Script shape

```python
meta = {
    "name": "review-changes",
    "description": "Review changed files, then verify findings.",
    "defaults": {"engine": "codex", "mode": "safe"},
}

findings = pipeline(
    args["files"],
    lambda _prev, file, index: agent(f"Review {file}", phase="Review"),
    lambda finding, file, index: agent(f"Verify: {finding}", mode="call"),
)
return {"confirmed": [item for item in findings if item]}
```

`meta` must be a pure top-level dict literal. Top-level `return` becomes the workflow result in `result.json`. Injected globals are `agent`, `pipeline`, `parallel`, `phase`, `log`, `workflow`, `judges`, `args`, and `budget`.

## Core DSL

- `agent(prompt, engine=None, mode=None, model=None, effort=None, schema=None, label=None, phase=None, isolation=None, passthrough=False, timeout=None, retries=None)` launches a real Delegate child Run and returns parent-facing output, a validated schema object, or `None`.
- `pipeline(items, stage1, ...)` runs per-item stage chains with no inter-stage barrier. A throwing stage drops that item to `None` and skips later stages for that item.
- `parallel([lambda: ...])` is a barrier and preserves order; failures become `None` slots.
- `workflow(name_or_path, args=None, gate=False)` nests another workflow. Use `gate=True` or `gate="on-failure"` for approval checkpoints.
- `judges(prompt, schema, engines=[...])` runs one `call --read-only` judge lane per engine and returns the votes.

## Patterns

- Use `safe` or `work` lanes for agents that inspect or modify the tree.
- Use `call --read-only` lanes for judge/verify steps that should not touch files.
- Prefer `agent(phase="...")` under concurrency; global `phase()` is intentionally racy like Claude's workflow primitive.
- Use `--budget N` for run-count control. `budget.spent()` and `budget.remaining()` are available inside scripts.
- Use `schema=` when a stage must return structured JSON. Codex uses native `--output-schema`; other engines get schema instructions and validation retries.
- Use gates for human checkpoints: `workflow approve <wfId>` or `workflow run --resume <wfId>` releases a paused gate.

## CLI

```bash
python3 bin/delegate.py workflow check review.py
python3 bin/delegate.py --json workflow run review.py --args '{"files":["src/cli.py"]}' --budget 10
python3 bin/delegate.py workflow events wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow wait wf_0123abcdef45 --timeout 60
python3 bin/delegate.py workflow approve wf_0123abcdef45
python3 bin/delegate.py workflow save review.py --name review-changes
```

Saved workflows live only in the user library `~/.delegate/workflows/<name>.py`; project-level workflow libraries are intentionally deferred.
