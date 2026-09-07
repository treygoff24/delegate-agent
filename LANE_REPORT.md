# Lane report: dlg-j0q

## Status

Done pending independent verification. The audit found no runtime-generated volatile field in the workflow gate key, so the implementation deliverable is the replay regression test rather than a production-code change.

## Root cause

The field report compared different semantic checkpoints: wave 1 versus wave 2, followed later by changed checkpoint arguments. The current gate key already depends only on the checkpoint's structural scope and canonical nested-workflow arguments; replay attempts and other runtime identities do not enter it. Changed arguments change the key by design, while a changed child result at the same key is deliberately bound separately through `gateResultHash` and can also require a new approval.

## Key-input audit

The gate key is SHA-256 over the UTF-8 bytes of this exact string:

```text
gate:{scope}:{canonical_args}
```

Every variable field entering that string is:

1. `scope`: `WorkflowState.next_child_scope(f"wf:{name}")`, where `name` is `Path(name_or_path).stem`. The initial namespace is the literal `root`. Each nested workflow adds `/wf:{child_stem}@{zero-based structural ordinal}`. If the call is inside another deterministic DSL container, its parent namespace also contains the structural container coordinates: `/pipeline@{ordinal}/item#{index}/stage#{index}`, `/parallel@{ordinal}/thunk#{index}`, or the named soft-park scope. These counters are rebuilt from source-order structure on every replay; no journal sequence or replay-attempt counter contributes.
2. `canonical_args`: the entire `args` value passed to that `workflow()` call. `_canonical_json` uses `json.dumps(..., sort_keys=True, separators=(",", ":"), default=str)`. JSON object keys are therefore order-independent; array order and every JSON scalar value remain semantic. CPython's JSON encoder supplies deterministic rendering for supported floats in the pinned runtime lane.

`default=str` means out-of-contract Python objects such as `Path` and `datetime` use their string form, while dataclass instances use their normal representation unless they customize `__str__`. Those types are outside the declared `JsonValue` API and outside CLI inputs, which are parsed as JSON. If a nested Python workflow nevertheless passes one, its rendered value is treated as semantic input: a new timestamp or temporary path must change the key rather than be stripped as presumed volatility. Arbitrary custom representations are a residual reason to keep checkpoint arguments JSON-only; rejecting all legacy non-JSON nested arguments would be a separate compatibility change.

The formula does **not** read the workflow ID, workspace/root/script paths, gate mode, child result, journal sequence, status timestamps, supervisor token, replay attempt, attempt config/environment, run handle, phase, budget, or thread scheduling. The test deliberately varies the supervisor token, replay attempt, attempt timestamp, temporary-directory value, journal sequence, run handle, and dictionary insertion order while retaining the same semantic checkpoint.

## Changes

- `tests/test_workflow_gate_binding.py`: added a fresh-`WorkflowState` replay helper and a regression test proving identical checkpoint keys compare byte-for-byte across the same persisted workflow journal/state. The test also changes one semantic evidence field and proves the key changes.
- `LANE_REPORT.md`: recorded this audit and verification evidence.
- No production file changed because the audit found no runtime volatility to remove.

## Verification

- `uv run --extra dev pytest -q tests/test_workflow_gate_binding.py` before the change: **9 passed**.
- `uv run --extra dev ruff format src/ tests/ bin/`: **244 files left unchanged**.
- `uv run --extra dev pytest -q tests/test_workflow_gate_binding.py` after the change and after restoring both mutations: **10 passed**.
- Mutation check 1, temporarily adding `state.supervisor_token` to the gate-key input, then running the new test alone: **1 failed as expected** at the byte-identity assertion. The production formula was restored.
- Mutation check 2, temporarily removing canonical args from the gate-key input, then running the new test alone: the semantic-change assertion **failed as expected**; the same run also reported **1 unrelated teardown error** from the linked-worktree source-registry lock guard. The production formula was restored.
- `scripts/gate.sh`: Ruff 0.15.15 matched the pin, Ruff check passed, and all **244 files** passed format check. Pytest reached 100% with **3,279 passed, 15 skipped, and 2,378 subtests passed**, but the command exited 1 with **1 session-teardown error** because concurrent `ci-burn` Delegate parent/sibling processes outside this worktree touched `/home/trey-agent/Code/delegate-agent/.delegate/.registry.lock` while the suite ran.
- `git diff --check`: passed.

No tests, validators, timeouts, tolerances, or lint rules were relaxed.

## Not verified

The canonical gate did not produce a green exit because of external source-registry activity from the live orchestrator and sibling lanes. I did not bypass the lock guard or stop those processes. A verifier should rerun `scripts/gate.sh` when no process is operating on the source checkout's `.delegate` registry.

## Open questions

None for the supported JSON checkpoint-argument contract. Optional future hardening could reject non-JSON nested-workflow arguments instead of retaining `_canonical_json`'s legacy `default=str` fallback, but that is a compatibility decision rather than a volatility fix for this bead.
