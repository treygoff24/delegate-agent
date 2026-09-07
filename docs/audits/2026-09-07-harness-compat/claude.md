# Claude Code compatibility audit

Latest version: **2.1.263** (npm `latest`/`next`, published 2026-09-06T02:07:58Z, <https://registry.npmjs.org/@anthropic-ai/claude-code>) · Installed here: **2.1.263** (`/home/trey-agent/bin/claude --version` → `2.1.263 (Claude Code)`)
Delegate assumptions checked: 42 · BROKEN: 1 · LATENT: 8 · SIMPLIFY: 5 · OK: 28

The installed binary **is** the latest release. The upstream CHANGELOG's top heading is `## 2.1.263` ("Bug fixes and reliability improvements"), so there are **zero** newer entries to diff against. Note the npm `stable` dist-tag lags at 2.1.236; delegate runs `latest`. Every flag delegate emits for Claude still exists and still behaves as delegate expects. The single hard defect is the pre-existing `--json-schema` object-root bug, which I reproduced live on 2.1.263 at zero cost.

Local invocations in this report ran the real binary directly, not `delegate`, except for `dry-run`, `models`, and `capabilities`, which are non-executing. Two probes made real Haiku calls (about 1 cent each) to capture the live `result` object shape; everything else was free (`--help`, or an API-400 path that bills nothing).

## BROKEN

- **[B1] A workflow schema with a non-object root kills every Claude lane before the model runs.** delegate: `src/delegate_agent/workflows/runtime.py:3759-3760` (`_native_schema` returns the schema unchanged for `claude`), fed to `--json-schema` at `src/delegate_agent/argv_builders.py:333`; `src/delegate_agent/workflows/schema.py:68` explicitly admits `"array"` as a valid root type, so the workflow validates and then dies at launch. Harness truth: Claude Code turns `--json-schema` into a custom tool's `input_schema`, and the Messages API pins that field to the literal `"type": "object"` (<https://platform.claude.com/docs/en/api/messages>, Tool object). Still live on 2.1.263.

  Repro, free (the request is rejected before inference; `total_cost_usd: 0`):
  ```sh
  claude -p --output-format json --model haiku --max-turns 1 \
    --json-schema '{"type":"array","items":{"type":"object","properties":{"a":{"type":"string"}},"required":["a"]}}' 'x'
  # rc=1, result: "API Error: 400 tools.8.custom.input_schema.type: Input should be 'object'"
  ```
  Unit-level repro, no harness at all:
  ```sh
  PYTHONPATH=src python3 -c "
  from delegate_agent.workflows import runtime, schema
  arr = {'type':'array','items':{'type':'object','properties':{'a':{'type':'string'}},'required':['a'],'additionalProperties':False}}
  print(runtime._native_schema('claude', arr))   # returns the array schema
  print(runtime._native_schema('codex', arr))    # returns None (correct)
  print(schema.validate_schema_subset(arr))      # None -> accepted
  "
  ```
  Smallest fix: in `_native_schema`, gate the `claude` branch on an object root and return `None` otherwise, reusing the predicate that already exists at `src/delegate_agent/structured_output.py:29` (`_is_object_node`). Codex's branch already does exactly this. See the fix-option evaluation below.

### Evaluating the three fix options in `docs/issues/2026-09-07-claude-json-schema-non-object.md`

Two preconditions the issue assumed, now confirmed against 2.1.263:

- `--json-schema` **still exists** and is documented. `claude --help`: `--json-schema <schema>  JSON Schema for structured output validation.` The CLI reference adds that Claude Code "exits with an error on an invalid schema" (<https://code.claude.com/docs/en/cli-reference>).
- The object-root constraint is **not documented anywhere in Claude Code's own docs**. The Agent SDK structured-outputs page arguably implies the opposite, stating the SDK "supports standard JSON Schema features including all basic types (object, array, string, number, boolean, null)" (<https://code.claude.com/docs/en/agent-sdk/structured-outputs>). The constraint is only discoverable from the Messages API tool spec. Treat it as undocumented-but-enforced, and do not expect a future release to relax it.

| Option | Verdict |
| --- | --- |
| 1. Return `None` for non-object roots on `claude` | **Recommended.** One conditional, no consumer changes, and it routes those stages onto the prompt-and-parse path every non-Codex engine already uses. Structurally identical to the Codex branch directly above it. |
| 2. Wrap the root in a `{"result": <schema>}` envelope | Workable but costlier than the issue estimates. The unwrap has to happen in `_parse_claude_call_json` (`src/delegate_agent/runner.py:4538`), which reads `result["result"]` as **text**, so the envelope arrives as the string `{"result":[...]}` and must be parsed then unwrapped. It also has to survive the structured-retry path, which resumes the session (`STRUCTURED_RESUME_ENGINES` at `workflows/runtime.py:53`) and re-shows the schema in the correction prompt. Only worth it if native enforcement on array-rooted schemas is genuinely wanted. |
| 3. Preflight non-object roots | **Do this regardless**, but it must be Claude-specific, not added to `validate_schema_subset`. Array roots are legitimate for Codex-via-prompt-parse and every other engine; a blanket rejection would break workflows that work today. The issue text already allows for this ("or a Claude-specific preflight beside the enum rule"), and the enum precedent at `workflows/schema.py:95-100` is the right shape to copy. |

Options 1 and 3 together are the complete fix. Option 2 is optional scope.

## LATENT

- **[L1] `ultracode` effort is unreachable through delegate.** `src/delegate_agent/reasoning.py:73` pins `CLAUDE_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")`, and `docs/cli-reference.md:321` repeats that list. The CLI reference documents a sixth value: "`low`, `medium`, `high`, `xhigh`, `max`, or `ultracode`" (<https://code.claude.com/docs/en/cli-reference>), and the binary silently accepts it (`claude --effort ultracode --help` prints no warning). Delegate rejects it at preflight:
  ```
  python3 bin/delegate.py --json dry-run claude work --reasoning-effort ultracode "t"
  # ok=false, error=unsupported_reasoning_effort
  ```
  The discovery path cannot rescue this either: the binary's own `Valid values:` warning omits `ultracode`, so `parse_claude_efforts` is blind to it too. Fixing this means hardcoding the value, which is why it is latent rather than broken.

- **[L2] Native-file persona detection hangs on one line of unrelated help prose.** `src/delegate_agent/harness_discovery.py:1422-1428` sets `personaTransports["native-file"]` by string-matching `--append-system-prompt-file` or `--append-system-prompt[-file]` in help output. On 2.1.263 the literal long spelling **never appears**, and the bracket form appears exactly once, inside the `--bare` option's description:
  ```
  claude --help | grep -n -- "append-system-prompt"
  # 25:  --append-system-prompt <prompt>
  # 52:      --append-system-prompt[-file], --add-dir (CLAUDE.md dirs), ...   <- the --bare blurb
  ```
  A reword of the `--bare` description silently flips persona transport to unsupported even though the flag works. The flag itself is fine and documented ("Load additional system prompt text from a file and append to the default prompt", CLI reference); it is simply hidden from `--help`. Verified working: `claude -p --append-system-prompt-file /var/tmp/nonexistent-xyz.txt 'hi'` → `Error: Append system prompt file not found: ...` (rc 1), versus `error: unknown option` for a genuinely absent flag.

- **[L3] Effort discovery parses an undocumented warning string.** `parse_claude_efforts` (`harness_discovery.py:1234`) regexes `Valid values:\s*([^\r\n.]+)` out of the response to a sentinel `--effort` value. It works today:
  ```
  claude --effort __delegate_probe__ --help 2>&1 | grep -i "valid values"
  # Warning: Unknown --effort value '__delegate_probe__' — ignoring it and using the default effort. Valid values: low, medium, high, xhigh, max.
  ```
  This wording appears in no documentation. The code comment already flags the risk and the failure mode degrades to an error record rather than wrong data, so this is acceptable as designed. Recording it so the synthesizer knows it is a watch item, not a verified-stable contract.

- **[L4] `--continuity pinned` fails on every Claude model alias.** Claude reports the fully-dated served model id, so a pinned run started with any alias trips `served_model_mismatch` and the accumulator marks the run failed. Free repro:
  ```sh
  PYTHONPATH=src python3 -c "
  from delegate_agent import harness_events as he; import json
  acc = he.StreamAccumulator(harness='claude', requested_model='haiku', continuity_mode='pinned')
  acc.ingest_line(json.dumps({'type':'system','subtype':'init','session_id':'s1','model':'claude-haiku-4-5-20251001'}))
  acc.finish_stream(); print(acc.continuity_violation, acc.terminal_status)"
  # {'reason': 'served_model_mismatch', 'requestedModel': 'haiku', 'servedModel': 'claude-haiku-4-5-20251001', ...} failed
  ```
  This hits every documented Claude alias, since none of them ever equal a served id: `opus`, `sonnet`, `haiku`, `fable`, `best`, `opusplan`, `opus[1m]`, `sonnet[1m]` (<https://code.claude.com/docs/en/model-config>). Latent because `continuityMode` defaults to `fungible`. A fix has a ready ingredient, see [S4].

- **[L5] Structured output is read from the result text, not the field built for it.** `src/delegate_agent/runner.py:4538` requires `result["result"]` to be a string and returns it; `structured_output` is never read anywhere in `src/`. The duplication is real but undocumented, confirmed against a live call:
  ```
  result (text):       '{"ok":true}'
  structured_output:   {'ok': True}
  ```
  The headless doc only promises the field: "the structured output in the `structured_output` field" (<https://code.claude.com/docs/en/headless>). Nothing states the JSON is also echoed into `result`. Delegate works today and relies on behavior the vendor never committed to.

- **[L6] The bundled Claude model table is stale, and it is the only source there is.** `src/delegate_agent/bundled_models.py` lists `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`, `claude-fable-5`. Missing: `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5-1`, and every alias. Per the model-config doc, `opus` now resolves to Opus 5 and `sonnet` to Sonnet 5 on the Anthropic API, and "Unless you set `ANTHROPIC_DEFAULT_FABLE_MODEL`, the `fable` alias resolves to Fable 5.1". Because `LIVE_UNSUPPORTED_ENGINES = frozenset({"claude"})` (`model_discovery.py:17`), no live probe can ever correct it:
  ```
  python3 bin/delegate.py --json models claude --live
  # "live": {"reason": "claude has no non-interactive model enumeration", "supported": false}
  # models: claude-fable-5, claude-haiku-4-5, claude-opus-4-8, claude-sonnet-4-6  (all source "bundled")
  ```
  Not BROKEN, because model ids pass through unvalidated. I confirmed `opus`, `sonnet`, `claude-opus-5`, `opus[1m]`, `fable`, `best`, and `opusplan` all produce `ok: true` dry-runs with the value forwarded verbatim to `--model`. The table only misleads whoever reads `delegate models claude`.

- **[L7] `--output-format json` returns a top-level array, and the vendor doc says otherwise.** `runner.py:4528` rejects any non-list payload as `call_output_invalid`. That matches the binary: a successful run emitted a 64-element array (`system/init`, `system/status`, 40+ `system/thinking_tokens`, `assistant`, `user`, `rate_limit_event`, then `result`). But the headless doc's own examples read it as a single object (`jq -r '.result'`, `jq '.structured_output'`, `jq -r '.session_id'`). Delegate is right about the binary and wrong about the documentation, which is the fragile direction: if a release ever makes the doc true, delegate fails closed on every Claude call. Cheap hardening is to accept both shapes.

- **[L8] Claude safe mode is not hermetic, and delegate says so.** `docs/cli-reference.md:316` states plainly that delegate "does not prove hooks, plugins, user settings, output styles, or other non-MCP customization surfaces are disabled". Confirmed live. Reading the `system/init` event from a safe-mode argv:
  ```
  tools: ['Bash', 'Glob', 'Grep', 'Read', 'StructuredOutput']
  permissionMode: plan     mcp_servers: []     plugins: []     skills count: 59
  ```
  The tool and MCP lockdowns hold exactly as intended. Hooks still fire (`system/hook_started` events for `SessionStart` appeared in a separate stream-json probe) and 59 skills load. The documentation is honest, so this is not a defect; it is now avoidable, see [S3].

## SIMPLIFY

- **[S1] Read `structured_output` instead of re-parsing `result` text.** The field is present and already parsed into a native object on every structured call, and it is the only surface the vendor documents. `_parse_claude_call_json` (`runner.py:4521-4571`) could prefer it and keep the text read as fallback. This also removes the [L5] dependency. Estimated change: about 10 lines in one function, plus the fixture in `tests/test_pure_call.py:254`.

- **[S2] `--permission-prompts none` replaces the "nobody can answer" assumption.** New in 2.1.259, so available on the installed version, and verified accepted. Documented as: "Pass `none` when nobody can answer" — "anything that would prompt is denied automatically; the permission mode still decides everything else" (<https://code.claude.com/docs/en/cli-reference>). Delegate's safe and read-only call modes currently rely on `--permission-mode plan` plus a hand-maintained `CLAUDE_SAFE_ALLOWED_TOOLS` allowlist (`argv_builders.py:57-60`) and on there being no approver in `-p`. One flag makes the deny deterministic and documented, and would let the Bash allowlist shrink rather than grow.

- **[S3] `--restricted` closes the safe-mode hermeticity gap in [L8].** Verified accepted on 2.1.263. Per `claude --help` it "removes the built-in tools that run commands or code (Bash, PowerShell, REPL and the other code-running tools) and WebFetch unless `--tools` names them, and ignores user, project and local settings files", confines file tools to the working directories, and "refuses bypassPermissions". That is most of what `docs/cli-reference.md:316` currently disclaims. Pairing it with the existing `--safe-mode` (already used in pure mode) and `--disable-slash-commands` would let delegate make a hermeticity claim it cannot make today.

- **[S4] `modelUsage[<id>].canonicalModel` is a better resolved-model source.** `_claude_model_resolved` (`runner.py:4493`) picks the `modelUsage` key with the most output tokens, yielding the dated id. The real payload carries a canonical form alongside it:
  ```json
  "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 412, "canonicalModel": "claude-haiku-4-5", "provider": "firstParty", "costUSD": 0.0032582, "contextWindow": 200000}}
  ```
  Using `canonicalModel` gives a stabler resolved-model string, and feeding it into the continuity comparison narrows [L4] to the alias case alone.

- **[S5] The object-root predicate for [B1] already exists.** `structured_output._is_object_node` (`structured_output.py:29`) handles `type: "object"`, union types containing `object`, and `properties`/`patternProperties` presence. The Claude fix should call it rather than write a fresh `schema.get("type") != "object"` check, which would wrongly reject union-typed and type-less roots that the API accepts.

## OK (verified)

Verified against `claude --help` on 2.1.263, the CLI reference, and live invocation. All of these are correct as delegate has them.

**Flags delegate emits** (`build_claude_argv`, `argv_builders.py:241-337`), each present in `--help` and in the CLI reference: `-p`, `--output-format {text,json,stream-json}`, `--input-format text`, `--permission-mode {plan,auto,bypassPermissions}` (all three in the current choice list), `--tools`, `--allowedTools`, `--strict-mcp-config`, `--no-session-persistence`, `--safe-mode`, `--bare`, `--json-schema`, `--model`, `--effort`, `--resume`, `--append-system-prompt-file` (works; hidden from help, see [L2]).

- **stream-json needs no `--verbose`** — delegate omits it and the stream is emitted correctly. Verified: `printf 'x' | claude -p --output-format stream-json --input-format text ...` produced 20 NDJSON lines.
- **stdin prompt transport** — `promptTransport: "stdin"` in dry-run; verified the binary reads the prompt from stdin with `--input-format text`. `docs/cli-reference.md:317`'s claim that dry-run argv carries no prompt is correct (the dry-run argv ends at `--no-session-persistence`).
- **`--output-format json` array shape** — `runner.py:4528` requires a list; the binary emits one. Correct, with the caveat in [L7].
- **`is_error` is the right failure key** — a 400 produced `is_error: true` alongside `subtype: "success"`. `_parse_claude_call_json:4562` and `_ingest_result_event:944` both key on `is_error`, not `subtype`. Keying on `subtype` would have been wrong.
- **`usage` field casing** — real payload uses snake_case `input_tokens`/`output_tokens`; `_claude_usage` (`runner.py:4507`) reads exactly those.
- **`modelUsage` field casing** — real payload uses camelCase `outputTokens` inside each model entry; `_claude_model_resolved` reads exactly that. The two casings differ within the same object and delegate has both right.
- **`permission_denials`** — present as `[]` on success, so the pure-mode boundary check (`runner.py:4540-4561`) has the field it requires.
- **`session_id`** — present on `system` and `result` events; `_capture_session_id` (`harness_events.py:712`) reads `session_id`/`sessionId` for `claude` on exactly those event types.
- **Event types** — `system`, `assistant`, `user`, `result` all handled (`harness_events.py:596-617`). Unknown types are deliberately dropped, which correctly absorbs the new `rate_limit_event`, `system/thinking_tokens`, and `system/status` events observed in the live capture.
- **Pure mode boundary holds** — `--safe-mode --tools "" --strict-mcp-config --no-session-persistence` yielded `tools: ['StructuredOutput']`, `mcp_servers: []`, `plugins: []`, `permission_denials: []`.
- **Safe mode tool lockdown holds** — yielded `tools: ['Bash','Glob','Grep','Read','StructuredOutput']`, `permissionMode: plan`, `mcp_servers: []`.
- **Version regex** — `_CANONICAL_VERSION_PATTERNS["claude"]` matches `2.1.263 (Claude Code)`. Discovery records `version: "2.1.263 (Claude Code)"`, `probeStatus: "partial"`, `personaTransports: {"native-file": true}`.
- **No live model enumeration** — `docs/cli-reference.md:191` ("Every harness except Claude exposes a live model listing") is correct. There is no `claude models` subcommand; the command list is `agents, attach, auth, auto-mode, doctor, gateway, import, install, logs, mcp, plugin, project, respawn, rm, setup-token, stop, ultrareview, update`. `/v1/models` discovery exists only behind `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` for LLM-gateway deployments (<https://code.claude.com/docs/en/llm-gateway-protocol>), which reflects the gateway inventory, not Claude Code's supported set.
- **`[1m]` suffix** — valid documented syntax on aliases and full names; delegate forwards it verbatim.
- **`--add-dir <root>` space form** for mail grants (`mail_core.py:388`) matches `--help`: `--add-dir <directories...>`.
- **Flags delegate correctly does not use** — `--max-turns`, `--include-partial-messages`, `--continue`, `--dangerously-skip-permissions`, `--disallowedTools`, `--session-id`, `--fork-session`, `--verbose`, and `--settings` appear nowhere in the Claude path (`--settings` and `--add-dir` hits in `src/` belong to mail-push and mail-core, not `build_claude_argv`). None is required for what delegate does.
- **Exit codes** — 0 on success, non-zero on failure; observed 1 for both an API 400 and an unknown flag. `result_exit_code = parsed_exit if process.returncode == 0 else process.returncode` (`runner.py:4756`) preserves the child code. The headless doc commits only to "code 0 on success and a non-zero code when the run fails", plus 143 for SIGTERM. delegate makes no finer assumption.
- **`estate-claude`** — a 265-byte `sh` wrapper that validates `DELEGATE_PROFILE`/`AI_PROFILE` against `work|personal` and execs `estate-harness launch claude <realm>`. It passes `"$@"` through unmodified, so it is argv-transparent. It is referenced in the repo only inside the issue doc's repro; `config.example.json:70` ships `"binary": "claude"`, while the local config resolves to `estate-claude` (confirmed in dry-run argv and in the discovery `selector`). No compatibility concern.

## Could not verify

- **`--resume` followup round-trip on a live Claude session.** The argv construction is unit-tested (`tests/test_engine_argv.py:1205`, `:1388`) and `--resume` is present in `--help`, but I did not spend a paid multi-turn call to confirm a real resume returns the prior session's context.
- **`--permission-mode bypassPermissions` behavior.** Gated behind `policy.harness.claude.work.bypassApprovalsAndSandbox` and not enabled in this config, so it was never exercised.
- **Whether `claude-sonnet-4-6` is still a servable model id.** It is in delegate's bundled table. The model-config doc lists Sonnet 4.6 only for the Claude Platform on AWS, not the Anthropic API. Confirming would need a live call per id.
- **`subtype: "error_max_turns"` and the `terminal_reason` values.** Documented in the SDK reference but unreachable from delegate, which never passes `--max-turns`.
- **Whether hooks can be fully suppressed in delegate's safe mode.** [S3] is based on the documented behavior of `--restricted` and `--safe-mode`, verified only as far as flag acceptance. I did not run a safe-mode argv with `--restricted` added to confirm the hook count drops.

## Sources

- <https://registry.npmjs.org/@anthropic-ai/claude-code> — latest 2.1.263 published 2026-09-06; `stable` tag lags at 2.1.236.
- <https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md> — top entry is 2.1.263, so no newer versions exist to diff. Also established that `--max-turns`, `--json-schema`, and `--append-system-prompt-file` have no "Added" entries, and that `xhigh` effort arrived in 2.1.111.
- <https://code.claude.com/docs/en/cli-reference> — verbatim flag definitions for `--effort` (including `ultracode`), `--json-schema`, `--max-turns`, `--append-system-prompt-file`, `--tools`, `--allowedTools`, `--disallowedTools`, `--permission-mode`, `--permission-prompts`, `--no-session-persistence`, `--safe-mode`, `--bare`, `--strict-mcp-config`.
- <https://code.claude.com/docs/en/headless> — `structured_output` field, exit-code language, SIGTERM 143, 10MB stdin cap, `system/init` and `stream_event` shapes. Its `jq -r '.result'` examples conflict with the observed array output ([L7]).
- <https://code.claude.com/docs/en/agent-sdk/typescript> — `SDKResultMessage` field list, the `error_max_turns` subtype, `terminal_reason` values, and the note that `modelUsage` is preferred over `usage` for accounting.
- <https://code.claude.com/docs/en/agent-sdk/structured-outputs> — states array is a supported type and never mentions a root constraint. `/docs/en/structured-outputs` 404s.
- <https://platform.claude.com/docs/en/api/messages> — the only primary source for the object-root rule: a tool's `input_schema` is pinned to `type: "object"`. This is the root cause of [B1].
- <https://code.claude.com/docs/en/model-config> — alias table (`default`, `best`, `fable`, `sonnet`, `opus`, `haiku`, `opusplan`, `[1m]` variants), alias-to-model resolution per provider, effort-level support per model, and the absence of any programmatic model listing.
- <https://code.claude.com/docs/en/errors> — exit code 1 cases including invalid `--json-schema`.
- <https://code.claude.com/docs/en/llm-gateway-protocol> — gateway-only `/v1/models` discovery behind `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`.
- Local command output: `claude --version`, `claude --help`, `claude --effort __delegate_probe__ --help`, `claude -p --append-system-prompt-file <missing>`, four `claude -p` probes (two free API-400 paths, two paid Haiku calls), `python3 bin/delegate.py --json dry-run|models|capabilities`, and three `PYTHONPATH=src python3 -c` module-level repros.
