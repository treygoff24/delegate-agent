# OpenCode compatibility audit

Latest version: 1.18.29 (2026-09-04, https://github.com/anomalyco/opencode/releases/tag/v1.18.29) · Installed here: not installed (`command -v opencode` empty; `~/.opencode/bin` does not exist)
Delegate assumptions checked: 43 · BROKEN: 1 · LATENT: 6 · SIMPLIFY: 2 · OK: 34

Repo note: `sst/opencode` now redirects to **`anomalyco/opencode`** (`gh api repos/sst/opencode --jq .full_name` → `anomalyco/opencode`). Docs stay at `https://opencode.ai/docs`. The npm package is `opencode-ai` (dist-tag `latest` = `1.18.29`); there is no package named plain `opencode`. All source citations below are pinned to tag `v1.18.29`.

Delegate's own captured fixtures are from opencode **1.17.18** (`tests/fixtures/discovery/provenance.json`) and **1.17.17** (`tests/fixtures/opencode/README.md`), so everything here is a 1.17 → 1.18 delta check.

## BROKEN

- **[B1] Live model discovery silently drops every model whose id contains a slash, and still reports `probeStatus: "ok"`.** Delegate: `src/delegate_agent/harness_discovery.py:864` matches selectors with `re.compile(r"(?m)^([^\s/]+/[^\s/]+)\r?\n(?=\s*\{)")`, which requires exactly one slash. Harness: `packages/opencode/src/cli/cmd/models.ts` writes `` process.stdout.write(`${providerID}/${modelID}`) `` where `modelID` is a raw catalog key. In the catalog opencode actually fetches (`Flag.OPENCODE_MODELS_URL || "https://models.opencode.ai"`, read as `${source}/api.json`; models.dev is the user-facing name and returns the same data), **4,278 of 7,560 models across 86 of 213 providers have a slash inside the model id**, including 360/360 openrouter, 371/371 vercel, 369/369 kilo, and 405/592 nano-gpt. 124 ids carry two or more internal slashes; the deepest is six segments, `edenai/fireworks_ai/accounts/fireworks/models/muse-glimmer-30b`. The literal case exists today as `openrouter/anthropic/claude-sonnet-4.5`. `-m` parsing splits on the *first* slash only (`packages/core/src/model.ts:33`, `const [providerID, ...modelID] = input.split("/")`), so those ids are legitimate and runnable. Delegate's own docs claim the opposite reach: `docs/cli-reference.md:436` says live discovery "includes any provider in OpenCode's models.dev catalog."

  Repro (no binary, no paid call):
  ```
  PYTHONPATH=src python3 -c '
  from delegate_agent.harness_discovery import parse_opencode_catalog
  import json
  raw = ""
  for sel, prov, mid in [("openai/gpt-5","openai","gpt-5"),
                         ("openrouter/anthropic/claude-sonnet-4.5","openrouter","anthropic/claude-sonnet-4.5")]:
      raw += sel + "\n" + json.dumps({"providerID": prov, "id": mid, "name": sel}, indent=2) + "\n"
  f = parse_opencode_catalog(raw)
  print(sorted(f["models"]), f["probeStatus"], f["warnings"])'
  ```
  Output: `['openai/gpt-5'] ok []` — the openrouter entry is gone with no warning and no status downgrade.

  Blast radius is discovery only, not launch: an operator who types a multi-slash id still runs, because `_reject_opencode_flag_like_value` only bars a leading `-` and the id passes through verbatim. What breaks is `delegate models opencode --live` (the id is absent), the capability cache, and reasoning validation (`reasoning.resolve_discovered_model_capability` finds no declaration and degrades every such model to `opencode_variant_unvalidated` pass-through).

  Smallest fix: change the selector regex to `^([^\s/]+/\S+)\r?\n(?=\s*\{)` and keep the existing `f"{provider}/{model_id}" != selector` equality check as the guard against over-matching — that check already rejects anything the two-field JSON body does not confirm. First-party providers (anthropic, openai, the `opencode`/Zen provider, github-copilot) have zero slashed ids, which is why this went unnoticed.

## LATENT

- **[L1] OpenCode reads `~/.claude/CLAUDE.md` and `.claude/skills` by default, and delegate never disables it — including in safe mode.** The documented env table at https://opencode.ai/docs/cli lists `OPENCODE_DISABLE_CLAUDE_CODE` ("Disable reading from .claude (prompt + skills)"), `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` ("Disable reading ~/.claude/CLAUDE.md") and `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` ("Disable loading .claude/skills"), all defaulting to off. `_opencode_env_overrides` (`src/delegate_agent/request_build.py:3095`) sets only `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_CONFIG_CONTENT`, and `OPENCODE_PERMISSION`. So a safe review pulls in the operator's global Claude Code instructions and any `.claude/skills` present in the mirrored workspace. `--pure` does not cover this: it only sets `OPENCODE_PURE=1` to skip external plugins (`packages/opencode/src/index.ts:62`). This is instruction-surface leakage into a boundary `docs/security-model.md:120` describes as locked down; it does not grant write tools, because the permission deny-all still binds. Smallest fix: add the three `OPENCODE_DISABLE_CLAUDE_CODE*` vars to the read-only override set, or state the exposure in `docs/security-model.md`.

- **[L2] The discovery probe runs without the autoupdate suppression every launch path applies.** `_probe_opencode` (`src/delegate_agent/harness_discovery.py:1371`) runs `[binary, "--pure", "models", "--verbose"]` with `env = profiles.child_environment(overrides=profile.env)` and no `OPENCODE_DISABLE_AUTOUPDATE=1`, under `METADATA_PROBE_TIMEOUT_SEC = 15`. Every real run sets that variable. An update check, or a cold `models.dev` fetch (`OPENCODE_DISABLE_MODELS_FETCH` also defaults to off), can therefore run inside a 15-second budget on the one code path that is supposed to be a cheap metadata read. Repro is a live-binary observation only. Smallest fix: pass the same `_opencode_env_overrides` non-read-only dict into the probe env.

- **[L3] A locally built opencode reports its version as the literal string `local`, which delegate cannot read.** `packages/core/src/installation/version.ts`: `export const InstallationVersion = typeof OPENCODE_VERSION === "string" ? OPENCODE_VERSION : "local"`, wired as `.version("version", "show version number", InstallationVersion)` (`packages/opencode/src/index.ts:51`). Delegate has no canonical banner pattern for opencode, so `_expected_version` falls back to `_GENERIC_VERSION = re.compile(r"^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?$")` (`harness_discovery.py:109`), which `local` does not match. Result: `version: null` in the discovery record and `cached_version_has_drifted` can never fire. Released binaries print a bare semver (npm `opencode-ai` dist-tag `latest` = `1.18.29`), so this only bites development installs. Inferred from source; not observed, since the binary is not installed here.

- **[L4] Ambient OpenCode environment is inherited wholesale.** `profiles.child_environment` returns `dict(os.environ)` for every non-pure engine (`src/delegate_agent/profiles.py:240`), and opencode is never pure-eligible (`constants.pure_call_supported` returns true only for claude). So `OPENCODE_CONFIG` (precedence slot 3), `OPENCODE_CONFIG_DIR`, and `OPENCODE_AUTO_SHARE` reach the child untouched. The permission lockdown still wins for the keys it sets, because `OPENCODE_CONFIG_CONTENT` merges at slot 6 and `OPENCODE_PERMISSION` deep-merges last — delegate re-asserts both after profile resolution (`request_build.py:3711`). But `OPENCODE_CONFIG_DIR` can still supply agents and commands, and `share: "disabled"` versus an ambient `OPENCODE_AUTO_SHARE` has no documented precedence I could establish. Two config layers also outrank `OPENCODE_CONFIG_CONTENT` outright: managed config files (`/etc/opencode/` on Linux) and macOS MDM preferences (https://opencode.ai/docs/config precedence list, slots 7 and 8). `docs/cli-reference.md:405` claims the merge means "a repository config cannot restore write-capable tools" — true for repository config, not for a managed one.

- **[L5] `OPENCODE_PERMISSION` fails open on malformed JSON.** From `packages/core/src/config/config.ts`: invalid JSON in that variable is caught and logged (`"OPENCODE_PERMISSION contains invalid JSON, skipping"`), and the run proceeds with whatever permissions the config files produced. Delegate always emits machine-serialized JSON via `json.dumps`, so this cannot misfire today; it matters only as a property of a boundary `docs/security-model.md:120` presents as enforcement. `OPENCODE_CONFIG_CONTENT` carries the same policy, so the two would have to fail together.

- **[L6] OpenCode ends with `process.exit()`, which discards unflushed pipe data — and delegate's stdout reader does slow work inline.** `packages/opencode/src/index.ts` closes with `} finally { process.exit() }` (a deliberate guard against hanging MCP subprocesses), and `run.ts` contains no flush or drain before it. Delegate's drain loop is not a fast reader: `runner.py:2263` `handle_stdout_line` runs `accumulator.ingest_line`, `watchdog.observe_line`, and event-buffer appends per line, then every `PROGRESS_PERSIST_LINE_INTERVAL` lines or `PROGRESS_PERSIST_TIME_INTERVAL_SEC = 0.5` seconds (`runner.py:63`) stops to `events_handle.flush()` and `maybe_persist_running()` — a synchronous disk write inside the reader.

  A delegated experiment against Bun 1.3.14 reproduced the mechanism directly: 20,000 NDJSON-shaped lines burst-written and then `process.exit()`ed delivered all 20,000 to a fast reader, but only **272** to a reader with a 1-second delay; removing the `process.exit()` restored all 20,000. 272 lines at roughly 250 bytes is about 68KB, one 64KB pipe buffer — everything the kernel had not already accepted is dropped.

  I did not reproduce this myself and it was measured against system Bun, not the Bun embedded in the shipped opencode binary, so the exact threshold will differ; the mechanism will not. Real exposure is bounded, because opencode's events are model-paced and delegate keeps up during the run — the risk window is a large final burst, and the payload most likely to fill a pipe is a `tool_use` line, which carries the tool's entire `state.output`. Losing the tail would drop the final `step_finish`, which is where `reason: "stop"`, cost, and token totals live. Mitigation on delegate's side is to drain stdout into memory and do accumulation off the read thread; the harness-side fix is not ours.

## SIMPLIFY

- **[S1] OpenCode has native session resume, and delegate declares it absent.** `constants.py:88` sets `"nativeSessionResume": engine in {"codex", "claude"}`, so `delegate followup` refuses opencode, and `docs/cli-reference.md:1184` records the agent selection as "Inherited only for a same-engine OpenCode resume." The harness supplies everything needed: `run` takes `--session` / `-s` ("Session ID to continue"), `--continue` / `-c` ("Continue the last session"), and `--fork` (https://opencode.ai/docs/cli#run), and **every** JSON event already carries a top-level `sessionID` — visible in delegate's own fixtures (`tests/fixtures/opencode/simple_text.ndjson`, `"sessionID":"ses_0b850d7d2ffeowrlqEvxrNru7d"` on all three lines). Delegate does not capture it: `_capture_session_id` (`harness_events.py:708`) branches only on codex, claude/cursor, and omp. Adding an `elif self.harness == "opencode"` reading `payload["sessionID"]` plus `--session <id>` in `build_opencode_argv` would enable `delegate followup opencode` and let the resume-inheritance special case collapse into the ordinary path. Session ids are `ses_`-prefixed alphanumerics, so `validate_session_id` accepts them unchanged.

- **[S2] `parse_opencode_models_output` is dead code.** `src/delegate_agent/model_discovery.py:298` is referenced only by `tests/test_model_discovery.py:182` and `:204`; the live path is `harness_discovery._probe_opencode`, which calls `parse_opencode_catalog` directly. Deleting it removes a second, divergent parser — note that its bare-line fallback handles multi-slash ids correctly while the catalog parser (B1) does not, so the two disagree about what a valid model id is. Estimated deletion: ~17 lines in `model_discovery.py` plus two tests.

## OK (verified)

Argv, verified against https://opencode.ai/docs/cli and `packages/opencode/src/cli/cmd/run.ts` at v1.18.29, cross-checked with `python3 bin/delegate.py --json dry-run opencode <mode>`:

- `opencode --pure run ...` — `--pure` ("Run without external plugins") is a root-level yargs option; root options default to global in yargs, and `.strict()` (`index.ts:116`) therefore accepts it either side of the subcommand. Delegate places it before `run`.
- `--print-logs` after `run` — same mechanism. Declared at root (`index.ts:53`, "print logs to stderr"), listed under "Global Flags" in the docs, inherited by `run`. The yargs 18.0.0 API reference states plainly under `.global()`: "Options default to being global." OpenCode's own tree uses both positions in working code — `packages/opencode/script/run-workspace-server:65` spawns `["bun","run","dev","serve","--port",...,"--print-logs"]` with the flag after the subcommand, and `packages/desktop/src/main/wsl/sidecar.ts:38` puts it before. Delegate emits it post-subcommand; that parses.
- `--format json` — real, `choices: ["default","json"]`, described as "Format: default (formatted) or json (raw JSON events)".
- `--dir <path>` — real `run` flag, "Directory to run in, or path on the remote server when attaching".
- `--model <provider/model>` (`-m`) — "Model to use in the form of provider/model". Delegate passes ids verbatim.
- `--agent <name>` — real `run` flag, "Agent to use".
- `--variant <level>` — real, "Model variant (provider-specific reasoning effort)". Delegate's `--reasoning-effort` → `--variant` mapping is correct.
- `--auto` in work mode only — real, "Auto-approve permissions that are not explicitly denied". Delegate adds it for `work` and withholds it for `safe` and `call`, matching the dry-run argv.
- Prompt on stdin — `run.ts` reads `process.stdin.isTTY ? undefined : await Bun.stdin.text()` and, with an empty positional, uses the piped text as the whole message. Delegate sets `prompt_transport=PROMPT_TRANSPORT_STDIN` with an empty positional.
- Undocumented aliases `--yolo` and `--dangerously-skip-permissions` exist as `hidden: true` synonyms of `--auto` in `run.ts`; delegate correctly uses the documented flag.

Environment and config, verified against the documented env table and `https://opencode.ai/config.json`:

- `OPENCODE_DISABLE_AUTOUPDATE=1` — documented boolean; `truthy()` accepts `"1"` and `"true"` case-insensitively.
- `OPENCODE_CONFIG_CONTENT` — documented as "Inline json config content", merged (not replaced) at local scope after project config.
- `OPENCODE_PERMISSION` — documented as "Inlined json permissions config", deep-merged into `result.permission`; its shape is the `permission` block itself, which is what delegate sends.
- `"share": "disabled"` — schema enum is exactly `["manual","auto","disabled"]`.
- `"autoupdate": false` — schema accepts boolean or the string `"notify"`.
- Agent `"mode": "primary"` — schema enum `["subagent","primary","all"]`; delegate sets it for the synthetic `delegate-read-only` agent.
- Agent `"prompt"` string and arbitrary agent keys — `Config.agent` has `additionalProperties: {$ref: AgentConfig}`, and `AgentConfig.prompt` is a plain string, so the persona agent-config merge is well-formed.
- Permission values `allow` / `ask` / `deny` — `PermissionActionConfig` enum is exactly those three.
- The `"*"` catch-all key — `PermissionConfig` carries `additionalProperties: {$ref: PermissionRuleConfig}`, and the docs state keys "are matched as wildcard patterns against the underlying tool name". Delegate's `OPENCODE_SAFE_PERMISSION` puts `"*": "deny"` first, which is the documented idiom ("put the catch-all `\"*\"` rule first, and more specific rules after it", last match wins).
- Every named key delegate uses (`read`, `glob`, `grep`, `edit`, `bash`, `task`, `skill`, `external_directory`, `webfetch`) is in the schema's explicit property list.

JSON event stream, verified against `run.ts` and delegate's own NDJSON fixtures:

- NDJSON, one object per line — the emitter writes `JSON.stringify({type, timestamp, sessionID, ...data}) + EOL` per event.
- The five event types delegate handles (`step_start`, `text`, `tool_use`, `step_finish`, `error`) are the complete non-reasoning set; the sixth, `reasoning`, is gated on `--thinking`, which delegate never passes.
- Underscore envelope `type` versus hyphenated `part.type` (`step-start`, `step-finish`) — `_ingest_opencode_event` (`harness_events.py:1068`) checks both, correctly.
- `tool_use` fires only on `state.status` of `completed` or `error`; `_opencode_tool_status` maps `completed` → `success` and passes the rest through.
- `error` payload shape `{error: {name, data: {message}}}` — matches `tests/fixtures/opencode/error_run.ndjson`.
- A denied tool produces no JSON event at all: without `--auto`, `run.ts` auto-rejects each `permission.asked` and prints the notice through `UI.println`. `_ingest_opencode_tool`'s comment ("OpenCode does not emit a 'permission denied' event") and `docs/security-model.md:127` both state this correctly.
- **stdout under `--format json` is pure NDJSON, so delegate's line-by-line parse is safe.** Two independent legs. The logger never targets stdout: `packages/core/src/observability/logging.ts` defines `stderrLogger` as `process.stderr.write(...)` and `--print-logs` merely adds that sink alongside the always-on file logger. And `UI.println`/`UI.print` in `packages/opencode/src/cli/ui.ts` both write to `process.stderr`, so the auto-rejection notice, the `> agent · model` header, and every error banner stay off stdout. An audit of every stdout write in `run.ts` found exactly three: the `emit()` JSON writer, and two human-readable fallbacks that sit after `if (emit(...)) continue`, which short-circuits whenever `format === "json"`.
- `variants` is declared `optional(...)` in the printed struct, so the key can be absent entirely rather than an empty object. `parse_opencode_catalog` guards with `isinstance(variants, dict)` and falls through to `capabilities.reasoning`, which is the correct handling.
- Exit codes are `0` on success and `1` on every failure, with no distinct code for permission denial, model error, or timeout. Delegate does not over-trust the stream: `runner.py:2755` derives status from the exit code and lets an accumulator terminal status override only toward `failed`/`cancelled`, so a `step_finish` with `reason: "stop"` followed by exit 1 is still reported as a failure.

Discovery:

- `opencode --pure models --verbose` is the right probe. The per-model JSON body is the local `Model` struct in `packages/opencode/src/provider/provider.ts:1074`, which at v1.18.29 still has `id`, `providerID`, `name`, `capabilities` (a struct still containing `reasoning: Schema.Boolean` at `:1029`), and `variants` as `Record<string, Record<string, Any>>` keyed by variant name. Every field `parse_opencode_catalog` reads is intact. Do not confuse this with `ModelV2.Info` in `packages/schema/src/model.ts`, a separate newer internal type whose `variants` is an array and whose `capabilities` is `{tools, input, output}` — that type is not what `models --verbose` serializes.
- Disabled variants never reach delegate: `provider.ts:1570` applies `pickBy(merged, (v) => !v.disabled)` and then `omit(v, ["disabled"])` before exposing them, so delegate cannot advertise a variant opencode has switched off.
- Install path `~/.opencode/bin` (`docs/agent-setup.md:148`) matches the official installer, which sets `INSTALL_DIR=$HOME/.opencode/bin`.
- State under `~/.local/share/opencode` (`command_help.py:583`) is correct and `--dir` does not change it. `packages/core/src/global.ts` derives every path from `xdg-basedir` (`data` = `~/.local/share/opencode`, `cache`, `config`, `state`), file storage is `Global.Path.data/storage` laid out as `session/<projectID>/<sessionID>.json`, and the SQLite database is `~/.local/share/opencode/opencode.db` on a release channel. `run` only `process.chdir()`s, so sessions accumulate in the global store partitioned by project id, never inside the isolated workspace. That means delegate's throwaway safe workspace does not contain the session, which is consistent with `docs/cli-reference.md:443`.
- No native structured output: `constants.py` gives opencode `structuredOutput: False` and `workflows/runtime._native_schema` returns `None` for it. Correct — opencode has no `--output-schema` or JSON-schema flag.

Regression check: `python3 -m pytest tests/test_harness_events.py tests/test_persona_opencode.py tests/test_model_discovery.py tests/test_harness_discovery.py -q -k 'opencode or OpenCode'` → 21 passed. Those tests pass both before and after B1, because none of them exercises a multi-slash id.

Documentation drift (not a harness mismatch): `docs/cli-reference.md:436` says `delegate models opencode --live` runs `opencode --pure models`, but the code runs `opencode --pure models --verbose`. `docs/configuration.md:587` has it right.

## Could not verify

- **Anything requiring the binary.** `opencode` is not installed on this machine and `~/.opencode/bin` does not exist, so no `--help`, `--version`, or `models` output was observed. Every harness claim above rests on the v1.18.29 source, the live docs at `https://opencode.ai/docs/cli`, or the published config schema at `https://opencode.ai/config.json`.
- **The stdout-buffering claim looks wrong, but I could not test the real binary.** `docs/cli-reference.md:441` and `command_help.py:575` say "OpenCode currently buffers stdout until completion, so progress can remain silent." A delegated experiment against Bun 1.3.14 found the opposite for the emit pattern `run.ts` uses: writes through a pipe at 300ms intervals arrived about 1ms after each write, not batched at exit. If that holds for the Bun embedded in the shipped binary, delegate's progress display should work for opencode and both doc lines are stale. What is real is the exit-time truncation in L6, which is a different failure and may be what the original observation actually was. Needs one run against an installed binary to settle.
- **Whether `--variant` is validated server-side.** The CLI passes `args.variant` straight into `session.prompt`; I did not trace the server handler, so "OpenCode silently ignores bogus variant names" (`docs/cli-reference.md:432`) remains delegate's prior observation rather than something I re-confirmed.
- **`bundled_models.py:78` fallback ids** (`opencode/claude-opus-4-5`, `opencode/gpt-5`, `anthropic/claude-sonnet-4-5`, `openai/gpt-5`). Confirming these resolve needs an authenticated `opencode models` run. `models.dev` lists 102 models under the `opencode` provider and 14 under `anthropic`, but I did not check these four ids individually.
- **`OPENCODE_AUTO_SHARE` versus config `share: "disabled"` precedence.** Both exist; which wins is not stated in the docs and I did not trace it.

## Sources

- https://github.com/anomalyco/opencode/releases/tag/v1.18.29 — latest release, 2026-09-04.
- https://opencode.ai/docs/cli — `run` flag table (`--format`, `--dir`, `--model`, `--agent`, `--variant`, `--thinking`, `--auto`, `--session`, `--continue`, `--fork`), Global Flags table (`--print-logs`, `--log-level`, `--pure`), `models` flags (`--refresh`, `--verbose`), `agent create` permission list, and the environment-variable table including `OPENCODE_CONFIG_CONTENT`, `OPENCODE_PERMISSION`, `OPENCODE_DISABLE_AUTOUPDATE`, and the three `OPENCODE_DISABLE_CLAUDE_CODE*` vars.
- https://opencode.ai/config.json — published JSON schema; established `share` enum, `autoupdate` type, `PermissionActionConfig` enum, `PermissionConfig` additionalProperties, `AgentConfig.mode`/`prompt`/`permission`.
- https://opencode.ai/docs/config — config precedence list (remote → global → `OPENCODE_CONFIG` → project → `.opencode` → `OPENCODE_CONFIG_CONTENT` → managed files → MDM) and the "merged together, not replaced" statement.
- https://opencode.ai/docs/permissions and https://opencode.ai/docs/agents — `allow`/`ask`/`deny` semantics, the permission key table, catch-all-first / last-match-wins ordering.
- `packages/opencode/src/cli/cmd/models.ts` @ v1.18.29 — `${providerID}/${modelID}` output format and the `--verbose` JSON body.
- `packages/opencode/src/provider/provider.ts` @ v1.18.29 — lines 1029, 1074, 1570: the `Model` struct printed by `models --verbose`, `capabilities.reasoning`, `variants` as a name-keyed record, and disabled-variant filtering.
- `packages/opencode/src/index.ts` @ v1.18.29 — root yargs options, `.strict()`, the `--print-logs`/`--log-level`/`--pure` middleware, and `.version(..., InstallationVersion)`.
- `packages/opencode/src/cli/cmd/run.ts` @ v1.18.29 — full `run` builder including hidden `--yolo`/`--dangerously-skip-permissions`, the `emit()` JSON writer and its six event types, stdin/positional prompt joining, permission auto-rejection, and exit-code paths.
- `packages/core/src/model.ts` @ v1.18.29 — `parse()` splitting a model ref on the first slash only.
- `packages/core/src/installation/version.ts` @ v1.18.29 — `InstallationVersion` falling back to the literal `"local"`.
- https://models.opencode.ai/api.json — the catalog opencode actually fetches (`Flag.OPENCODE_MODELS_URL` default, cached to `Global.Path.cache/models.json` with a 5-minute TTL). 213 providers, 7,560 models, 4,278 with a slash in the model id; per-provider counts for openrouter, vercel, kilo, nano-gpt, anthropic, openai, opencode. https://models.dev/api.json returns the same data and gave identical counts.
- `packages/core/src/observability/logging.ts` and `packages/opencode/src/cli/ui.ts` @ v1.18.29 — established that both the logger and `UI.println` write to stderr, leaving stdout as pure NDJSON.
- `packages/core/src/global.ts`, `packages/opencode/src/storage/storage.ts`, `packages/core/src/database/database.ts` @ v1.18.29 — XDG-derived paths, the session storage layout, and the SQLite database location.
- yargs 18.0.0 API reference, `.global()` — "Options default to being global."; corroborated by `packages/opencode/script/run-workspace-server:65` and `packages/desktop/src/main/wsl/sidecar.ts:38` using `--print-logs` in both positions.
