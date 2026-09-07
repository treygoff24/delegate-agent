I'll load the review, beads, and repo-agent skills first, then map the branch diff against the plan without mutating anything.

Skills loaded: beads, code-review (pin merge-base + spec), caveman-review, talking-to-trey. Output stays the operator’s findings list. Next I’ll pin the diff base and read the plan.

Git call was blocked; retrying with smaller commands, then I’ll read the mail lane and the source diff.

Shell and `.git` are blocked in this safe isolation, so I’ll review from the plan and current sources, then flag anything I couldn’t verify with git or pytest.

I'll pin the remaining line numbers and try git/pytest if the shell is available this pass.

**Status:** completed  
**What:** Read-only defect review of `feat/harness-compat-audit` vs `PLAN.md` v2. No edits. Shell was rejected in this isolation, so no `git merge-base`/`git log`/`pytest`/`dry-run`. Findings are from current sources vs the plan. Uncommitted beads/handoff files were not treated as the branch.  
**Reviewed:** `docs/audits/2026-09-07-harness-compat/PLAN.md`, `mail.md`, `src/delegate_agent/{mail_core,mail,cli,request_build,argv_builders,stall_watchdog,sandbox_bwrap,harness_events,structured_output,workflows/runtime,prompt_transport,config,reasoning}.py`, focused tests.  
**Verification:** none executable (shell rejected).  
**Risks:** finding 1 needs a live argv inspect; finding 2 is the B4 CLI-only seam; Lane I still deferred.

---

1. **major** — `src/delegate_agent/mail_core.py:367-376`  
Codex isolated-work mail grant is spliced before the last token only when `prompt_transport == "argv"`. Production Codex is stdin (`request_build.py:2968`, `argv_builders.py:736-737` ends with `-`). Live launch (`cli.py:425-431`, `worktree_execution.py:658-669`) therefore takes `updated.extend(flags)` and puts `-c sandbox_workspace_write.writable_roots=[...]` **after** `-`. Mail is default on (`config.py:202-204`) and Lane M tests set `isolation.work: worktree` (`tests/test_lane_m_mail_defaults.py:49-72`) but only assert substring presence. Unit grants use default `prompt_transport="argv"` and a fake trailing `"prompt"` (`tests/test_mail_gating.py:188-199`). The only stdin case is the no-grant read-only warning (`tests/test_lane_m_mail_defaults.py:118-131`).  
Failing input: `delegate --json dry-run --cwd <git-repo> --isolation worktree codex work x` (or config `isolation.work: worktree`). Inspect argv: `-` then `-c` `sandbox_workspace_write.writable_roots=...`. If clap rejects extra tokens, the launch dies; if they become prompt text, the sandbox grant never applies and `.delegate/mail` is unreachable inside workspace-write.  
Smallest fix: splice the `-c` pair before the last token whenever that token is `-` (same as the argv-prompt branch). Assert `argv[-3:]` is `["-c", writable_roots, "-"]` with `prompt_transport="stdin"`.

2. **major** — `src/delegate_agent/cli.py:444-453` and `src/delegate_agent/workflows/runtime.py:3188-3242`  
B4’s `effective_stall_seconds` (`stall_watchdog.py:101-115`) disables the default 8‑minute stall for kimi/devin only when `timeout_seconds is not None`. That rewrite runs only after `request_from_parsed` in `cli.py:1847`. Workflow children are `delegate --json --group <wf> run --input-json` and the payload **omits `timeout`** even though `timeout` is a `RUN_INPUT_KEYS` field (`request_build.py:124`). Supervisor timeout is `communicate()` only. Child `Request.timeout` stays `None` → stall stays 8 minutes. Kimi/devin are silent during tools by design.  
Failing input: workflow `ctx.agent("...", engine="kimi", timeout=3600)` then a tool silent >8 min while the parent is still waiting.  
Smallest fix: copy `int(timeout)` into the child input-json when set, or apply `effective_stall_seconds` in the runner from the actual deadline.

