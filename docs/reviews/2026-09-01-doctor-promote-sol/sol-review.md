DO NOT SHIP: tracked Claude schema continuity is broken, workflow schemas still bypass Claude's native enforcement, and `doctor` violates the read-only profile boundary.

## Findings

### 1. `major` - Claude schemas are not persisted, and an inherited Claude schema crashes before launch

**Evidence:** `src/delegate_agent/request_build.py:3480-3500`, `src/delegate_agent/request_build.py:2981-2988`, `src/delegate_agent/resume_command.py:835-840`.

**Failure scenario:** Launch `delegate claude safe --output-schema schema.json ...`. The initial argv is correct, but `_build_request_for_workspace` records schema text only when `engine == "codex"`, so the tracked manifest has no `outputSchema`. A later `delegate resume` therefore silently drops schema enforcement. If a Claude manifest does contain `outputSchema` (for example, a seeded record or after fixing persistence), resume sets the inline placeholder, but `_claude_request_parts` treats that placeholder as a real path and raises `invalid_output_schema` before launch. I reproduced both states: the initial request had `output_schema_record_text=None`; an inline Claude request failed with `Output schema is not readable: <delegate-inline-output-schema>`.

**Fix:** Record raw schema text for tracked Claude safe/work requests. Carry `output_schema_text` into `_claude_request_parts` and pass it directly to `build_claude_argv` instead of reopening `build.output_schema`. Add an end-to-end test that creates a real tracked Claude manifest, resumes it, verifies redacted argv, and confirms `--json-schema` receives the inherited bytes.

### 2. `major` - Workflow Claude schemas still take the prompt-only path

**Evidence:** `src/delegate_agent/workflows/runtime.py:2805-2808`, `src/delegate_agent/workflows/runtime.py:2831-2850`, `src/delegate_agent/workflows/runtime.py:2873-2888`.

**Failure scenario:** A workflow calls `agent(..., engine="claude", schema=SCHEMA)`. `native_schema` is created only for Codex, so Claude falls into `_structured_prompt(...)` and `_run_delegate(..., output_schema=None)`. The new tracked Claude capability is never used; malformed model JSON can still reach the retry parser even though Claude now has native schema enforcement.

**Fix:** Treat Claude as a native-schema engine in workflow runtime, using the workflow schema bytes appropriate to Claude rather than Codex-only normalization. Add a Claude workflow test that asserts the generated child input JSON contains `outputSchema`, the child argv contains redacted `--json-schema`, and a schema-shaped result is adopted without prompt-only retry behavior.

### 3. `major` - `doctor` is classified as read-only but writes machine state

**Evidence:** `src/delegate_agent/profile_guard.py:28-45`, `bin/delegate-profile-shim:106-140`, `src/delegate_agent/workflow_pinning.py:576-602`, `src/delegate_agent/workflow_pinning.py:630-639`, `src/delegate_agent/command_help.py:1623-1624`.

**Failure scenario:** With `AI_PROFILE=work` and no readable work overlay, both guards allow `delegate --json doctor` as read-only. `doctor()` calls `reconcile_active_supervisors()`, which creates or rewrites `~/.delegate/active-supervisors.json` and its lock when the index is missing or stale. My shim probe returned 0 and created the file under the fallback HOME. This contradicts the guard's stated launch-or-mutation boundary and the help text's claim that doctor only reads.

**Fix:** Make doctor inspection byte-preserving: compute a reconciled view in memory and move stale-entry pruning to an explicit mutation path. If reconciliation must remain mutating, remove doctor from both read-only allow-lists and correct the docs; the former is preferable because doctor must remain usable when an overlay is broken.

### 4. `major` - the live digest can report clean while the invoked launcher is stale

**Evidence:** `src/delegate_agent/workflow_pinning.py:244-279`, `bin/delegate.py:1-15`.

