# Wave-1 review (Opus) — feat/harness-compat-audit @ 77ba100

Diff base `git merge-base main HEAD` = `12fe7ac`. Read-only; no files edited, no live model
calls. Verification used `python3 bin/delegate.py --json dry-run`, `.venv/bin/pytest -q
-p no:cacheprovider <file>` on nine affected test files, direct `bwrap` probes, and
string/help probes of the installed `claude` and `cursor-agent` binaries.

---

## 1. BLOCKER — the redacted terminal reason is written to the run record verbatim anyway

`src/delegate_agent/harness_events.py:682-692` (`_record_terminal_event`), consumed by
`src/delegate_agent/runner.py:663-671`.

Commit `5329637` redacts `terminalEvent.reason` because "a usage-limit message that quoted
an Authorization header therefore wrote a bearer token to disk". The same method appends
`NormalizedEvent(kind="run.completed", message=reason)` with the **unredacted** reason, and
`runner.py:666` writes `record["recentEvents"] = recent_events` into the same run record.
The secret still reaches disk, now twice per error.

```
$ .venv/bin/python -c "
from delegate_agent import harness_events as he
a = he.StreamAccumulator(harness='codex')
a.ingest_line('{\"type\":\"error\",\"message\":\"401 from api: Authorization: Bearer sk-ant-api03-SECRETVALUE123\"}')
print(a.terminal_event); print(a.bounded_recent_events()[0])"
{'event': 'error', 'status': 'failed', 'reason': '401 from api: Authorization: ***'}
[{'kind': 'error', 'message': '401 from api: Authorization: Bearer sk-ant-api03-SECRETVALUE123'},
 {'kind': 'run.completed', 'status': 'failed', 'message': '401 from api: Authorization: Bearer sk-ant-api03-SECRETVALUE123'}]
```

The `kind: "error"` row is pre-existing, but the `run.completed` mirror is new on this
branch: before it, a generic `error` event never recorded a terminal at all
(`shared.md` B1). Smallest fix: redact once at the source — apply `redact_string` in
`_ingest_error_event` before `_last_error_message` is stored and the `error` event is
appended, and drop the second redaction in `_record_terminal_event`. That covers both
sinks and the `current` line with one call.

## 2. MAJOR — `kimi safe` under the bwrap backend refuses to launch when `~/.kimi-code` is absent

`src/delegate_agent/sandbox_bwrap.py:78-86` (`_engine_home_path`), bound unconditionally at
`:432-436`, checked at `:477`.

`_ENGINE_HOME_DEFAULT_DIR` makes `$HOME/.kimi-code` an unconditional `--bind` source for
every kimi run. `build_bwrap_argv` never checks existence, and bwrap refuses a missing bind
source. The old `.kimi` row lived in `_OPTIONAL_ENGINE_DOT_DIRS`, which *is* existence
filtered (`:488 os.path.isdir`). A kimi-equipped machine that has never run kimi now cannot
launch a bwrap safe run.

```
$ .venv/bin/python -c "
from delegate_agent import sandbox_bwrap as s; import tempfile, os
home, ws = tempfile.mkdtemp(), tempfile.mkdtemp()
argv = s.wrap_engine_argv(engine_argv=['/bin/true'], cwd=ws, env={'HOME': home, 'PATH': os.environ['PATH']}, engine='kimi')
s.preflight_plan(argv)"
DelegateError: bwrap preflight failed: bwrap: Can't find source path /tmp/tmpXXXX/.kimi-code: No such file or directory
```

All three new tests (`tests/test_sandbox_bwrap.py:262-320`) call `.mkdir()` first, so the
case is untested. Smallest fix: in `_engine_home_path`, return the default-directory branch
only when `os.path.isdir(candidate)`; keep the explicit `KIMI_CODE_HOME` branch unguarded
so a bad override still fails loudly.

## 3. MAJOR — a kimi/opencode/pi run whose stdout is plain text now returns no text at all

`src/delegate_agent/harness_events.py:736-758` (`_record_malformed_line` increments
`structured_events_seen`), consumed at `src/delegate_agent/runner.py:4810-4820`.