3. **major** — `src/delegate_agent/structured_output.py:25-32` vs `src/delegate_agent/request_build.py:3009-3028`  
S1 is supposed to bound the **serialized `--json-schema` argv token**. `native_schema_eligible` measures `json.dumps(parsed)`. Direct Claude inlines the **file text** (`_claude_request_parts` reads the path and `build_claude_argv` puts it on `--json-schema`). Tests write schemas with `json.dumps(schema)` (`tests/test_lane_a_transport.py:758-763`), so they never see pretty-print.  
Failing input: a pretty-printed object-root schema whose `json.dumps(json.loads(text))` is &lt; 120000 bytes but `len(file.encode())` ≥ 120000. Preflight passes; Linux `MAX_ARG_STRLEN` (131072) can then fail the exec. Inverse: compact file under the limit that `dumps()` inflates over it is refused wrongly.  
Smallest fix: measure `len(actual_argv_token.encode("utf-8"))` (file bytes or `output_schema_text`), not a re-serialized object. Keep the object-root check on the parsed dict.

4. **minor** — `src/delegate_agent/sandbox_bwrap.py:78-85` and `:435-438`  
Kimi always returns `$HOME/.kimi-code` (or `KIMI_CODE_HOME`) and `build_bwrap_argv` rw-binds it with no existence check. RO candidates require `os.path.isdir` (`:488-490`). Tests always `mkdir` the home (`tests/test_sandbox_bwrap.py:262-268`). Missing dir → bwrap `--bind` fails closed at preflight. Default backend is copy, so this is experimental-bwrap-only.  
Failing input: `DELEGATE_SAFE_BACKEND=bwrap` + kimi safe, no `~/.kimi-code`.  
Smallest fix: skip the rw-bind when the path does not exist (same as RO), or mkdir before bind.

5. **minor** — `src/delegate_agent/harness_events.py:327-337` and `:1376-1384`  
E5 asked to classify `max_tokens` with `max_turn_requests`. `_PROVIDER_MAX_TURNS_CODES` has `max_turn_requests`/`maxturnrequests` only. `_ingest_grok_end` maps `max_tokens` to generic `failed` with no `provider_terminal_state`. Tests pin that (`tests/test_harness_events.py:1443-1459`) and only type `max_turn_requests` as `provider_max_turns` (`:1456-1459`). Run still fails (not a silent success).  
Failing input: grok stream `{"type":"end","stopReason":"max_tokens"}`.  
Smallest fix: add `max_tokens`/`maxtokens` to `_PROVIDER_MAX_TURNS_CODES` (same shape as the turn-request pair).

6. **minor** — `src/delegate_agent/harness_events.py:672` and `:906-917`  
E10 counts `unhandled_event_types`; I1 was supposed to surface it. No reads in `runner.py` / snapshot. `malformed_lines` at least bump `structured_events_seen` and emit `stream.malformed`. This counter is a dead field until Lane I.  
Failing input: any unused event type, e.g. kimi `{"role":"meta","type":"session.resume_hint"}`.  
Smallest fix: emit `unhandledEventTypes` on the run record (Lane I), or drop the counter until then.

7. **nit** — `src/delegate_agent/reasoning.py:90-94`  
`PI_THINKING_LEVELS = OMP_NATIVE_EFFORTS` now includes `auto`. Config validation still uses `PI_NATIVE_EFFORTS` for pi aliases (`config.py:1133-1136`). CLI pi `--reasoning-effort auto` is still rejected. Workflow parse uses `PI_THINKING_LEVELS` for every engine (`workflows/script.py:250`), so `effort="auto"` on a non-omp stage parses and fails later in the child.  
Smallest fix: keep `PI_THINKING_LEVELS` as the pi tuple; use `OMP_NATIVE_EFFORTS` only on omp alias/workflow paths.

8. **nit** — `src/delegate_agent/mail.py:65`  
On storage failure, `prepare_launch_storage` sets `config["mail"] = {"enabled": False}` in place. One CLI process is one launch; a long-lived in-process loop sharing that dict would stick.  
Smallest fix: a request-local flag, not a mutation of the loaded config.