**Failure scenario:** `_runtime_source_files()` reads the imported `delegate_agent` package but appends embedded workflow-pin `_LAUNCHER` and `_SITE_CUSTOMIZE` templates. It does not read the actual installed `~/.delegate/bin/delegate.py` that invoked the process, nor the optional profile shim. A partial upgrade can update the package while leaving a behaviorally stale launcher or shim; `promote` and `doctor` compute the same package/template digest and report `promotionMatchesRuntime: true`.

**Fix:** Separate the workflow-pin digest from the installed-runtime digest. The installed digest should cover the actual resolved entrypoint and every installed file the promotion ritual promises to replace. Keep a direct test that the pin digest equals `_write_runtime_snapshot`'s directory digest, and add a negative test where the installed launcher differs.

### 5. `major` - concurrent promotes can replace a newer stamp with an older one

**Evidence:** `src/delegate_agent/workflow_pinning.py:697-715`, `src/delegate_agent/private_io.py:355-398`.

**Failure scenario:** Two agents compute different runtime digests and promotion timestamps. The older process can pause after constructing its payload, the newer process can publish, and the older process can then `os.replace` the stamp last. JSON stays intact, but `last-promotion.json` now names the older runtime and time. This is plausible on the shared runtime the command is meant to coordinate.

**Fix:** Serialize promotion under a dedicated lock and construct the timestamp/digest while holding it. If different-runtime concurrent installs are possible, make the installer and promote use the same lock or reject replacing a stamp whose `promotedAt` is newer.

### 6. `minor` - inserting `PromoteOptions` removed the dataclass contract from `ParsedCommand`

**Evidence:** `src/delegate_agent/request_models.py:126-134`.

**Failure scenario:** `@dataclass(init=False)` now decorates `PromoteOptions` on top of `@dataclass(frozen=True)`, while `ParsedCommand` has no decorator. `ParsedCommand` instances lose dataclass equality, representation, and field introspection; `dataclasses.fields(ParsedCommand)` now raises `TypeError`. `PromoteOptions.__dataclass_params__` is also inconsistent with its generated frozen methods.

**Fix:** Put one `@dataclass(frozen=True)` on `PromoteOptions` and restore `@dataclass(init=False)` immediately above `ParsedCommand`. Add a small invariant test for both classes.

### 7. `minor` - valid short schema JSON is marked `suspect_short`

**Evidence:** `src/delegate_agent/runner.py:1019-1037`, `src/delegate_agent/harness_events.py:68-98`.

**Failure scenario:** A safe schema run returns valid compact JSON such as `{"ok":true}`. The result event is correctly treated as completion text, but the generic safe-report heuristic classifies any sub-200-character child report that does not look like prose as `suspect_short`. The run remains successful, but emits a false warning precisely because schema output is compact.

**Fix:** Skip prose-length heuristics when the run manifest records an enforced output schema, and add short/long structured-output cases for both Codex and Claude.

### 8. `minor` - the public doctor envelope uses the active-supervisor schema name

**Evidence:** `src/delegate_agent/workflow_pinning.py:644-650`, `tests/test_workflow_pinning.py:388-401`.

**Failure scenario:** `delegate --json doctor` returns digest, promotion, match status, warnings, and supervisors but labels the envelope `delegate.active-supervisors.v1`. A consumer selecting a decoder by `schema` receives a materially different object than the schema name promises.

**Fix:** Introduce `delegate.doctor.v1` for the public envelope. Keep the active-supervisor index schema only on the nested/on-disk index.

### 9. `minor` - help and README coverage is incomplete

**Evidence:** `src/delegate_agent/command_help.py:2327`, `README.md:456-460`.

**Failure scenario:** Global help advertises `--output-schema` for direct tracked Claude runs and Claude call dry-runs, but omits it from the tracked `dry-run claude {safe,work}` line. README's missing-overlay diagnostic list omits `doctor`, even though both guards allow it.

**Fix:** Add the option to the tracked Claude dry-run usage and add a test requiring it on all four Claude schema-capable overview lines. Add `doctor` to the README list after resolving the read-only mutation bug.

## A. Schema lift correctness