The plan's E3 asked for a bounded diagnostic while keeping the malformed-line protection.
The implementation additionally counts each malformed line as a structured event, which
flips `runner.py:4810` from the raw-stdout fallback to "suppressed raw event output".
Before the change these lines returned early without touching the counter, so a child that
printed a plain-text failure to stdout still delivered it as the call text.

```
$ .venv/bin/python -c "
from delegate_agent import harness_events as he
a = he.StreamAccumulator(harness='pi')
a.ingest_line('Error: no credentials found for provider anthropic')
a.finish_stream(); print(a.structured_events_seen, repr(a.assistant_text))"
1 ''
```

`delegate pi call ...` against an unauthenticated pi now returns empty text plus the generic
warning; the message survives only if pi also wrote it to stderr. Smallest fix: leave
`structured_events_seen` for parsed objects and change the fallback condition at
`runner.py:4810` to `accumulator.structured_events_seen > accumulator.malformed_lines`, so a
stream that produced at least one real event still suppresses raw stdout.

## 4. MAJOR — a Claude call whose `result` is the empty string is now `call_output_invalid`

`src/delegate_agent/runner.py:4540-4543` with `harness_events.claude_result_text`
(`harness_events.py:566-591`), which requires `result.strip()`.

The old guard accepted any `str`, including `""`, and returned exit 0. The new extractor
returns `None` for an empty string, so the caller falls into the invalid-output branch.

```
$ .venv/bin/python -c "
from delegate_agent import runner
print(runner._parse_claude_call_json('[{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"\",\"permission_denials\":[]}]', pure=False)[:2])"
('', 1)      # was ('', 0)
```

This is reachable, not hypothetical: Claude's deferred-tool-use branch emits exactly
`result: ""` with `is_error: false` (visible in the 2.1.263 bundle at byte 199429275,
`yield {...v, is_error:!1, stop_reason:"tool_deferred", result:""}`). Smallest fix: in
`_parse_claude_call_json`, when `claude_result_text` returns `None`, fall back to
`result.get("result")` if it is a `str` before declaring the output invalid.

## 5. MAJOR (unverifiable here) — codex `turn.completed` now arms the 1-second terminal-exit kill

`src/delegate_agent/harness_events.py:1316-1327`, reaching
`src/delegate_agent/runner.py:2271-2272` and `:2414-2426`; `TERMINAL_EXIT_GRACE_SEC = 1.0`
at `runner.py:71`.

Plan item E7 asked only for usage capture and candidate clearing. The implementation also
records a `succeeded` terminal, which sets `terminal_signal`. One second later the runner
SIGTERMs the child if it has not exited and flags `stoppedAfterCompletion`. Before this,
a successful codex run had no terminal status and the runner waited for natural exit. Any
codex work done after `turn.completed` — rollout flush, session-file finalization on a
`--resumable` run — is now on a one-second clock. I could not test this without a live
codex run. Either verify with one live `codex --resumable` round trip that the session file
is complete and resume still works, or record the terminal status without arming the kill
(a separate flag on `_record_terminal_event`, or exclude codex from `terminal_signal`).

The finalizer ordering is sound: `runner.py:2763-2777` lets a provider terminal override
the new success status, so a max-turns codex run still finalizes failed. Verified by reading.

## 6. MINOR — `validate_cache_payload` silently invalidates a whole reasoning cache that mentions cursor

`src/delegate_agent/reasoning.py:1426`.

R4 asked to accept a `grok` row "without touching the routing tables". The change replaced
the `TRANSPORT_BY_HARNESS` membership test with a literal `("codex", "droid", "grok")`.
`TRANSPORT_BY_HARNESS` is `{codex, cursor, droid}`, so `cursor` was dropped. A cache file
carrying a cursor row now raises, and `_load_cache` (`reasoning.py:799-802`) swallows the
error and returns `None`, discarding the codex rows in the same file. Smallest fix:
`if harness not in (*TRANSPORT_BY_HARNESS, "grok")`.

## 7. MINOR — `parse_grok_catalog` now requires a bullet, so an unbulleted list fails the whole probe

`src/delegate_agent/harness_discovery.py:1278`.

