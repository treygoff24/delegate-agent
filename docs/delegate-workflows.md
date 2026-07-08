# Delegate Workflows

Delegate Workflows are Python workflow scripts executed by a detached supervisor
on top of normal Delegate runs. A workflow gets its own registry at
`.delegate/workflows/<wfId>/`; every `agent()` child is an ordinary run tagged
with `--group <wfId>`, so `runs`, `snapshot`, `run-output`, `wait`, and
`cancel` keep working on child runs.

Workflow registries use this file set as needed:

- `script.py`: pinned workflow source for this run.
- `args.json`: launch arguments supplied with `--args`.
- `journal.jsonl`: append-only workflow events.
- `status.json`: current supervisor/status snapshot.
- `result.json`: final workflow result, present only after success.
- `approval.json`: gate approval state, present after `workflow approve`.
- `workflow.lock`: process lock held while a supervisor is active.

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

`meta` must be a pure top-level dict literal. Top-level `return` becomes the
workflow result in `result.json`. Injected globals are `agent`, `pipeline`,
`parallel`, `phase`, `log`, `workflow`, `judges`, `args`, and `budget`.

## Core DSL

- `agent(prompt, engine=None, mode=None, model=None, effort=None, schema=None, label=None, phase=None, isolation=None, passthrough=False, timeout=None, retries=None)` launches a real Delegate child run and returns parent-facing output, a validated schema object, or `None`.
- `pipeline(items, stage1, ...)` runs per-item stage chains with no inter-stage barrier. A throwing stage drops that item to `None` and skips later stages for that item.
- `parallel([lambda: ...])` is a barrier and preserves order. Ordinary item failures become `None` slots; gate checkpoints propagate to the supervisor.
- `phase(title)` emits a progress event.
- `log(message)` emits a JSON-safe log event.
- `workflow(name_or_path, args=None, gate=False)` nests another workflow. Use `gate=True` or `gate="on-failure"` for approval checkpoints.
- `judges(prompt, schema, engines=[...])` runs one `call --read-only` judge lane per engine and returns the votes.

## Gates and resume

A workflow gate checkpoints the whole workflow tree, not just the nested child
that reached the gate. When a gate closes, the runtime stops admitting new
`agent()` calls tree-wide, drains already in-flight agents to the journal, emits
the gate event, writes `status: "paused"`, and exits the supervisor without
writing `result.json`. Resume or approval releases the gate:

```bash
python3 bin/delegate.py workflow approve wf_0123abcdef45
python3 bin/delegate.py workflow run --resume wf_0123abcdef45
```

This preserves in-flight sibling results for replay while preventing unrelated
siblings from starting after a human checkpoint has requested control.

## Limits

Workflow scripts are intentionally capped:

- Script size: 512 KiB.
- Nesting depth: 3 workflow levels.
- Lifetime `agent()` calls per workflow tree: 1000.
- `pipeline()`/`parallel()` item count: 4096.

These caps protect local machines from accidental fan-out and keep resume state
bounded. The caps are hard validation/runtime errors, not warnings.

## Config

The top-level `workflows` config block controls concurrency and schema retries:

```json
{
  "workflows": {
    "engineCaps": {"codex": 4, "claude": 2},
    "itemThreads": 64,
    "structuredOutputRetries": 2
  }
}
```

- `engineCaps`: optional per-engine concurrent child-run caps. Keys are Delegate engine names; values are positive integers. Engines without a cap are unconstrained by this setting.
- `itemThreads`: maximum concurrent workflow item worker threads across `pipeline()` and `parallel()`. Positive integers override the default; `0` or a missing value falls back to the default.
- `structuredOutputRetries`: retry count for `agent(schema=...)` validation failures. Retries include the previous invalid output and validation error as correction context.

## Nested workflow references

Explicit CLI script paths and saved workflow names are accepted at launch. Nested
`workflow()` references are narrower: they may name a saved user-library script
under `~/.delegate/workflows/`, use an absolute path inside that same saved
library, or use a path inside the parent workflow's pinned script directory.
Other filesystem paths are rejected at resolution time.

Saved workflows live only in the user library
`~/.delegate/workflows/<name>.py`; project-level workflow libraries are
intentionally deferred.

## Patterns

- Use `safe` or `work` lanes for agents that inspect or modify the tree.
- Use `call --read-only` lanes for judge/verify steps that should not touch files.
- `call` is the default mode for `judges()`, and workflow call-mode children are read-only by default.
- `passthrough=True` is incompatible with `schema=` and with `mode="call"`; slash pass-through needs a work lane or an argv-enforced-safe lane.
- Prefer `agent(phase="...")` under concurrency; global `phase()` is intentionally racy like Claude's workflow primitive.
- Use `--budget N` for run-count control. `budget.spent()` and `budget.remaining()` are available inside scripts. Dry-runs simulate budget ticks but do not consume real budget.
- Use `schema=` when a stage must return structured JSON. Codex uses native `--output-schema`; other engines get schema instructions and validation retries.

## CLI

```bash
python3 bin/delegate.py workflow check review.py
python3 bin/delegate.py --json workflow run review.py --args '{"files":["src/cli.py"]}' --budget 10
python3 bin/delegate.py workflow events wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow watch wf_0123abcdef45 --since 12
python3 bin/delegate.py workflow wait wf_0123abcdef45 --timeout 60
python3 bin/delegate.py workflow approve wf_0123abcdef45
python3 bin/delegate.py workflow kill wf_0123abcdef45
python3 bin/delegate.py workflow save review.py --name review-changes
```
