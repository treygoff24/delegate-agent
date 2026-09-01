DO NOT SHIP

Revision `7822d6b81022d9442ad4dd89c54c2c4f7ccb5e5f` fixes the doctor write, launcher identity, and promotion race in production code. It does not fully fix F2: `validate_schema_subset` still accepts enum values that are duplicates under JSON equality, and Claude 2.1.257 rejects those schemas before authentication.

## Per-claim verification

| Claim | Verdict | Evidence |
| --- | --- | --- |
| 1. F3 doctor is read-only | **FIXED** | `active_supervisors_view` now calls `_reconcile_active_supervisors(..., write=False)` without a lock (`src/delegate_agent/workflow_pinning.py:602-627`, `637-644`). The only active-index write helper uses `write_json_atomic` (`src/delegate_agent/workflow_pinning.py:596-599`); every production reference to `active_index_path` and `_write_active_index` is in the locked reconcile/register paths at `src/delegate_agent/workflow_pinning.py:630-668`. `write_json_atomic` writes and fsyncs a temporary file, then calls `os.replace` (`src/delegate_agent/private_io.py:355-398`), so an unlocked reader sees the old or new complete file rather than a truncate-in-place writer. Direct CLI probe with a resolved interpreter and isolated HOME: `.delegate` was `MISSING` before and after `doctor`; after I seeded `active-supervisors.json`, the before/after tree contained only that 133-byte file, its SHA-256 stayed `0d0f636e...b4bc5c9e`, the returned live view was `{}`, and lock count was zero. |
| 2. F5 promote race | **FIXED** | `promote(runtime_digest=None)` now resolves `live_runtime_digest()` inside the promotion lock (`src/delegate_agent/workflow_pinning.py:737-786`, specifically `762-770`). `emit_promote` forwards `runtime_digest` without defaulting it (`src/delegate_agent/workflow_pinning.py:817-826`). The parser stores the explicit value (`src/delegate_agent/cli_parser.py:443-476`) and dispatch forwards it unchanged (`src/delegate_agent/cli.py:379-388`). In the pause-after-capture process probe, promoter A paused from inside `live_runtime_digest`; explicit promoter B could not finish while A was paused (`newer_finished_while_old_paused=False`). After release, both exited 0 and the final stamp was B's `bbbb...` digest. A separate CLI probe with `--runtime-digest dddd...` emitted the same 64-byte digest. |
| 3. F4 launcher identity | **FIXED** | `entrypoint_path(home)` is the fixed `~/.delegate/bin/delegate.py` and does not inspect `sys.argv` (`src/delegate_agent/workflow_pinning.py:284-305`). Both promotion and doctor read that same file (`src/delegate_agent/workflow_pinning.py:672-720`, `737-786`). In an isolated installed-runtime fixture, the console script, `.venv/bin/python -m delegate_agent.cli`, and the profile shell shim all reported the same runtime digest, launcher path, launcher digest, `promotionMatchesRuntime=true`, and no warnings. Appending to the installed Python launcher produced the launcher warning while the package digest still matched. The outer profile shell shim remains invisible: appending to it produced no warning. That does not invalidate the warning itself, which names the fixed installed Python launcher and claims the package and that launcher were not promoted together. One help string remains imprecise: `src/delegate_agent/command_help.py:1661-1665` says promotion records "the launcher that ran it," although console/module invocation records the fixed installed launcher instead. |
| 4. F2 schema preflight | **PARTIALLY FIXED** | Empty enum, exact duplicate enum values, duplicate `required`, and duplicate `type` members are now rejected (`src/delegate_agent/workflows/schema.py:29-67`). The enum implementation compares canonical JSON text (`line 65`), not JSON values. It therefore accepts `[1, 1.0]`, `[0, -0.0]`, `[{"k": 1}, {"k": 1.0}]`, and `[[1], [1.0]]`; Claude 2.1.257 rejects all four as duplicate enum items before authentication. It also accepts non-finite enum numbers and serializes `NaN`/`Infinity`, which Claude rejects as invalid JSON. I ran a 25-shape unauthenticated matrix under a temporary `CLAUDE_CONFIG_DIR`, with Anthropic, Claude, AWS, and Google credential variables removed. Root strings, optional objects, typed maps, strict objects, unique mixed enums, empty/unknown `required`, unique type unions, arrays, and string limits all advanced to `Not logged in`; no paid prompt was possible. The newly tightened cases did not reject any plausible shape that Claude accepted. The residual direction is still subset-accepts/Claude-rejects. |
| 5. Workflow docs and describe | **FIXED** | The docs say Claude receives every supported schema natively (`docs/delegate-workflows.md:218-226`), and `schemaNotes` says the same (`src/delegate_agent/describe_payload.py:1065-1075`). This matches `_native_schema`, which returns the schema unchanged for Claude (`src/delegate_agent/workflows/runtime.py:3751-3761`), after subset validation and before passing the temporary schema through `output_schema` (`src/delegate_agent/workflows/runtime.py:2805-2850`). |
| 6. Test quality | **PARTIALLY FIXED** | The doctor test now catches the old lock-file mutation, but two claims still outrun their tests: the promote-lock test does not cover the exact old `emit_promote` capture point, and the enum test does not implement JSON numeric equality. Details follow. |