D1 asked to accept `-` bullets and drop the `startswith("-")` break. The regex went from
`^\*?\s*(\S+?)...` to `^[*-]\s+(\S+?)...`, which also rejects the previously-accepted
unbulleted form. A grok build that prints plain selectors raises
`ValueError("Grok catalog had no Available models entries")` and fails the probe outright.
Smallest fix: `^[*-]?\s*(\S+?)(?:\s+\(default\))?$` — it matches `* grok-4.6 (default)`,
`- grok-4.5`, and a bare `grok-4.5`.

## 8. MINOR — the unhandled-event counter is written and nothing reads it

`src/delegate_agent/harness_events.py:670-673`.

`unhandled_event_types` and `unhandled_event_types_truncated` have no reader anywhere in
`src/`. `malformed_lines` and `malformed_samples` have none either, though the samples do
reach `recentEvents` through the `stream.malformed` event. Plan I1 assigns the manifest and
snapshot surfacing to Lane I in wave 2, so this is scheduled rather than forgotten — but as
the branch stands, E10 ships as a counter nothing reads, which is precisely the failure mode
its own comment describes.

## 9. MINOR — droid custom-model selectors become positional and drop `id`

`src/delegate_agent/harness_discovery.py:1197-1208`.

Every custom model is now keyed `custom:<Display-Name>-<index>` and `item["id"]` is ignored,
so an entry that previously appeared under its own id disappears from the catalog, and
reordering `customModels` in droid settings renames every selector after the moved entry.
D3 specifies this, so it is intended, but it is a user-visible selector break: it wants a
CHANGELOG line and a note in `docs/cli-reference.md` telling operators to re-read
`delegate models droid`.

## 10. MINOR — grok `max_tokens` truncation is a bare failure, not an incomplete terminal

`src/delegate_agent/harness_events.py:1907-1912` and `:1370-1382`.

E5 asked for `max_tokens`, `max_turn_requests`, and `max_turns_reached` to be classified as
incomplete terminals. `max_turn_requests` and `max_turns_reached` reach
`PROVIDER_MAX_TURNS`; `max_tokens` records a plain `failed` terminal with no
`provider_terminal_state`, so the run record carries no `failureReason` distinguishing a
truncated answer from any other failure.

```
max_turn_requests | terminal=failed provider=provider_max_turns
max_tokens        | terminal=failed provider=None
```

Smallest fix: add `max_tokens`/`maxtokens` to `_PROVIDER_MAX_TURNS_CODES` (the state is
"the provider truncated the turn" either way) and delete `_grok_stop_reason_incomplete`,
which then has no remaining case.

## 11. NIT — the "option after the prompt" warning never fires for resume or followup

`src/delegate_agent/cli_parser.py:2183-2192`, wired at `:1214` and `:1364` only.

`parse_resume` and `parse_followup` also call `parse_prompt_tail` but discard
`tail.warnings`, so `delegate followup <handle> "text" --model x` still silently absorbs the
flag. Verified working for launch subcommands:
`dry-run cursor safe "hello" --model foo` →
`"option after the prompt is treated as prompt text: --model."`

## 12. NIT — `dry_run_payload` treats a missing config as mail-enabled

`src/delegate_agent/cli.py:416`. `_mail.launch_enabled(mode, config or {})` resolves to
`True` because `mail_enabled({})` defaults to on, so a caller passing `config=None` gets
mail argv wiring on a work dry-run regardless of the machine's actual config.

## 13. NIT — the mail-suffix strip depends on the suffix being last in the framed prompt

`src/delegate_agent/request_build.py:743-748`; consumers at `mail.py:69` and `cli.py:420`
both use `removesuffix` / `endswith`.

`dirty_note` is appended after `mail_suffix`. Today they cannot co-occur (`dirty_note` is
safe-mode only, the mail suffix is work-mode only), so the strip works — but one reordering
turns both call sites into silent no-ops and the degraded launch keeps telling the model to
run `delegate mail inbox`. Assert the invariant, or strip by search rather than by suffix.

## 14. NIT — the opencode unhandled counter files a part-shape mismatch under the event type

`src/delegate_agent/harness_events.py:1427` and `:1441`. An opencode event whose `type` is
known but whose `part.type` disagrees is counted under the known type name, so the counter
reports a handled type as unhandled and hides which half of the pair changed.

---

## Verified OK

