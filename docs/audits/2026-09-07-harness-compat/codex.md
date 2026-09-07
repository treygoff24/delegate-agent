# Codex CLI compatibility audit

Latest version: **0.153.4** (2026-09-04, https://github.com/openai/codex/releases/tag/rust-v0.153.4) · Installed here: **codex-cli 0.153.4** at `/usr/local/bin/codex`

The installed binary *is* the latest stable release. `rust-v0.154.0-alpha.1..3` exist but are prereleases (`gh api repos/openai/codex/releases`).

Delegate assumptions checked: **41** · BROKEN: **1** · LATENT: **7** · SIMPLIFY: **4** · OK: **29**

---

## BROKEN

- **[B1] `--search` never reaches `codex exec`; `policy.webSearch` is a no-op for Codex safe and call runs.**
  Delegate `src/delegate_agent/argv_builders.py:630-631` emits a bare `--search` *before* the `exec` token
  (`if policy.get("webSearch") is True: argv.append("--search")`), and
  `src/delegate_agent/describe_payload.py:1164` documents this as "webSearch enables global `--search` before exec."
  Harness truth: `--search` is declared on `TuiCli`, not on `SharedCliOptions`
  (`codex-rs/tui/src/cli.rs:68-70`, `codex-rs/utils/cli/src/shared_options.rs:11-72`). The `Subcommand::Exec`
  arm in `codex-rs/cli/src/main.rs:1144-1158` forwards only `inherit_exec_root_options(&interactive.shared)`,
  and `inherit_exec_root_options` (`shared_options.rs:91-120`) destructures exactly
  `images, model, oss, oss_provider, config_profile_v2, sandbox_mode, auto_review,
  dangerously_bypass_approvals_and_sandbox, bypass_hook_trust, cwd, add_dir`. `web_search` is not among them.
  Codex honors `interactive.web_search` only for `features list` (`main.rs:1775-1779`), `debug prompt-input`
  (`main.rs:2205-2211`), and the interactive TUI. The flag is parsed, then silently discarded for `exec`.

  Repro (no model call):
  ```
  $ codex exec --search
  error: unexpected argument '--search' found
  $ codex --search --profile delegate --model gpt-5.6-sol exec --cd . --sandbox read-only --color BOGUS --json -
  error: invalid value 'BOGUS' for '--color <COLOR>'   # everything before it parsed; --search was accepted and dropped
  ```

  Effective impact is mode-dependent. Work mode emits `--dangerously-bypass-approvals-and-sandbox`, and the
  documented `web_search` default is `"live"` under full-access sandboxes, so work runs get live search anyway.
  Safe (`--sandbox read-only`) and call (`--sandbox workspace-write`) stay at the `"cached"` default — "an
  OpenAI-maintained index without external web access"
  (https://learn.chatgpt.com/docs/config-file/config-reference). So `policy.webSearch: true` silently buys
  nothing in the two modes where it is the only way to ask for it.

  Smallest fix: replace the `--search` token with a config override placed with the other `-c` flags,
  `argv.extend(["-c", 'web_search="live"'])`, and update the `workNotes` string in `describe_payload.py:1164`.

---

## LATENT

- **[L1] `--ask-for-approval never` is also dropped before `exec`; the safe-mode contract rests on an inert flag.**
  `argv_builders.py:632-633` emits `--ask-for-approval never` ahead of `exec`, and both
  `docs/security-model.md:112` ("Codex safe keeps `--ask-for-approval never`") and
  `describe_payload.py:1161` ("Non-interactive: `--ask-for-approval never`.") present it as enforcement.
  `approval_policy` lives on `TuiCli` (`codex-rs/tui/src/cli.rs:64-66`), not `SharedCliOptions`, so it is
  discarded by the same `inherit_exec_root_options` path as B1. The exec-level flag no longer exists at all:
  ```
  $ codex exec --ask-for-approval bogus
  error: unexpected argument '--ask-for-approval' found
  $ codex -a bogus
  error: invalid value 'bogus' for '--ask-for-approval <APPROVAL_POLICY>'
    [possible values: on-request, never]
  ```
  Behavior is currently correct only by coincidence: `codex-rs/exec/src/lib.rs:411-413` sets
  `// Default to never ask for approvals in headless mode.` / `approval_policy: Some(AskForApproval::Never)`.
  Delegate's boundary therefore holds, but it is held by a Codex default rather than by the flag Delegate
  emits and documents. Note also that top-level `-a` `conflicts_with` `dangerously_bypass_approvals_and_sandbox`
  and `auto_review` (`tui/src/cli.rs:140-143`); Delegate escapes that conflict only because the bypass flag is
  emitted in the exec scope, not the root scope. Smallest fix: emit `-c approval_policy="never"` after `exec`,
  or drop the flag and state plainly in the docs that headless Codex defaults to never-ask.

- **[L2] `codex.profile` now selects a separate `$CODEX_HOME/<name>.config.toml` file, and a missing one fails open.**
  `argv_builders.py:634-635` emits `--profile <name>`; `docs/configuration.md:387` calls it an "optional Codex CLI
  config overlay name." At 0.153.4 the flag's value type is `CONFIG_PROFILE_V2` and its help reads
  "Layer `$CODEX_HOME/<name>.config.toml` on top of the base user config"
  (`codex-rs/utils/cli/src/shared_options.rs:34-36`). The config reference confirms profiles are no longer
  `[profiles.<name>]` tables inside `config.toml`. A name with no matching file is accepted silently:
  ```
  $ ls ~/.codex/*.config.toml
  ls: cannot access '/home/trey-agent/.codex/*.config.toml': No such file or directory
  $ grep -c '^\[profiles' ~/.codex/config.toml
  0
  $ codex --profile nonexistent-xyz debug prompt-input   # exits 0, renders normally
  ```
  This machine's live `~/.delegate/config.json` sets `"profile": "delegate"` against a `CODEX_HOME` with neither
  a `delegate.config.toml` nor a `[profiles.delegate]` table, so every Codex run here already resolves no overlay
  and says nothing about it. `codex.fallbackProfile` is unaffected — it names a `profiles.definitions` entry with
  its own `CODEX_HOME` (`src/delegate_agent/config.py:779-800`), not a Codex CLI profile.
  Smallest fix: have `delegate doctor` check that `$CODEX_HOME/<codex.profile>.config.toml` exists, and reword
  `docs/configuration.md:387` to name the file.

- **[L3] Completion-report preamble suppression only clears on `command_execution` items.**
  `harness_events.py:965-977` promotes the last `agent_message` to `completion_text` at `turn.completed`, and
  clears the candidate only when a `command_execution` item arrives. Codex's item taxonomy
  (`codex-rs/exec/src/exec_events.rs:106-131`) also includes `file_change`, `mcp_tool_call`, `collab_tool_call`,
  `web_search`, `todo_list`, `reasoning`, and `error`. Notably `apply_patch` surfaces as `file_change`, not as a
  command: "Represents a set of file changes by the agent. The item is emitted only as a completed event once the
  patch succeeds or fails." A run whose last activity is a patch, an MCP call, or a web search after an
  "I'll start by…" preamble will promote that preamble as the completion report — exactly the failure the guard
  was written to prevent. Smallest fix: clear `_codex_completion_candidate` for every non-`agent_message` item type.

- **[L4] `item.updated` is dropped entirely.**
  `harness_events.py:628` matches only `("item.started", "item.completed")`. The wire enum has three
  (`exec_events.rs:26-34`: `item.started`, `item.updated`, `item.completed`). `todo_list` is documented as
  updating in place ("updates as steps change state"), so plan progress never reaches the normalized event
  stream or `delegate snapshot`. Not a failure, just a blind spot in live progress.

- **[L5] Bundled Codex model and reasoning tables are stale in five ways.**
  `bundled_models.py:14-20` lists `gpt-5.6-sol, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark`;
  `reasoning.py:19-40` carries matching reasoning declarations. Live catalog (`codex debug models`, 11 entries):

  | Delegate bundled | Catalog 0.153.4 |
  |---|---|
  | `gpt-5.3-codex-spark` present | absent |
  | `gpt-6-astra`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.2` absent | present, `visibility: list` |
  | `gpt-5.6-sol` efforts end at `max` | also supports `ultra` |
  | `gpt-5.6-sol` default `medium` | default `low` |
  | `gpt-5.4-mini` default `high` | default `medium` |

  These are advisory fallbacks only — discovery wins, and discovery is healthy (see OK below) — so this bites
  only on a machine that has never run `delegate capabilities refresh`. There, `--reasoning-effort ultra`
  against `sol` would fail closed against a table that predates the level.

- **[L6] Reasoning effort is unvalidated on both sides when no model resolves.**
  With `codex.defaultModel: null`, Delegate passes the level straight through:
  ```
  $ env -u AI_PROFILE HOME=$tmp DELEGATE_CONFIG=config.example.json \
      python3 bin/delegate.py --json dry-run codex safe --reasoning-effort high "Review only."
  argv: [..., "-c", "model_reasoning_effort=\"high\"", "exec", ...]
  ```
  (`docs/agent-setup.md:143` states this is intentional.) Codex does not validate the value at config-parse time
  either — `codex -c model_reasoning_effort="bogus" debug prompt-input` exits 0 and renders normally, as do
  `ultra`, `minimal`, and `none`. So an unsupported level for the served model is discovered only by the API, at
  cost. Worth noting the vendor's own config reference still documents `minimal | low | medium | high | xhigh`
  and omits `max` and `ultra`, which the live catalog advertises; the catalog is the more current source and is
  what Delegate's discovery already reads.

- **[L7] Codex's bundled default model changed to `gpt-6-astra` in 0.153.4 with no Delegate change.**
  Release note for `rust-v0.153.4`: "Fixed Astra's visibility in the bundled model picker and made it the
  bundled default when no model is explicitly configured." (#42874). `config.example.json` ships
  `codex.defaultModel: null` and the resulting argv carries no `--model`, so a stock Delegate install now lands
  on Astra at its catalog default effort `low`, where it previously landed elsewhere. Deliberate on the vendor's
  side, but it silently changes cost and behavior for anyone relying on the unset default.

---

## SIMPLIFY

- **[S1] `turn.completed` carries token usage that Delegate discards for Codex.**
  `TurnCompletedEvent { usage: Usage }` with `input_tokens, cached_input_tokens, cache_write_input_tokens,
  output_tokens, reasoning_output_tokens` (`exec_events.rs:50-73`). `harness_events.py:988-990` handles
  `turn.completed` only to seal the completion candidate, and the sole `self.usage` assignment in the module
  (line 941) is gated on `self.harness == "cursor"`. Codex runs therefore report no usage while Cursor runs do.
  `_normalize_reported_usage` (line 423) already reads snake_case keys, so this is roughly a three-line
  addition inside `_ingest_codex_turn_completed`, not new machinery.

- **[S2] Two dead Codex event branches.** `harness_events.py:618` matches `"turn.error"` and lines 625 and 378
  match `"turn.cancelled"/"turn.canceled"`. Codex's `ThreadEvent` enum has exactly eight variants —
  `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.started`, `item.updated`,
  `item.completed`, `error` (`exec_events.rs:11-37`). Neither name can ever arrive from Codex. They may be
  live for another harness; if not, they are deletable.

- **[S3] `--strict-config` (exec-level, new) would turn silent `-c` typos into hard errors.**
  `codex-rs/exec/src/cli.rs:21-22`. Delegate emits up to four `-c` overrides per run
  (`model_reasoning_effort`, `sandbox_workspace_write.network_access`, `service_tier`, `features.fast_mode`);
  today a renamed key would be accepted and ignored. Note it is rejected on `codex debug` and `codex sandbox`
  ("`--strict-config` is not supported for `codex debug`"), so it cannot be smoke-tested without a real run.

- **[S4] `-o/--output-last-message <FILE>` is global on exec and resume and would hand back the final message directly.**
  `exec/src/cli.rs:68-74`. Delegate currently reconstructs the completion report by tracking the last
  `agent_message` across items and sealing it at `turn.completed` — the machinery that L3 shows is incomplete.
  A file write is unambiguous, needs no preamble heuristic, and coexists with `--json`. Would let
  `_codex_completion_candidate`, `_ingest_codex_turn_completed`, and the command-execution clearing logic go away.

---

## OK (verified)

Argv shape, all three modes, parse-verified against the installed binary by poisoning one enum value and
confirming the error names *that* value (proving every preceding token parsed):

```
$ codex --ask-for-approval never --profile delegate --model gpt-5.6-sol -c 'model_reasoning_effort="high"' \
    exec --cd . --sandbox BOGUS --color never --json --ephemeral -
error: invalid value 'BOGUS' for '--sandbox <SANDBOX_MODE>'
$ codex --search --profile delegate --model gpt-5.6-sol -c 'model_reasoning_effort="high"' \
    exec --cd . --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust \
    --color BOGUS --json --ephemeral -
error: invalid value 'BOGUS' for '--color <COLOR>'
$ codex --search --ask-for-approval never ... exec --cd /tmp --skip-git-repo-check \
    --sandbox workspace-write -c 'sandbox_workspace_write.network_access=true' --color BOGUS --json --ephemeral -
error: invalid value 'BOGUS' for '--color <COLOR>'
```

- `codex exec --json` — still the flag, with `alias = "experimental-json"` retained (`exec/src/cli.rs:58-65`). Delegate uses `--json`; the older name also still parses.
- `--sandbox` values `read-only | workspace-write | danger-full-access` — unchanged; matches `codex.workSandbox` validation.
- `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-bypass-hook-trust` — both present on exec and on `exec resume`; `mark_exec_global_args` makes them global within exec (`exec/src/cli.rs:138-148`).
- `-C/--cd`, `-m/--model`, `-p/--profile` — present in `SharedCliOptions` and inherited into exec.
- `--skip-git-repo-check`, `--ephemeral`, `--ignore-user-config`, `--output-schema` — all `global = true` on exec, so they parse both before and after the `resume` token.
- **Resume argv ordering is correct and non-obvious.** `--color` is *not* global (`exec/src/cli.rs:55-56`) and is absent from `codex exec resume --help`; `--sandbox` and `--cd` are not global either. `argv_builders.py:653-676` places the sandbox tokens between `exec` and `resume` and suppresses `--color`/`--ephemeral` on the resume path. Both the structured (`resume <id>` early) and unstructured (`<id>` trailing) orderings are valid against `[SESSION_ID] [PROMPT]`.
- **Resume by UUID ignores cwd filtering.** `resolve_resume_thread_id` returns early for a parseable UUID (`exec/src/lib.rs:1655-1658`); `cwds_match` gating applies only to `--last` and to name-based lookup. Delegate passes the `thread_id` from `thread.started`, and separately relaunches in the recorded `executionCwd` (`request_build.py:1798-1812`), so both paths are safe.
- **`--ephemeral` correctly suppressed when a session must be resumable** (`argv_builders.py:678-686`) — an ephemeral run persists no rollout and could never be resumed.
- **Stdin prompt transport with `-`** — `StdinPromptBehavior::Forced` reads stdin and, unlike the piped-without-`-` path, prints no `Reading prompt from stdin...` notice, so nothing pollutes stderr (`exec/src/lib.rs:2049-2065`).
- **Event stream shapes all match.** `thread.started` → `thread_id` (`harness_events.py:710-711`); `turn.failed` → `error.message` (line 736-741 vs `TurnFailedEvent { error: ThreadErrorEvent }`); `error` → `message` (line 728-733 vs `ThreadErrorEvent { message: String }`); item envelope `{item: {id, type, …}}` with `type` flattened via `#[serde(tag = "type", rename_all = "snake_case")]`; `agent_message.text`; `command_execution.{command,status}` with `CommandExecutionStatus` = `in_progress | completed | failed | declined`, and `_codex_command_status` maps `completed` → `success`.
- **Exit codes.** Codex exec uses only `std::process::exit(1)` for failures and 0 for success; no richer taxonomy exists. `runner.status_from_exit` (0 = succeeded, else failed) is compatible.
- **`no thread with id: {0}`** — exact string still emitted (`codex-rs/protocol/src/error.rs:102-103`, `ThreadNotFound`). Delegate's first `_THREAD_LOSS_PATTERNS` entry (`child_failures.py:22`) matches it verbatim.
- **Usage-limit classification.** Every plan variant of the limit message contains the literal "usage limit" (`error.rs:659-745`), matching `_USAGE_PATTERNS`. Reset windows render as `" Try again at 3:45 PM."` / `" or try again later."` (`error.rs:753-769`), matching `_RESET_PATTERN`'s `try again` alternative.
- **Model discovery is healthy and is the authoritative path.** `codex debug models` still renders the raw catalog as JSON. Ran Delegate's own parser against live output:
  ```
  $ codex debug models > catalog.json
  $ PYTHONPATH=src python3 -c "...parse_codex_catalog(open('catalog.json').read())..."
  parsed OK, n= 11
    gpt-6-astra    default='low'    supported=['low','medium','high','xhigh','max','ultra']
    gpt-5.6-sol    default='low'    supported=['low','medium','high','xhigh','max','ultra']
    ...
  ```
  Entry keys `slug`, `display_name`, `default_reasoning_level`, and `supported_reasoning_levels` (list of
  `{effort, description}` objects) all still exist, and `parse_codex_models_payload` already handles both the
  object and bare-string forms.
- **Fast-tier wiring is exactly right and still load-bearing.** `argv_builders.py:645-651` emits
  `-c service_tier="fast"` plus `-c features.fast_mode=true`. Confirmed:
  `ServiceTier::Fast.request_value() == "priority"` and `from_request_value` accepts `"fast" | "priority"`
  (`protocol/src/config_types.rs:538-553`); `core/src/config/mod.rs:3864-3872` maps a configured `Fast` to
  `"priority"` **only when** `features.enabled(Feature::FastMode)`, which is precisely the no-op the inline
  comment predicts. `features.fast_mode` is now `stable` and on by default (`codex features list`), so the
  override is belt-and-braces rather than required — do not delete it, it is the documented gate.
- **`--no-fast` sentinel is correct.** `SERVICE_TIER_DEFAULT_REQUEST_VALUE = "default"`
  (`config_types.rs:536`), described as "not a catalog service tier id… the user intentionally selected no
  service tier, so model catalog defaults should not apply." `get_service_tier` (`core/src/session/mod.rs:968-980`)
  admits it unconditionally and `service_tier_for_request` then strips it from the wire request.
- **Strict structured output.** `Prompt::output_schema_strict` defaults to `true`
  (`core/src/client_common.rs:35-52`) with no config surface to relax it, so
  `structured_output.normalize_codex_schema`'s object-root and closed-object requirements remain correct, as does
  `workflows/runtime.py:3764-3776` falling back to prompt-and-parse for schemas that would fail preflight.
  `--output-schema` failures are clean `exit(1)` with a readable message (`exec/src/lib.rs:1950-1973`).
- **Version regex.** `codex --version` prints `codex-cli 0.153.4`, matching `^codex-cli\s+[0-9]…` (`harness_discovery.py:87`).
- **`constants.py:86-91` capability flags** — `structuredOutput`, `noSessionPersistence`, `nativeSessionResume`, and `promptStdin` are all true of Codex at 0.153.4.
- **`safe_workspace._ensure_codex_skip_git_repo_check`** inserts before the final prompt token, which is valid because `--skip-git-repo-check` is global on exec.
- **`--output-last-message` and `--experimental-json` are not emitted anywhere in Delegate** (repo-wide grep), so their status is informational only.

---

## Could not verify

- **`--strict-config` against Delegate's real `-c` key set.** It is rejected on every subcommand that does not
  call a model (`Error: --strict-config is not supported for codex debug` / `for codex sandbox`), so confirming
  that all four override keys are recognized needs a paid `codex exec` run. Each key is individually documented
  in the vendor config reference, which is why they are listed under OK rather than unverified.
- **The exact failure text when resuming a UUID whose rollout is gone.** `resolve_resume_thread_id` short-circuits
  on any parseable UUID, so the failure surfaces later from the `thread/resume` app-server request rather than
  from the `no thread with id` path. Whether Delegate's `codex_thread_lost` classifier catches *that* message is
  untested. Delegate's own tests use a synthetic `"no thread with id: synthetic-thread"` string
  (`tests/test_codex_thread_resilience.py:135`), which does match the real `ThreadNotFound` display; the
  app-server resume error is a different, unexamined string. Worth one live probe against a deleted session.
- **Whether any live Codex run actually ends on a `file_change` item after a preamble message** (the L3 trigger).
  The code path is clearly reachable; how often it fires is not something a dry-run can answer.
- **The vendor's non-interactive doc is stale relative to the binary** and was therefore not treated as truth.
  https://learn.chatgpt.com/docs/non-interactive-mode still lists `--full-auto` ("Deprecated compatibility flag")
  and `--ask-for-approval` as `codex exec` flags; both are rejected by 0.153.4
  (`error: unexpected argument '--full-auto' found`). Every flag claim above is sourced from the binary or from
  `codex-rs` at tag `rust-v0.153.4`, not from that page.

---

## Sources

- `codex --version`, `codex --help`, `codex exec --help`, `codex exec resume --help`, `codex debug models`, `codex features list` (local, codex-cli 0.153.4) — installed-binary ground truth for every flag, enum value, and the model catalog.
- Clap parse probes against the installed binary (`codex exec --ask-for-approval bogus`, `codex exec --full-auto --sandbox bogus`, `codex exec --search`, `codex -a bogus`, `codex exec --experimental-json --sandbox bogus`) — established which flags were removed vs. retained as hidden aliases, with no model call.
- https://github.com/openai/codex/releases (via `gh api`) — 0.153.4 is the latest stable; 0.154.0-alpha.* are prereleases. Release bodies for 0.153.0–0.153.4 established the Astra default-model change.
- `codex-rs/exec/src/cli.rs` @ `rust-v0.153.4` — exec flag definitions, `--json`/`--experimental-json` alias, globality of `--output-schema`/`--ephemeral`/`--skip-git-repo-check`/`--json`/`-o`, non-globality of `--color`, `mark_exec_global_args`.
- `codex-rs/exec/src/exec_events.rs` @ `rust-v0.153.4` — the complete JSONL event and item taxonomy, `Usage` fields, `CommandExecutionStatus` values.
- `codex-rs/exec/src/lib.rs` @ `rust-v0.153.4` — headless approval default, exit-code behavior, stdin `-` handling, resume/fork thread resolution and cwd matching.
- `codex-rs/cli/src/main.rs` and `codex-rs/utils/cli/src/shared_options.rs` @ `rust-v0.153.4` — proof that `--search` and `--ask-for-approval` are not inherited into `exec`; `--profile` help text naming `$CODEX_HOME/<name>.config.toml`.
- `codex-rs/tui/src/cli.rs` @ `rust-v0.153.4` — `approval_policy` and `web_search` are TuiCli-only.
- `codex-rs/protocol/src/error.rs`, `config_types.rs`, `openai_models.rs`; `codex-rs/core/src/config/mod.rs`, `session/mod.rs`, `client_common.rs` @ `rust-v0.153.4` — error strings, service-tier normalization and gating, strict-schema default.
- https://learn.chatgpt.com/docs/config-file/config-reference — `service_tier`, `web_search` modes and defaults, `model_reasoning_effort`, `features.fast_mode`, and confirmation that profiles are separate files.
- https://learn.chatgpt.com/docs/non-interactive-mode — used only to show the published doc is behind the binary.