Could not check commit-body justifications for test relaxations (`git log --format='%h %s%n%b' <base>..HEAD` blocked). The mail-grant and Claude-schema tests above encode the non-production / dumps() shape without a visible justification in the test comments.

---

**Verified OK** (source inspection, not pytest)

- Cursor `--mode ask` on safe and `call --read-only`; work still `--force` / `--approve-mcps`; single `-p`.
- Codex `-c web_search="live"` and `-c approval_policy="never"` after `exec`; safe stays read-only sandbox + never.
- Cursor/omp stdin; kimi remains argv; `CURSOR_PROMPT_REDACTION` / `OMP_PROMPT_REDACTION` gone.
- Claude `--permission-prompts none` only when discovery `permissionPrompts` is true.
- Kimi home excluded from RO candidates for the selected engine; other engines do not bind it; workspace intersection refused.
- `--no-mail` is a global flag; `cli.py:1682` sets `mail.enabled` false before `request_build`; suffix gate uses outer `skip_skill_preamble` (pass-through only), so default-off skill preamble still injects mail; `--no-mail --mail-push` → `mail_push_disabled`.
- `claude_result_text` lives in `harness_events.py:566`; `runner.py:4542` calls it; no leftover `_claude_result_text` shim. `is_error` is independent.
- `native_schema_eligible` uses explicit `type == "object"` (not `_is_object_node`); direct Claude array-root → `schema_not_native`; Codex direct path still `normalize_codex_schema`.
- Error events record failed terminals; grok usage sealing + last buffer at `end`; terminal reasons redacted in `_record_terminal_event`.
- Mail sandbox table covers `KNOWN_ENGINES`; cursor work is not given a sandbox just for mail.
- Omp work `--approval-mode yolo`; omp safe `always-ask`; kimi pass-through pins `--output-format text`.
- GROK efforts `low/medium/high/xhigh`; skill preamble default off; Devin 3000.4 pin retained; safe still refused.
- `OPENCODE_DISABLE_CLAUDE_CODE=1` only on read-only opencode launches.

Lane I (docs/`unhandledEventTypes`/doctor `codex.profile`) is not in this wave; PLAN I2 still lists cursor `--mode plan`.

I'll load the review, beads, and repo-agent skills first, then map the branch diff against the plan without mutating anything.Skills loaded: beads, code-review (pin merge-base + spec), caveman-review, talking-to-trey. Output stays the operator’s findings list. Next I’ll pin the diff base and read the plan.Git call was blocked; retrying with smaller commands, then I’ll read the mail lane and the source diff.Shell and `.git` are blocked in this safe isolation, so I’ll review from the plan and current sources, then flag anything I couldn’t verify with git or pytest.I'll pin the remaining line numbers and try git/pytest if the shell is available this pass.**Status:** completed  
**What:** Read-only defect review of `feat/harness-compat-audit` vs `PLAN.md` v2. No edits. Shell was rejected in this isolation, so no `git merge-base`/`git log`/`pytest`/`dry-run`. Findings are from current sources vs the plan. Uncommitted beads/handoff files were not treated as the branch.  
**Reviewed:** `docs/audits/2026-09-07-harness-compat/PLAN.md`, `mail.md`, `src/delegate_agent/{mail_core,mail,cli,request_build,argv_builders,stall_watchdog,sandbox_bwrap,harness_events,structured_output,workflows/runtime,prompt_transport,config,reasoning}.py`, focused tests.  
**Verification:** none executable (shell rejected).  
**Risks:** finding 1 needs a live argv inspect; finding 2 is the B4 CLI-only seam; Lane I still deferred.

---

