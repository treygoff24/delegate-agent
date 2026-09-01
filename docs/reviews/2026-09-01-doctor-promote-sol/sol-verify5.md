SHIP

HEAD `084be855f92040a3e07d8d31ce794f67dca6da81` closes all three round-4 behavioral defects. I found no material implementation defect in `git diff 2e00859..HEAD`. The new code has one low-severity wording problem: two comments/error messages describe the numeric semantics inaccurately.

## Per-claim verification

| Claim | Verdict | Evidence |
| --- | --- | --- |
| 1. `_json_identity` rejects unsafe integers and non-string object keys | **FIXED** | Integers with `abs(value) > 2**53` raise `SchemaError` before identity construction (`src/delegate_agent/workflows/schema.py:34-39`). Object keys are checked before sorting (`src/delegate_agent/workflows/schema.py:48-54`), so mixed key types cannot leak a `TypeError`; recursion through lists and string-keyed objects makes both checks apply at any depth (`lines 46-54`). Direct probes covered positive and negative integers, nested integers, integer/boolean/NaN keys, mixed keys, and deeply nested keys. Every invalid case raised `SchemaError`, never `TypeError`. |
| 1a. Delegate/Claude preflight parity | **FIXED in the launch-failure direction** | I reran the 40 round-4 cases and added 14 boundary/nesting cases against the preserved Claude Code 2.1.257 binary. All three unsafe-integer duplicate shapes and both key-coercion duplicate shapes now reject on both sides. Across 54 comparisons there were **no subset-accepts/Claude-rejects divergences**. There were nine subset-rejects/Claude-accepts cases: five schemas containing an unsafe integer that Claude happened to accept after binary64 parsing, and four schemas whose Python non-string keys serialized as strings. Those are intentional conservative restrictions required by this fix, not launch failures. Exactly `+/-2**53` was accepted by both Delegate and Claude; `2**53` minus 1 and `2**53` remained distinct, while `2**53` and `float(2**53)` were duplicate. Accepting `2**53` is correct because that boundary value is exactly representable in binary64. |
| 2. Runtime enum membership uses JSON identity | **FIXED** | `validate_value` builds and compares `_json_identity` keys (`src/delegate_agent/workflows/schema.py:119-126`). Direct probes rejected `True` against `[1]`, `1` against `[True]`, object/list/deeper nested boolean-number variants, and accepted `1.0` against `[1]`, the nested equivalent, and reordered equal objects. |
| 2a. A failed membership candidate is retried | **FIXED** | The structured path validates before returning (`src/delegate_agent/workflows/runtime.py:2925-2934`) and routes validation exceptions into `agent_structured_retry` (`lines 2940-2978`). A mocked two-attempt probe used first output `true`, second output `1`, and schema `{"enum":[1]}`. It made two child calls, recorded `agent_structured_retry` with `value must be one of [1].`, and returned `1` as an `int`, not the first boolean candidate. |
| 3. Anything else in `2e00859..HEAD` | **CLEAN except low wording issue** | The production and test diff is narrow, lint-clean, formatted, and whitespace-clean. No material behavioral or standards defect was found. The wording issue is below. |

## New defect

### Low: numeric comments and diagnostic overstate the semantics

The membership comment says Python accepts both `True` for `1` and `1.0` for `1`, but "JSON does not" (`src/delegate_agent/workflows/schema.py:121-124`). Claude's equality and this implementation do treat `1.0` and `1` as equal. The unsafe-integer diagnostic says every integer beyond `2**53` is rounded (`lines 34-38`), but values such as `2**53 + 2` are exactly representable. The real reason for the conservative cutoff is that binary64 parsing is no longer injective over all integers above the boundary. These are misleading maintenance/operator messages, not a ship blocker.

## Test-credit boundary

The added tests bind direct unsafe-integer rejection, direct non-string-key rejection, boolean-versus-number membership, nested membership, and `1.0`-versus-`1` acceptance (`tests/test_workflow_schema.py:137-166`). They do not directly bind deeper recursive key/integer rejection or an enum-specific structured retry. The manual direct and two-attempt probes above supply that evidence; the green checked-in suite alone does not.

## What I ran

- `UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider tests/test_workflow_schema.py tests/test_workflow_commands.py tests/test_workflow_retry_outcomes.py`: **197 passed, 44 subtests passed in 207.87s**.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 /tmp/dlg-review/verify5_matrix.py`: 54 credential-free Claude 2.1.257 comparisons. Accepted schemas stopped at `Not logged in`; the clean smoke reported zero tokens and cost.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 /tmp/dlg-review/verify5_direct.py`: direct identity, membership, and two-attempt retry probes passed.
- `git diff --check 2e00859..HEAD`, focused `ruff check`, and focused `ruff format --check`: passed.
- The merge base was exactly `2e00859`; final worktree inspection was clean. No repository files were changed.
