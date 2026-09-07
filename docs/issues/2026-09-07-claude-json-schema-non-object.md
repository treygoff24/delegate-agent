# Claude lanes fail preflight when a workflow schema is not a top-level object

**Status:** open · **Severity:** major (every Claude reviewer lane in a planc workflow exhausts instantly) · **Found:** 2026-09-07, fin-model `norem-v1` run `wf_8f73ab8b6bd8` · **delegate-agent:** 0.30.0 (runtime pin `1521f3024c718df6`) · **Claude Code:** 2.1.263

## Observed

Every `review:*` stage routed to `claude` died in under two seconds, twice per lane (structured retry, then exhausted), while the identical stage on `codex` ran normally. Child stdout:

```
API Error: 400 tools.8.custom.input_schema.type: Input should be 'object'
```

Journal (`.delegate/workflows/wf_8f73ab8b6bd8/journal.jsonl`, seq 15–19): `agent_structured_retry` ×2 with `child attempt nonzero_exit: Expecting value: line 1 column 1 (char 0)`, then `agent_structured_exhausted`, then `agent_finished result=null exhausted=true`. Runs: `del_20260907T044705Z_c87e44`, `del_20260907T044707Z_43e36f` (fin-model worktree `.worktrees/plan-norem-v1/.delegate/runs/`).

## Cause

The compiled reviewer schema is a top-level array — planc `contracts.py:175`, `FINDINGS = {"type": "array", "items": FINDING}`. `workflows/runtime.py:_native_schema` hands Claude every validated workflow schema as-is ("Claude's `--json-schema` takes the validated subset as-is, so it is enforced natively"), and `argv_builders.py:335` passes it through `--json-schema`. Claude Code turns that schema into a custom tool's `input_schema`; the Anthropic API requires a tool `input_schema` to be `type: object`, so the request is rejected before the model runs. `tools.8` is that structured-output tool's position in the headless tool list.

`workflows/schema.py:validate_schema_subset` accepts array-typed roots, so the workflow validates and then fails the moment the child starts — the same class of gap the enum check at `schema.py:97` was added to close.

## Reproduction (no delegate needed)

```sh
cd /var/tmp
estate-claude -p --output-format json --model opus --effort low \
  --json-schema '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}' \
  'Reply with ok true.'          # is_error:false, structured_output {"ok":true}

estate-claude -p --output-format json --model opus --effort low \
  --json-schema '{"type":"array","items":{"type":"object","properties":{"a":{"type":"string"}}}}' \
  'Reply with an empty array.'   # API Error: 400 tools.8.custom.input_schema.type: Input should be 'object'
```

Codex accepts the same array schema (its lane on the same stage succeeded), so this is Claude-specific.

## Fix options

1. **Minimal, safe today:** in `_native_schema`, return `None` for `claude` when `schema.get("type") != "object"`, so those stages take the prompt-and-parse path the other engines use. One conditional; no consumer changes.
2. **Keeps native enforcement:** wrap a non-object root in an envelope `{"type":"object","properties":{"result":<schema>},"required":["result"],"additionalProperties":false}` for the `--json-schema` hand-off and unwrap `result` when the structured output is read back. Needs the unwrap on the parse side and a test that the envelope round-trips arrays and scalars.
3. **Either way:** have `validate_schema_subset` (or a Claude-specific preflight beside the enum rule) reject or flag non-object roots for native Claude enforcement, so a workflow cannot validate and then fail at launch.

A regression test should run a workflow stage with an array root on the `claude` engine (or a unit test on `_native_schema`) and assert the child argv either carries an object root or no `--json-schema` at all.

## Workaround used in the field

Kill, patch the frozen `script.py` reviewer pool to non-Claude engines (Sol / GLM / Grok), `delegate workflow check`, resume (writing-plans run-ops recipe 1). Adjudicator, executor, verifier, and close schemas are object-rooted and unaffected, so Opus still adjudicates.