1. **major** — `src/delegate_agent/mail_core.py:367-376`  
Codex isolated-work mail grant is spliced before the last token only when `prompt_transport == "argv"`. Production Codex is stdin (`request_build.py:2968`, `argv_builders.py:736-737` ends with `-`). Live launch (`cli.py:425-431`, `worktree_execution.py:658-669`) therefore takes `updated.extend(flags)` and puts `-c sandbox_workspace_write.writable_roots=[...]` **after** `-`. Mail is default on (`config.py:202-204`) and Lane M tests set `isolation.work: worktree` (`tests/test_lane_m_mail_defaults.py:49-72`) but only assert substring presence. Unit grants use default `prompt_transport="argv"` and a fake trailing `"prompt"` (`tests/test_mail_gating.py:188-199`). The only stdin case is the no-grant read-only warning (`tests/test_lane_m_mail_defaults.py:118-131`).  
Failing input: `delegate --json dry-run --cwd <git-repo> --isolation worktree codex work x` (or config `isolation.work: worktree`). Inspect argv: `-` then `-c` `sandbox_workspace_write.writable_roots=...`. If clap rejects extra tokens, the launch dies; if they become prompt text, the sandbox grant never applies and `.delegate/mail` is unreachable inside workspace-write.  
Smallest fix: splice the `-c` pair before the last token whenever that token is `-` (same as the argv-prompt branch). Assert `argv[-3:]` is `["-c", writable_roots, "-"]` with `prompt_transport="stdin"`.

2. **major** — `src/delegate_agent/cli.py:444-453` and `src/delegate_agent/workflows/runtime.py:3188-3242`  
B4’s `effective_stall_seconds` (`stall_watchdog.py:101-115`) disables the default 8‑minute stall for kimi/devin only when `timeout_seconds is not None`. That rewrite runs only after `request_from_parsed` in `cli.py:1847`. Workflow children are `delegate --json --group <wf> run --input-json` and the payload **omits `timeout`** even though `timeout` is a `RUN_INPUT_KEYS` field (`request_build.py:124`). Supervisor timeout is `communicate()` only. Child `Request.timeout` stays `None` → stall stays 8 minutes. Kimi/devin are silent during tools by design.  
Failing input: workflow `ctx.agent("...", engine="kimi", timeout=3600)` then a tool silent >8 min while the parent is still waiting.  
Smallest fix: copy `int(timeout)` into the child input-json when set, or apply `effective_stall_seconds` in the runner from the actual deadline.

3. **major** — `src/delegate_agent/structured_output.py:25-32` vs `src/delegate_agent/request_build.py:3009-3028`  
S1 is supposed to bound the **serialized `--json-schema` argv token**. `native_schema_eligible` measures `json.dumps(parsed)`. Direct Claude inlines the **file text** (`_claude_request_parts` reads the path and `build_claude_argv` puts it on `--json-schema`). Tests write schemas with `json.dumps(schema)` (`tests/test_lane_a_transport.py:758-763`), so they never see pretty-print.  
Failing input: a pretty-printed object-root schema whose `json.dumps(json.loads(text))` is &lt; 120000 bytes but `len(file.encode())` ≥ 120000. Preflight passes; Linux `MAX_ARG_STRLEN` (131072) can then fail the exec. Inverse: compact file under the limit that `dumps()` inflates over it is refused wrongly.  
Smallest fix: measure `len(actual_argv_token.encode("utf-8"))` (file bytes or `output_schema_text`), not a re-serialized object. Keep the object-root check on the parsed dict.

4. **minor** — `src/delegate_agent/sandbox_bwrap.py:78-85` and `:435-438`  
Kimi always returns `$HOME/.kimi-code` (or `KIMI_CODE_HOME`) and `build_bwrap_argv` rw-binds it with no existence check. RO candidates require `os.path.isdir` (`:488-490`). Tests always `mkdir` the home (`tests/test_sandbox_bwrap.py:262-268`). Missing dir → bwrap `--bind` fails closed at preflight. Default backend is copy, so this is experimental-bwrap-only.  
Failing input: `DELEGATE_SAFE_BACKEND=bwrap` + kimi safe, no `~/.kimi-code`.  
Smallest fix: skip the rw-bind when the path does not exist (same as RO), or mkdir before bind.

5. **minor** — `src/delegate_agent/harness_events.py:327-337` and `:1376-1384`  
E5 asked to classify `max_tokens` with `max_turn_requests`. `_PROVIDER_MAX_TURNS_CODES` has `max_turn_requests`/`maxturnrequests` only. `_ingest_grok_end` maps `max_tokens` to generic `failed` with no `provider_terminal_state`. Tests pin that (`tests/test_harness_events.py:1443-1459`) and only type `max_turn_requests` as `provider_max_turns` (`:1456-1459`). Run still fails (not a silent success).  
Failing input: grok stream `{"type":"end","stopReason":"max_tokens"}`.  
Smallest fix: add `max_tokens`/`maxtokens` to `_PROVIDER_MAX_TURNS_CODES` (same shape as the turn-request pair).