Initial direct CLI and input-JSON construction accept Claude schemas in safe/work mode and retain `--output-format stream-json`; call mode retains `json` (`src/delegate_agent/argv_builders.py:259-267`, `src/delegate_agent/argv_builders.py:334-335`). `harness_events.StreamAccumulator._ingest_result_event` accepts the schema-bound `result` string as completion text (`src/delegate_agent/harness_events.py:933-944`). Snapshot and run-output recovery use that completion text correctly.

Worktree and ordinary manifests are not correct because `output_schema_record_text` is Codex-only. That makes the changed Claude resume branch unreachable from a normally created manifest. Seeded metadata reaches the branch, but inline Claude resume then fails as described in finding 1.

`argv_utils.public_argv` redacts the value after `--json-schema` (`src/delegate_agent/argv_utils.py:36-45`). The tracked manifest callers and dry-run payload use `public_argv`; persistent worktree execution also derives its display argv through it. I found no schema-byte leak in those persisted or displayed surfaces.

`--pass-through` sets `stream_capture=False`, so Claude uses `--output-format text` while still receiving `--json-schema`. Raw schema-bound JSON text goes directly to stdout with no tracking, snapshot, completion report, or resume metadata. Local Claude 2.1.257 help does not prohibit the combination. The branch lacks a focused test.

Native `followup` does not inherit output schema for either Codex or Claude (`src/delegate_agent/followup_command.py:370-384`). That is pre-existing behavior, but it should be documented because a structured work run can be followed by an unstructured continuation. Workflow runtime still assumes native schema means Codex, finding 2. `structuredOutput` is emitted on stateless call results only; tracked schema identity is supposed to be the manifest's `outputSchema`. The latter is missing for Claude, so tracked metadata is misleading by omission.

## B. Safe-mode boundary

The schema lift does not add write-capable tools. Claude safe still uses `--permission-mode plan`, `--tools Read,Grep,Glob,Bash`, the narrow `--allowedTools` list, and `--strict-mcp-config` (`src/delegate_agent/argv_builders.py:286-297`). Local Claude Code 2.1.257 describes `--json-schema` as JSON Schema validation for structured output. It is a final-output channel, not a filesystem or shell capability. `StructuredOutput` is not added to Delegate's safe tool allow-list; any harness-internal structured-output mechanism has no workspace mutation primitive of its own.

Schema bytes enter a subprocess argv list, not a shell command. Flag-like schema contents cannot become new argv tokens. Schema descriptions can influence what the model emits, which is expected control input, but cannot expand the tool allow-list. The full schema is visible to same-host process inspection while Claude runs. Stored manifests and dry-run argv are redacted, but operators should not put secrets in schema descriptions.

## C. `doctor` / `promote`

Pin parity is exact by construction: `live_runtime_digest()` and `_write_runtime_snapshot()` both digest `_runtime_source_files()`. This is not a false positive between those two functions. That exact parity is narrower than the public claim because the digest does not read the actual installed launcher, finding 4.

`_runtime_source_files()` includes every regular non-`.pyc`/`.pyo` file anywhere under `delegate_agent`. Tests are outside that package and excluded. `.DS_Store`, swap files, backups, and other non-Python regular files inside the package are included. A promotion stamped after copying them matches immediately; adding or removing such junk later triggers a warning even if executable behavior did not change.

Promotion publication uses a private temp file, `fsync`s it, atomically replaces the target, and leaves mode 0600. Symlink checks are present. The helper does not `fsync` the parent directory after replacement, so atomic visibility is stronger than crash durability. Concurrent writers do not produce torn JSON, but last replace wins without ordering protection, finding 5.

## D. Profile guard parity

The shell classifier strips leading `--json`, then reaches `doctor`; `delegate --json doctor` and `AI_PROFILE=work delegate doctor` are allowed when the overlay is missing. My end-to-end shim probe confirmed return code 0. Python and shell layers agree that `doctor` is read-only and `promote` is a mutation. The doctor classification is semantically wrong because reconciliation writes, finding 3.

