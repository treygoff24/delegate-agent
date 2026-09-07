# Oh My Pi (`omp`) compatibility audit

Latest version: **18.1.13** (2026-09-07, https://github.com/can1357/oh-my-pi/releases/tag/v18.1.13) · Installed here: **18.1.13**

> **Version note.** The task brief said 18.1.12. The bun global was upgraded during this audit
> (`/home/trey-agent/.bun/bin/omp` → `.../node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js`, symlink target
> mtime 2026-09-07 05:14). Both `omp --version` and `estate-omp --version` now print `omp/18.1.13`, and
> `package.json` reports `18.1.13`. **Delegate's omp lane is running the newest release, not one behind.**
> Every source quotation below was re-verified against the 18.1.13 tree after the upgrade.
> The npm package is `@oh-my-pi/pi-coding-agent` (the npm names `oh-my-pi` and `omp` are unrelated third-party packages).

Delegate assumptions checked: 34 · BROKEN: 0 · LATENT: 6 · SIMPLIFY: 3 · OK: 25

The installed npm package ships its own `src/` tree and `CHANGELOG.md`, so most claims below are
grounded in the *installed binary's own source* rather than inferred from `main`. That tree is at
`/home/trey-agent/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent/` (referred to as `$P`).

## BROKEN

None found. No flag, subcommand, event type, model-id format, or exit-code convention that Delegate
depends on has been removed or renamed between the version Delegate was written against and 18.1.13.

## LATENT

- **[L1] The `--tools read` / `--approval-mode always-ask` safety claim is pinned to omp 17.0.4 and is
  not re-verified by any check that runs by default.** Delegate:
  `/home/trey-agent/Code/delegate-agent/src/delegate_agent/argv_builders.py:71-87` states "`--tools read` is
  NOT self-enforcing in omp 17.0.4 (the write/bash/python tools still execute under it)" and makes
  `--approval-mode always-ask` the load-bearing denial; `docs/security-model.md:122` repeats "Oh My Pi 17.0.4"
  twice. Harness truth: the installed version is 18.1.13, twelve minor versions on. The behavioral test that
  would prove the boundary still binds is skipped unless opted into:
  `tests/test_omp_read_only_behavior.py` reports `SKIPPED [4] ... set DELEGATE_OMP_BEHAVIOR_TEST=1 to run the
  live omp write-probe`, so `python3 -m pytest tests/test_omp_read_only_behavior.py` passes 3 argv-shape tests
  and silently skips all four behavioral probes. The file's own docstring says the shape assertions "cannot
  catch the failure this guards". Repro: `timeout 900 python3 -m pytest tests/test_omp_read_only_behavior.py -q -rs`.
  Smallest fix: run the gated probe against 18.1.13 and update both the version string and the evidence date;
  it spends one live subscription call, which is why I did not run it here (brief forbids paid calls).
- **[L2] Work mode pins no approval flag and inherits omp's ambient `tools.approvalMode`.** Delegate:
  `src/delegate_agent/argv_builders.py:517-518` applies `PI_FAMILY_SAFE_LOCKDOWN` only for safe mode and
  `call --read-only`; work mode emits no approval flag. Confirmed by dry-run: `omp -p --no-session --mode json
  --thinking high <prompt>`. Harness: `$P/src/config/settings-schema.ts:4103-4106` gives
  `"tools.approvalMode"` the schema default `"yolo"`, so work mode auto-approves today. But a user-level or
  project-level `config.yml` setting `always-ask` or `write` silently downgrades a work run to
  read-or-read+write with no Delegate-side signal. Delegate already knows a project-local `config.yml` can
  override the CLI (`tests/test_omp_read_only_behavior.py:143` writes a hostile `approvalMode: yolo` and asserts
  the safe lockdown beats it) but does not apply the mirror-image defense to work mode. Repro:
  `python3 bin/delegate.py --json dry-run omp work "x."` — no `--approval-mode` or `--auto-approve` in argv.
  Smallest fix: emit `--approval-mode yolo` (or `--auto-approve`) for omp work mode so write capability
  is a property of the invocation, not of ambient config.
- **[L3] Delegate never passes `--cwd`, so a workspace that resolves to `$HOME` is silently redirected.**
  Delegate sets the child working directory through `subprocess.Popen(..., cwd=cwd, ...)`
  (`src/delegate_agent/runner.py:2053-2062`) and the omp argv carries no directory flag. Harness:
  `$P/src/cli/startup-cwd.ts` `maybeAutoChdir` returns early only `if (parsed.allowHome || parsed.cwd)`;
  otherwise, when the launch cwd equals the home directory, it `setProjectDir`s to the first existing of
  `~/tmp`, `/tmp`, `/var/tmp`, then `os.tmpdir()`. The run then reads and writes a temp directory while
  Delegate's manifest records the home path. Narrow (needs workspace == `$HOME`) but silent. Smallest fix:
  pass `--cwd <workspace>` in `_build_pi_family_argv`, or `--allow-home`.
- **[L4] Every bundled omp model id is absent from the live catalog.** Delegate:
  `src/delegate_agent/bundled_models.py:89-93` lists `openai-codex/gpt-5.6-sol`, `anthropic/claude-opus-4-8`,
  `anthropic/claude-sonnet-5` (copied verbatim from the `pi` entry above it). The live catalog on this install
  holds 703 models across providers `cerebras, fireworks, google, kimi-code, opencode-go, openrouter, xai, zai`
  — no `anthropic/*` and no `openai-codex/*` at all. Repro:
  `python3 bin/delegate.py --json models omp --live` and compare `source: "bundled"` ids against
  `source: "live"` ids; all three report `NOT in live catalog`. Effect: with discovery unavailable, Delegate
  offers three ids that `--model` cannot serve. Smallest fix: replace the bundled trio with ids that exist in
  the omp catalog, or drop the bundled entry so the absence of discovery is visible rather than papered over.
- **[L5] Delegate passes alias-resolved model ids to `--model` without validating them against discovery,
  and omp's fuzzy resolver will silently substitute.** This is an **environment/config finding**, not a repo
  defect: `config.example.json` ships `"omp": {"models": {}}`, and the aliases live in the operator's local
  Delegate config. On this machine 9 of 9 configured omp aliases resolve to ids absent from the 703-model live
  catalog, with a near neighbour under a different provider in every case:

  | alias | configured target | present in catalog | nearest live id |
  |---|---|---|---|
  | deepseek | `opencode-go/deepseek-v4-pro` | no | `fireworks/deepseek-v4-pro` |
  | gemini | `google/gemini-3.7-flash` | no | `google/gemini-3.6-flash`, `google/gemini-3.8-flash` |
  | glm | `opencode-go/glm-5.3` | no | `fireworks/glm-5.3`, `opencode-go/glm-5.3-flash` |
  | grok | `xai-oauth/grok-4.6` | no (provider `xai-oauth` has 0 models) | `opencode-go/grok-4.6` |
  | kimi | `kimi-code/k3` | no | `kimi-code/k3-256k`, `fireworks/kimi-k3` |
  | minimax | `opencode-go/minimax-m3` | no | `fireworks/minimax-m3` |
  | muse | `openrouter/meta/muse-spark-1.3-contributor` | no | `opencode-go/muse-spark-1.3-contributor` |
  | ox | `openrouter/stealth/ox-alpha` | no | `opencode-go/ox-alpha-free` |
  | qwen | `opencode-go/qwen3.8-max` | no | `fireworks/qwen3.8-max` |

  Harness: `docs/models.md` §"Runtime model resolution" resolves `--model` by exact `provider/modelId` first,
  then exact bare id, then a **provider-scoped fuzzy then substring** pass. So a stale exact-form id does not
  reliably fail — it can fall through to a different concrete model. Delegate's own dry-run confirms it does
  no catalog check: `python3 bin/delegate.py --json dry-run omp work --model nosuch/model-xyz "x."` returns
  `"ok": true` with `--model nosuch/model-xyz` in argv. Combined with `continuityMode: "fungible"` for omp,
  a substitution is not treated as a violation. Repro: the `--live` listing above. Smallest fix (repo side):
  warn at request-build time when a resolved omp model id is absent from a fresh discovery catalog.
  Operator side: repoint the aliases at ids that `omp models --json` actually lists.
- **[L6] `notice` events are dropped, including `level: "error"`.** Delegate:
  `src/delegate_agent/harness_events.py:1178-1279` (`_ingest_pi_event`) handles `auto_retry_*`,
  `auto_compaction_*`, `turn_start`, `message_update`, `message_end`, `tool_execution_start`,
  `tool_execution_end`, `turn_end`, and `error`, then returns. Harness:
  `$P/src/session/agent-session-events.ts:58` defines
  `{ type: "notice"; level: "info" | "warning" | "error"; message: string; source?: string }`, which
  `--mode json` prints unmodified (it hits `printableEvent`'s `default:` branch). A session-layer error notice
  therefore never reaches the accumulator's error text or the completion report. Smallest fix: map
  `notice` with `level === "error"` onto `_ingest_error_event`.

## SIMPLIFY

- **[S1] omp now reads a piped prompt from stdin; Delegate's whole argv-transport special case for omp can go.**
  Delegate carries argv transport for omp on an explicit, now-false premise —
  `docs/cli-reference.md:155-157`: "Oh My Pi receives a positional prompt because 17.0.4 did not consume piped
  stdin in the verified non-interactive invocation." Harness truth on 18.1.13, from the installed source:
  `$P/src/main.ts:1479-1481` reads stdin for every non-protocol mode, and `--mode json` is not a protocol mode —

  ```ts
  const isProtocolMode = mode === "rpc" || mode === "rpc-ui" || mode === "acp";
  // Protocol modes own stdin; treating it as prompt text would consume JSON-RPC frames before their transports start.
  const pipedInput = isProtocolMode ? undefined : await logger.time("readPipedInput", readPipedInput);
  ```

  `$P/src/cli/initial-message.ts` then uses `stdinContent` directly as `initialMessage` when no argv message is
  present. `docs/cli-reference.md` upstream states it outright: "Non-TTY stdin is read automatically as the
  initial prompt; do not add a `-` marker", with the example `echo "review this diff" | omp -p`.
  Deletable if omp moves to `PROMPT_TRANSPORT_STDIN`: `OMP_PROMPT_REDACTION`
  (`src/delegate_agent/prompt_transport.py:5`), omp's membership in `ARGV_PROMPT_TRANSPORT_ENGINES`
  (`prompt_transport.py:44`, which also drops omp from the 100 KiB `ARGV_PROMPT_GUARD_BYTES` ceiling and the
  resume final-prompt size guard), the flag-like prompt guard and its 12-line comment
  (`argv_builders.py:523-536`), the `prompt` parameter threaded through `_build_pi_family_argv` /
  `build_omp_argv` (`argv_builders.py:495-580`), the omp branch in `describe_payload.py:923`, and the
  `engine in {"codex", "omp"} and prompt_transport == "argv"` branch in `mail_core.py:395`. It also makes omp
  match `pi`, which already uses `PROMPT_TRANSPORT_STDIN` in the same builder. Estimated deletion: ~40-60 lines
  across 5 files, plus the doc paragraphs at `docs/cli-reference.md:155-157` and `docs/configuration.md:639`.
  Two operational caveats to carry into the change: `readPipedInput` treats whitespace-only stdin as absent
  (`$P/src/main.ts:202-224`), and it blocks until EOF. Delegate already closes the pipe, and today's
  `stdin=subprocess.DEVNULL` for argv transport (`runner.py:2057`) is why omp does not currently hang.
- **[S2] If argv transport is kept, omp honors `--` and the flag-like prompt rejection can be replaced by it.**
  Delegate raises `DelegateError("pi_family_prompt_flag_like", ...)` for any omp prompt whose first character is
  `-` or `@` (`argv_builders.py:523-536`), justified as "argv transport has no end-of-options separator".
  Harness: `$P/src/cli/args.ts:293-297` —

  ```ts
  } else if (arg === "--") {
      // POSIX positional separator: drop the token and switch the loop
      // into "everything from here is a positional" mode.
      sawSeparator = true;
  ```

  and `args.ts:163-167` pushes every subsequent token straight to `result.messages` with no flag parsing and no
  `@` sigil handling (the `@` branch at `args.ts:281` is inside the same `if (sawSeparator) continue` guard).
  Upstream `docs/cli-reference.md` agrees: "`--` ends flag parsing; everything after it is literal message text,
  even if it looks like a flag." So appending `"--"` before the prompt makes both hazards impossible and removes
  a rejection that currently blocks legitimate prompts (a review brief opening with `--- diff` fails today).
  Estimated deletion: the 18-line guard plus its test at `tests/test_engine_argv.py:2967-2982` (`test_omp_rejects_flag_like_prompt_on_argv_transport`,
  whose own comment repeats the false premise verbatim). Prefer S1;
  S2 is the cheap fallback.
- **[S3] `@file` is a first-class prompt-file transport.** `$P/src/cli/file-processor.ts` inlines a text file
  as `<file name="/abs/path">…</file>` into the initial message, and `$P/src/cli/args.ts:281-289` collects
  `@`-prefixed argv into `fileArgs`. This would give omp the same `--prompt-file`-shaped path Droid and Grok
  use, avoiding ARG_MAX entirely. Two hard edges that make S1 the better choice: a missing file is a fatal
  `process.exit(1)`, and a file over `MAX_CLI_TEXT_BYTES` (5 MB) is **silently degraded** to a
  `(skipped: too large, N)` stub rather than failing. Listed for completeness; I do not recommend it.

## OK (verified)

Flags, all present in `omp --help` on 18.1.13 and in `$P/src/cli/args.ts`:

- `-p, --print` — "Non-interactive mode: process prompt and exit". Emitted first by
  `argv_builders.py:509`. Verified in `omp --help`.
- `--mode json` — "Output mode: text (default), json, rpc, or rpc-ui". `args.ts:23` declares
  `export type Mode = "text" | "json" | "rpc" | "acp" | "rpc-ui"`. Two-token form (`--mode json`) and
  `=` form both parse (`args.ts:179-185` splices the `=` value into the next slot).
- `--no-session` — "Don't save session (ephemeral)". Present in `args.ts`; correctly dropped by Delegate
  when a `resume_session_id` is set (`argv_builders.py:510-514`).
- `--resume=<id>` — `-r, --resume=<value>` in `--help`; `flag-tables.ts:246` registers
  `"--resume": { set: setResume, rejectEmpty: true }`, and `args.ts:179-185` handles the `=` form Delegate emits.
- `--model` — "Model to use (fuzzy match)". Selector format `provider/modelId` confirmed.
- `--thinking` — "Set thinking level: off, minimal, low, medium, high, xhigh, max, auto". Delegate maps
  `--reasoning-effort` straight through (`reasoning.py:132-136`, `TRANSPORT_PI_THINKING_FLAG`).
- Safe lockdown flags all still exist: `--tools`, `--no-extensions`, `--no-skills`, `--no-rules`, `--no-lsp`,
  `--approval-mode` (`always-ask|write|yolo`). Confirmed in `omp --help` and `launch-help.ts:106`.
- Delegate correctly never emits `--smol`, `--slow`, `--plan`, `--prewalk*`, `--plan-yolo*`
  (`tests/test_model_discovery.py:540` asserts this; `docs/cli-reference.md:493` documents it).

Reasoning effort:

- `PI_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")` (`reasoning.py:78`) is a strict subset of
  omp's accepted `--thinking` values. Verified end to end: `low|medium|high|xhigh|max` each dry-run to the
  matching `--thinking` token with `ok: true`; `none|minimal|off|auto` are rejected by Delegate before launch.
  Delegate cannot express omp's `off`, `minimal`, or `auto`, but never emits an invalid value. Omitting
  `--thinking` leaves omp on its own default.

Version detection and model discovery:

- `harness_discovery.py:93` `re.compile(r"^omp/[0-9][0-9A-Za-z.+-]*")` matches the real banner. Both
  `omp --version` and the estate wrapper `estate-omp --version` print `omp/18.1.13`, so the wrapper does not
  break identification.
- `_probe_omp` (`harness_discovery.py:1365-1368`) runs `omp models --json --no-extensions`. All three tokens are
  valid: `omp models --help` documents `--json  Output JSON` and `--no-extensions`.
- `parse_omp_catalog` (`harness_discovery.py:789-828`) reads `payload["models"]` as a list and keys on
  `entry["selector"]`. Verified against real output: top-level key is `models`, and each entry has exactly
  `{provider, id, selector, name, contextWindow, maxTokens, reasoning, thinking, input, cost}`. `selector` is
  the canonical `provider/modelId` string that `--model` accepts.
- The `thinking` array is read as the per-model effort enum with `evidence: "exact"`, and the boolean
  `reasoning` field as the fallback. Both fields are present in real entries, e.g.
  `{"provider":"fireworks","id":"glm-5.1","selector":"fireworks/glm-5.1","reasoning":true,"thinking":["minimal","low","medium","high","xhigh"],...}`.
- `delegate --json models omp --live` returns 703 live models plus config aliases, exit 0.
- Three-segment selectors (`openrouter/meta/muse-spark-1.3`) round-trip through `parse_omp_catalog` unharmed,
  because it keys on the whole `selector` string rather than splitting on `/`.

JSON event stream (`$P/src/modes/print-mode.ts`, re-read on 18.1.13):

- Framing is newline-delimited JSON on stdout, one object per line, written through a serialized
  `writeStdoutLine` that blocks shutdown on the drain — so the final `agent_end` is not truncated.
- Delegate's parsed event types all still exist in `$P/src/agent` types and
  `$P/src/session/agent-session-events.ts`: `turn_start`, `turn_end`, `message_update`, `message_end`,
  `tool_execution_start`, `tool_execution_end`, `error`, `auto_retry_start`, `auto_retry_end`,
  `auto_compaction_start`, `auto_compaction_end`.
- `message_update` → `assistantMessageEvent.delta` for `text_delta` (`harness_events.py:1219-1225`) is correct
  under print mode. `printableEvent` strips only `partial` from the stream event and drops the outer `message`
  snapshot; `delta` survives. Delegate reads only `delta`, never `partial`, so the print/RPC divergence does
  not bite it.
- `stream_capture._thinking_only` (`stream_capture.py:41-57`) requires the record's key set to be exactly
  `{"type","assistantMessageEvent"}` with inner keys exactly `{"type","contentIndex","delta"}`. That is
  precisely the shape `printableEvent` produces for a `thinking_delta` in print mode. The thinking-compaction
  path is therefore still armed.
- `_capture_session_id` reads `payload["id"]` on the `session` event (`harness_events.py:716-717`). Harness:
  print-mode emits `session.sessionManager.getHeader()` as the first line in JSON mode, and `docs/session.md`
  gives that header `{"type": "session", "version": 3, "id": "...", ...}`. Note the emission is guarded by
  `if (header)`, so Delegate must not require it — and it does not.
- `turn_end` carries `message.stopReason` and `message.errorMessage`; Delegate's status mapping
  (`stop`→succeeded, `aborted`→cancelled, `error`/`length`/HTTP `errorStatus >= 400`→failed) matches the
  `StopReason` union in `$P/src/ai` types.
- `stall_watchdog._classify_pi` keying on `_delta`/`_end` suffixes of `assistantMessageEvent.type` matches the
  `AssistantMessageEvent` union (`text_delta`, `thinking_delta`, `toolcall_delta`, `text_end`, `thinking_end`,
  `image_end`, `toolcall_end`). Its `_PI_BOUNDARY_TYPES` set (`turn_start`, `turn_end`, `message_start`,
  `message_end`, `agent_end`, …) are all real top-level types.
- `retry_fallback_succeeded` carries a `model` field (`agent-session-events.ts:49`), which
  `_served_model` (`harness_events.py`) picks up via its generic `"model"` key lookup, so a completed
  fallback hop is recorded as a model observation. `retry_fallback_applied` (`{from, to, role}`) has no `model`
  key and is not recorded, but it is the *attempt*, not the outcome — the successful hop is captured.

Exit codes and failure detection:

- **`--mode json` never exits non-zero on a failed turn.** The `stopReason === "error" || "aborted"` →
  `process.exit(1)` block in `$P/src/modes/print-mode.ts:188-218` is inside `if (mode === "text")`. Delegate
  does not rely on the exit code: `runner.py:3076` and `runner.py:4577` special-case
  `accumulator.harness in {"pi", "omp"} and terminal_status == "failed"`, and `_ingest_pi_event` derives
  terminal status from `turn_end`. This is the right design for this harness; recorded here so a future
  refactor does not "simplify" it into an exit-code check.
- Exit `2` is omp's CLI usage error for an unrecognized flag, added specifically so a typo'd flag cannot leak
  into the prompt and start a real LLM session (`$P/src/main.ts`, issue #2459). Delegate emits no unrecognized
  flags.
- Exit `1` covers `@file` not found and "No models available".

Process wiring:

- `subprocess.DEVNULL` for the child's stdin under argv transport (`runner.py:2057`) gives immediate EOF, so
  `readPipedInput`'s EOF wait cannot hang the omp lane today. This is load-bearing and currently incidental —
  if S1 is taken, stdin becomes a real pipe and the existing `_write_stdin` close path is what keeps it bounded.
- `stream_capture` limits (256 MB transport, 16 MB per record, 64 KB thinking sample) remain appropriate:
  print mode's `printableEvent` docstring explains it exists precisely because a long turn "used to
  re-serialize its whole in-progress message on every streamed delta, producing multi-GB logs".
- Install path: bun global at `/home/trey-agent/.bun/bin/omp`, matching the README's "Bun (recommended)"
  path `bun install -g @oh-my-pi/pi-coding-agent`. `config.example.json` sets `"binary": "omp"`; the local
  estate config overrides it to the realm-scoping wrapper `estate-omp`, which is transparent to Delegate.
- `config.example.json` omp section (`{binary, defaultModel, defaultReasoningEffort, models}`) and the
  `config.py:150-155` defaults are internally consistent and validated by `_validate_pi_family_section`.

## Could not verify

- **Whether `--approval-mode always-ask` still denies write/exec in headless `-p` on 18.1.13.** This is the
  single security-load-bearing behavior of omp safe mode. Proving it requires a live subscription call, which
  the brief forbids. The repo already ships the right instrument; it is gated:
  `DELEGATE_OMP_BEHAVIOR_TEST=1 python3 -m unittest tests.test_omp_read_only_behavior`. Someone with budget
  should run it before the next release. See [L1].
- **An observed `--mode json` event sample from a real run.** All schema claims above come from the installed
  TypeScript source and upstream `docs/rpc.md` / `docs/session.md`, not from a captured transcript. The
  print-mode shaping (`printableEvent`) is source-exact; the ordering and completeness of a real stream is not
  independently confirmed here.
- **Whether `--tools read` became self-enforcing between 17.0.4 and 18.1.13.** If it did, the safe lockdown is
  belt-and-braces rather than single-point; if it did not, [L1] is the only thing standing between omp safe
  mode and write capability. The same gated probe answers this.
- **A repo-wide grep of upstream `docs/` for an exit-code table.** GitHub's code-search API returns
  `401 Requires authentication` unauthenticated. The "no documented exit-code table" conclusion rests on the
  fetched doc set plus source reading, which I would call well-supported but not exhaustive.

## Sources

- `omp --version`, `estate-omp --version`, `omp --help`, `omp models --help`, `omp models --json` — installed
  18.1.13 ground truth for every flag and the catalog entry shape.
- `/home/trey-agent/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent/` (`package.json`,
  `CHANGELOG.md`, `src/main.ts`, `src/cli/args.ts`, `src/cli/flag-tables.ts`, `src/cli/initial-message.ts`,
  `src/cli/file-processor.ts`, `src/cli/startup-cwd.ts`, `src/modes/print-mode.ts`,
  `src/session/agent-session-events.ts`, `src/session/session-tools.ts`, `src/config/settings-schema.ts`) —
  the installed binary's own source; established stdin transport, the `--` separator, `printableEvent`
  shaping, the `mode === "text"` exit-code guard, the `tools.approvalMode` default, and home auto-chdir.
- https://github.com/can1357/oh-my-pi/releases/tag/v18.1.13 — latest release, published 2026-09-07T00:45:11Z;
  its three fixes (Herdr pane notifications, dotenv launch provenance, Astra context window) touch no interface
  Delegate uses.
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.1.13/docs/cli-reference.md — "Non-TTY stdin is read
  automatically as the initial prompt; do not add a `-` marker"; "`--` ends flag parsing".
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.1.13/docs/models.md — `--model` resolution precedence
  (exact `provider/modelId`, exact bare id, retired alias, provider-scoped fuzzy then substring).
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.1.13/docs/session.md — session header schema.
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.1.13/docs/rpc.md — event stream schema and the
  `agent_end` / `isTerminal` rule.
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.1.13/README.md — install commands, `bun ≥ 1.3.14`.
- `npm view @oh-my-pi/pi-coding-agent` — package identity and `latest: 18.1.13`.
- `python3 bin/delegate.py --json dry-run omp {safe,work} ...` and
  `python3 bin/delegate.py --json models omp --live` — Delegate-side argv, alias resolution, catalog contents.
- `timeout 900 python3 -m pytest tests/test_omp_read_only_behavior.py -q -rs` — 3 passed, 4 skipped.