6. **minor** — `src/delegate_agent/harness_events.py:672` and `:906-917`  
E10 counts `unhandled_event_types`; I1 was supposed to surface it. No reads in `runner.py` / snapshot. `malformed_lines` at least bump `structured_events_seen` and emit `stream.malformed`. This counter is a dead field until Lane I.  
Failing input: any unused event type, e.g. kimi `{"role":"meta","type":"session.resume_hint"}`.  
Smallest fix: emit `unhandledEventTypes` on the run record (Lane I), or drop the counter until then.

7. **nit** — `src/delegate_agent/reasoning.py:90-94`  
`PI_THINKING_LEVELS = OMP_NATIVE_EFFORTS` now includes `auto`. Config validation still uses `PI_NATIVE_EFFORTS` for pi aliases (`config.py:1133-1136`). CLI pi `--reasoning-effort auto` is still rejected. Workflow parse uses `PI_THINKING_LEVELS` for every engine (`workflows/script.py:250`), so `effort="auto"` on a non-omp stage parses and fails later in the child.  
Smallest fix: keep `PI_THINKING_LEVELS` as the pi tuple; use `OMP_NATIVE_EFFORTS` only on omp alias/workflow paths.

8. **nit** — `src/delegate_agent/mail.py:65`  
On storage failure, `prepare_launch_storage` sets `config["mail"] = {"enabled": False}` in place. One CLI process is one launch; a long-lived in-process loop sharing that dict would stick.  
Smallest fix: a request-local flag, not a mutation of the loaded config.

Could not check commit-body justifications for test relaxations (`git log --format='%h %s%n%b' <base>..HEAD` blocked). The mail-grant and Claude-schema tests above encode the non-production / dumps() shape without a visible justification in the test comments.

---

**Verified OK** (source inspection, not pytest)

- Cursor `--mode ask` on safe and `call --read-only`; work still `--force` / `--approve-mcps`; single `-p`.
- Codex `-c web_search="live"` and `-c approval_policy="never"` after `exec`; safe stays read-only sandbox + never.
- Cursor/omp stdin; kimi remains argv; `CURSOR_PROMPT_REDACTION` / `OMP_PROMPT_REDACTION` gone.
- Claude `--permission-prompts none` only when discovery `permissionPrompts` is true.
- Kimi home excluded from RO candidates for the selected engine; other engines do not bind it; workspace intersection refused.
- `--no-mail` is a global flag; `cli.py:1682` sets `mail.enabled` false before `request_build`; suffix gate uses outer `skip_skill_preamble` (pass-through only), so default-off skill preamble still injects mail; `--no-mail --mail-push` → `mail_push_disabled`.
- `claude_result_text` lives in `harness_events.py:566`; `runner.py:4542` calls it; no leftover `_claude_result_text` shim. `is_error` is independent.
- `native_schema_eligible` uses explicit `type == "object"` (not `_is_object_node`); direct Claude array-root → `schema_not_native`; Codex direct path still `normalize_codex_schema`.
- Error events record failed terminals; grok usage sealing + last buffer at `end`; terminal reasons redacted in `_record_terminal_event`.
- Mail sandbox table covers `KNOWN_ENGINES`; cursor work is not given a sandbox just for mail.
- Omp work `--approval-mode yolo`; omp safe `always-ask`; kimi pass-through pins `--output-format text`.
- GROK efforts `low/medium/high/xhigh`; skill preamble default off; Devin 3000.4 pin retained; safe still refused.
- `OPENCODE_DISABLE_CLAUDE_CODE=1` only on read-only opencode launches.

Lane I (docs/`unhandledEventTypes`/doctor `codex.profile`) is not in this wave; PLAN I2 still lists cursor `--mode plan`.
