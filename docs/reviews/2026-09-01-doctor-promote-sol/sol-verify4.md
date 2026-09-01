DO NOT SHIP

Commit `2e00859282cabe7e9b43be47ad217ae41805722c` fixes the reported ordinary numeric-equality cases, nested non-finite values, the promote help text, and the `emit_promote` test gap. It still permits schemas that pass Delegate's subset validator and fail Claude Code 2.1.257 preflight.

## Per-claim verification

| Claim | Verdict | Evidence |
| --- | --- | --- |
| 1. `_json_identity` closes enum-preflight parity | **PARTIALLY FIXED** | `_json_identity` tags booleans separately, normalizes integral floats to integers, recurses through arrays and objects, and rejects non-finite values (`src/delegate_agent/workflows/schema.py:24-43`); enum uniqueness uses it at `src/delegate_agent/workflows/schema.py:81-90`. Direct probes rejected `[1, 1.0]`, `[0, -0.0]`, `[{"k": 1}, {"k": 1.0}]`, `[[1], [1.0]]`, `1e2` versus `100`, reordered nested objects, and direct or nested NaN/positive Infinity/negative Infinity. `[true, 1]` and `["1", 1]` remained distinct. The same 25-case round-3 matrix matched Claude 2.1.257 in every case. The expanded matrix found three subset-accepts/Claude-rejects divergences at unsafe integer precision, and two more after JSON object-key coercion. No subset-rejects/Claude-accepts divergence appeared in the 40 Claude comparisons. |
| 2. Promote help names the fixed installed launcher | **FIXED** | The note now says promotion records the digest of `~/.delegate/bin/delegate.py`, regardless of the invoking entrypoint (`src/delegate_agent/command_help.py:1661-1666`). That matches `entrypoint_path`, which ignores `sys.argv` and returns only the fixed installed launcher when present (`src/delegate_agent/workflow_pinning.py:284-305`), and `promote`, which records its path and digest (`src/delegate_agent/workflow_pinning.py:762-775`). In a temporary HOME, I invoked promotion with `sys.argv[0]` set to `/different/entrypoint`; the stamp still named the fixed launcher and its SHA-256 matched. |
| 3. The lock test now covers `emit_promote` and kills the old behavior | **FIXED** | The test calls both `promote` and `emit_promote` under the patched digest probe and expects two locked observations (`tests/test_workflow_pinning.py:486-520`). Production `emit_promote` forwards `None` into `promote` instead of capturing the digest early (`src/delegate_agent/workflow_pinning.py:817-826`). I monkeypatched the exact old early-capture behavior into `emit_promote` and ran this test alone. It failed with `observed == [True, False]`, not `[True, True]`. |
| 4. Nothing else in `7822d6b..2e00859` is wrong | **FAILED** | The full four-file diff is formatted and lint-clean, but the enum identity is still incomplete at the Claude precision boundary, for non-string object keys, and at runtime membership validation. Details follow. |

## New and residual defects

### 1. Ship blocker: unsafe integers still pass Delegate and fail Claude preflight

Python integers retain arbitrary precision in `_json_identity`, while Claude Code 2.1.257 parses schema numbers at binary64 precision (`src/delegate_agent/workflows/schema.py:28-33`). Delegate accepted each of these enums; Claude rejected each as duplicate items before authentication:

- `[9007199254740993, 9007199254740992.0]`
- `[9007199254740992, 9007199254740993]`
- `[9223372036854775807, 9223372036854775808]`

This is the same operational failure as F2: subset validation succeeds, then the child dies at launch. Either reject enum integers outside Claude's exact integer range or compute identity under the actual serialization/parser semantics used at preflight.

### 2. High: non-string object keys escape JSON validation

The object branch sorts raw dictionary keys and never requires strings (`src/delegate_agent/workflows/schema.py:38-42`). Workflow literal schemas reach this validator as Python objects, and the caller catches `SchemaError`, not arbitrary comparison errors (`src/delegate_agent/workflows/script.py:161-178`). Three failures follow:

- `[{1: "x"}, {"1": "x"}]` and `[{True: "x"}, {"true": "x"}]` pass Delegate, serialize to duplicate JSON objects, and are rejected by Claude preflight.
- A single enum object with mixed integer and string keys raises an uncaught `TypeError` from `sorted`, rather than `SchemaError`.
- NaN and positive or negative Infinity are accepted as object keys. JSON objects cannot have numeric keys; reject them as non-JSON before sorting.

### 3. High: runtime enum membership still uses Python equality

The new identity correctly treats booleans and numbers as distinct during declaration validation, but `validate_value` still checks membership with Python's `value not in schema["enum"]` (`src/delegate_agent/workflows/schema.py:107-110`). Direct probes accepted `True` against `[1]`, `1` against `[True]`, `{"k": True}` against `[{"k": 1}]`, and `[True]` against `[[1]]`. Structured retry logic trusts this result and returns the candidate (`src/delegate_agent/workflows/runtime.py:2925-2934`). This line predates the commit, but the new JSON-equality rule remains incomplete until runtime membership uses the same identity.

## What I ran and found clean

- Invoked preserved Claude Code `2.1.257` with only temporary `HOME` and `CLAUDE_CONFIG_DIR` values. Every accepted clean schema stopped at `Not logged in`; no paid call occurred.
- Compared the same 25 round-3 schemas plus 15 adversarial cases. Only the unsafe-integer and non-string-key cases diverged.
- Ran `UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider tests/test_workflow_schema.py tests/test_workflow_pinning.py tests/test_command_help.py`: **82 passed, 958 subtests passed in 12.61s**.
- Ran the old-`emit_promote` mutation against the exact lock test: **1 failed as expected** with `[True, False] != [True, True]`.
- Ran `git diff --check 7822d6b..2e00859`, focused `ruff check`, and focused `ruff format --check`: all passed.
- Final `git status --short --branch` showed a clean worktree at `2e00859`. I made no repository changes or mutating Git calls.