- **Cursor read-only boundary.** `dry-run cursor safe` emits
  `--workspace <ws> -p --trust --mode ask --model <m> --output-format stream-json` with
  `promptTransport: stdin` and no prompt anywhere in argv. `--mode ask` ("Q&A style … 
  (read-only)") and `--add-dir` both exist in the installed `cursor-agent --help`.
  Work mode keeps `--approve-mcps --force` and emits no mode flag.
- **Claude `--permission-prompts none`.** Gated on `discovery.harnesses.claude.capabilities
  .permissionPrompts`, which `_probe_claude` sets from the observed help text. The flag and
  its `"none"` value are present in the installed claude 2.1.263 help, matching the fixture.
- **Codex flag move.** `-c web_search="live"` and `-c approval_policy="never"` join
  `sandbox_tokens`, which are emitted after `exec` on both the fresh and resume branches;
  `--sandbox read-only` still governs safe, and the bypass flags remain gated on
  `mode == work` plus an explicit policy key. The audit's clap probes back the claim that the
  old flags were parsed and dropped.
- **omp boundaries.** Safe still emits the full lockdown (`--tools read --no-extensions
  --no-skills --no-rules --no-lsp --approval-mode always-ask`); `--approval-mode yolo`
  appears only on work. `--cwd` is in `WORKSPACE_FLAG_BY_ENGINE`, so
  `replace_workspace_arg_in_argv` rewrites it to the isolated copy — safe mode does not point
  at the source tree.
- **Transport move does not corrupt argv.** `safe_workspace._isolated_prompt_argv:189` and
  `worktree_execution._request_for_execution_workspace:606-611` both branch on
  `PROMPT_TRANSPORT_ARGV`, so nothing rewrites `argv[-1]` for cursor or omp any more.
  `redacted_prompt_argv` has one caller (kimi) and kimi's argv still ends with the prompt.
- **Mail default flip fails open.** `prepare_launch_storage` catches `MailError`;
  `prepare_mail_storage` converts `OSError` into one; `MAIL_SANDBOX_ROWS` is asserted
  complete against `KNOWN_ENGINES` at import, so `_mail_scope` cannot `KeyError`;
  `mail_push` stays default-`False`. I found no path where a machine that never asked for
  mail fails a launch. `--no-mail` suppresses the suffix (verified by dry-run) and the mail
  suffix is independent of the new skill-preamble switch (`request_build.py:3634-3641` uses
  the un-widened `skip_skill_preamble`).
- **Pinned Claude alias preflight.** `--model best --continuity-mode pinned` fails with
  `unsupported_continuity_mode`; `--model opus` proceeds.
- **Effort vocabularies.** `pi --reasoning-effort auto` is refused; `omp` accepts it and
  emits `--thinking auto`.
- **Cross-lane seams.** `structured_output.native_schema_eligible` is the single decision
  point for both `workflows/runtime._native_schema` and the new claude `--output-schema`
  preflight; the `_claude_result_text` shim is gone from `runner.py` in favour of
  `harness_events.claude_result_text`; E owns only the cursor branch of `stall_watchdog.py`
  and B the rest, with no overlap; `effective_stall_seconds` is applied once in
  `cli.py:1847` and the `DELEGATE_STALL_MINUTES` env override still wins over the disable.
- **Tests and lint.** 749 tests plus 389 subtests pass across `test_harness_events.py`,
  `test_lane_a_transport.py`, `test_lane_b_lifecycle.py`, `test_lane_m_mail_defaults.py`,
  `test_lane_r_public_surface.py`, `test_lane_s_structured_retry.py`, `test_sandbox_bwrap.py`,
  `test_stall_watchdog.py`, `test_stall_watchdog_cursor.py`, `test_engine_argv.py`,
  `test_mail_gating.py`, `test_provider_outcomes.py`, `test_runner_capture.py`,
  `test_delegate_validation.py`, `test_delegate_help_cli.py`. `ruff check src/delegate_agent`
  is clean. The full suite was not run (a gate holds the checkout).
- **Relaxation discipline.** Every test relaxation I found carries a justification in its
  commit body: the `events == []` assertions (commit `15478e4`), the deleted
  `test_omp_rejects_flag_like_prompt_on_argv_transport` and the cursor→kimi exemplar moves
  (`fb59032`, and the omp probe commit), and the mail-gating manifest assertion (`fb59032`).