Treating `promote` as a mutation is conservative, but it blocks the stamp when an overlay is missing during an upgrade. My shim probe returned 1 and wrote no stamp. The ritual remains recoverable via `env -u AI_PROFILE delegate promote ...`, but the new docs do not say so. A better model would distinguish profile-independent machine mutations from account-sensitive mutations rather than mislabel `promote` as read-only.

## E. Tests

The new parser, argv, warning, dispatch, and mode tests are load-bearing for their narrow assertions. Reinstating the old Claude tracked-mode refusal, changing tracked output back to `json`, removing parser dispatch, or dropping 0600 would fail them.

Weak or vacuous coverage:

- `test_promote_defaults_to_live_digest_and_doctor_then_matches` compares two calls to the same `live_runtime_digest()` implementation. It cannot detect a digest of the wrong installed files or the ignored launcher.
- The profile-shim test searches source text for `|personas|doctor|`; it does not execute `--json doctor`, prove missing-overlay behavior, or detect doctor filesystem writes.
- The Claude tracked-schema test stops at initial argv. It does not assert `output_schema_record_text`, manifest bytes, inline resume, worktree manifests, pass-through, result-quality classification, or workflow routing.
- Resume tests seed `outputSchema` on the default Codex source. None exercises a Claude manifest or the Claude inline builder.
- All promote tests are sequential; none detects last-writer regression.
- Help tests require only that `--output-schema FILE` appears somewhere, so the missing tracked Claude dry-run line passes.

Required before merge: real Claude manifest/resume coverage, Claude workflow-native schema coverage, a byte-preserving doctor/profile-guard test, actual-launcher digest mutation coverage, concurrent promote coverage, and all-four-lines Claude help coverage.

## F. Docs/help drift

`docs/cli-reference.md` and the direct Claude `CommandSpec` correctly advertise tracked schemas. `render_overview_text` omits the option from tracked Claude dry-run, and README's profile-guard list omits doctor, both in finding 9.

`COMMAND_SPECS` contains doctor/promote, so the help index and both full and summary `describe` command catalogs include them automatically. I found no catalog omission there. Doctor's help and CLI reference call it read-only while it reconciles state on disk, finding 3. The public JSON schema label is wrong, finding 8.

## What I checked and found clean

The fixed point was `main` at `f2cec276389f7063e79a1cdc4f46384a1acfdcd6`; HEAD was `ae5bfc087399636a95f4b0f04f271e912f1e4f00`. The diff contained the stated two commits and passed `git diff --check`.

Initial Claude safe/work argv keeps `stream-json`, inlines schema under `--json-schema`, and suppresses completion-report prompt injection. Claude call mode keeps one `json` envelope. Result-event parsing, snapshot completion text, and run-output recovery do not assume Claude schemas imply call mode. Public argv redaction covers normal, dry-run, temporary safe, persistent worktree, and attached execution manifest paths I traced. Safe-mode flags and allowed Bash commands are unchanged by the schema lift.

`parse_runtime_subcommand` rejects workspace, isolation, pass-through, auth-profile, malformed digest, and missing actor/source inputs; CLI dispatch occurs before workspace resolution. The shell shim correctly reaches doctor after `--json` and blocks promote when the work overlay is absent. Stamp creation is private, symlink-resistant, and atomically visible with 0600 mode. `doctor` and `promote` appear in `COMMAND_SPECS`, the help index, and `describe --summary`.

Verification used `PYTHONDONTWRITEBYTECODE=1 UV_NO_SYNC=1 uv run --extra dev pytest -q -p no:cacheprovider` on the six relevant modules: **362 passed and 1,187 subtests passed**. Focused Ruff check and format-check passed on all 14 changed Python/test files. `python3 bin/delegate.py --json describe --summary` returned successfully. Local Claude Code was 2.1.257. I did not run `scripts/gate.sh` or a paid live Claude prompt.

I made no source, documentation, or test edits. When first inspected, the worktree showed `.beads/issues.jsonl` modified; I did not restore it.