## Test quality, test by test

### `tests/test_workflow_pinning.py`

`test_doctor_never_writes_under_home` (`tests/test_workflow_pinning.py:430-457`) exercises the real lock-file failure and snapshots the whole HOME subtree. Reintroducing the old lock-taking `active_supervisors_view` by monkeypatch made it fail. Its "empty home" half is not actually empty because `setUp` precreates `.delegate/personas` (`tests/test_workflow_pinning.py:21-27`); it does not prove the no-directory case. The external CLI probe above does.

`test_launcher_identity_is_the_installed_file_not_argv` (`tests/test_workflow_pinning.py:459-468`) is manufactured: it patches `sys.argv` rather than executing the three entrypoints. It does bind the fixed-path rule, and the external console/module/shim probe supplies the missing integration evidence.

`test_doctor_warns_when_installed_launcher_changed_since_promotion` (`tests/test_workflow_pinning.py:470-484`) exercises the real failure: it promotes, rewrites the actual installed launcher file, then checks doctor.

`test_promote_reads_the_live_digest_under_the_promotion_lock` (`tests/test_workflow_pinning.py:486-514`) inspects the lock from inside a patched digest function. It binds the `promote` seam but manufactures the concurrency check rather than reproducing the two-promoter race. Moving default capture immediately outside `promote` made the test red. Reintroducing the exact old production behavior only in `emit_promote` left this new test green because it calls `promote` directly. The process race probe, not this test alone, proves the current full path is fixed.

### `tests/test_workflow_schema.py`

- `test_empty_enum_rejected` (`tests/test_workflow_schema.py:116-118`) uses a real Claude-rejected shape at the validator seam, but does not run Claude.
- `test_duplicate_enum_values_rejected` (`tests/test_workflow_schema.py:120-124`) covers exact string, integer, object, and null duplicates. It misses JSON-equal numeric representations and nested numeric equivalents, so it stays green with the ship-blocking gap.
- `test_duplicate_required_rejected` (`tests/test_workflow_schema.py:126-129`) uses the real Claude-rejected shape at the validator seam.
- `test_duplicate_type_union_rejected` (`tests/test_workflow_schema.py:131-134`) uses the real Claude-rejected shape and includes one valid union control.

## New defects and residual risks

1. **Ship blocker, residual F2:** enum uniqueness is canonical-text equality rather than JSON equality. A workflow schema can pass Delegate validation and die at Claude launch. Fix numeric equality recursively, keeping booleans distinct from numbers, and add scalar plus nested numeric-equivalence cases.
2. **Additional F2 gap:** non-finite floats pass the subset even though they are not JSON and Claude rejects them during argument parsing. Reject non-finite numbers anywhere in a schema before native routing.
3. **Minor help defect:** `src/delegate_agent/command_help.py:1661-1665` says the promotion records the launcher that ran it. It records the fixed installed launcher, even when another entrypoint ran the command.

I found no other material production defect in the doctor, promote, launcher, or Claude-native routing changes.

## What I checked and found clean

- Confirmed HEAD is exactly `7822d6b81022d9442ad4dd89c54c2c4f7ccb5e5f`; `git diff --check 6520bba..7822d6b` passed.
- Ran the requested slice with bytecode, pytest cache, and uv synchronization disabled: **336 passed, 62 subtests passed in 231.49s**.
- Confirmed installed Claude Code is **2.1.257**. Every accepted schema probe stopped at the unauthenticated `Not logged in` boundary; no model request was made.
- Ran isolated doctor, promotion-race, explicit-digest, cross-entrypoint launcher, inner-launcher rewrite, outer-shim rewrite, and mutation probes.
- Final `git status --short --branch` was clean. I made no repository changes or mutating Git calls.
