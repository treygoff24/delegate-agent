# Cursor Agent compatibility audit

Latest version: `2026.09.02-c22c1a3` (https://cursor.com/install, line 85 pins
`DOWNLOAD_URL=".../lab/2026.09.02-c22c1a3/..."`) · Installed here: `2026.09.02-c22c1a3`
(`cursor-agent --version`; `cursor-agent about --format json` reports
`"latestStatus":"up_to_date"`).

Delegate assumptions checked: 24 · BROKEN: 2 · LATENT: 4 · SIMPLIFY: 3 · OK: 15

**Binary-name note (assigned to me by the coordinator).** On this machine
`/home/trey-agent/.local/bin/agent` is a symlink to `/home/trey-agent/.grok/bin/agent`
(`agent --version` → `grok 1.0.13 (5e9a58528b76) [stable]`). Cursor is nonetheless installed
and reachable as `/home/trey-agent/.local/bin/cursor-agent`. Cursor's own installer creates
**both** names and labels them (https://cursor.com/install, line 129:
`# Create symlinks to the Cursor Agent executable (primary: agent, legacy: cursor-agent)`),
so Grok's later install (Sep 4) clobbered Cursor's primary symlink (Sep 2).

**`harness_discovery.py` does fingerprint the binary, and it survives this collision.**
`_PATH_CANDIDATES["cursor"] = ("agent", "cursor-agent")` (`harness_discovery.py:82`) probes
`agent` first, `_identify_version` reads the banner, `_AMBIGUOUS_VERSION_BASENAMES =
frozenset({"agent"})` (`harness_discovery.py:115`) forbids the bare-version fallback for that
basename, and the branded Grok pattern (`harness_discovery.py:85`) positively identifies it as
Grok. Because the embedded default `argvPrefix` is also `["agent"]`, the candidate is
non-explicit, so delegate warns and falls through instead of failing. Verified:

```
$ PYTHONPATH=src python3 -c "import os; from delegate_agent import harness_discovery, config as c; \
  print(harness_discovery.resolve_harness_selector(c.embedded_default_config(),'cursor',env=os.environ))"
SelectorResolution(selector=('/home/trey-agent/.local/bin/cursor-agent',),
  version='2026.09.02-c22c1a3', error=None,
  warnings=("selector '/home/trey-agent/.local/bin/agent' identifies itself as grok, not cursor",),
  identified_harness=None)
```

So: correct selector, correct version, one accurate warning. See [L4] for the residual risk.
Trey's live config sidesteps the collision entirely with `argvPrefix: ["estate-cursor"]`.

---

## BROKEN

### [B1] `--continuity-mode pinned` fails 100% of Cursor runs: Cursor reports a display name, delegate compares it to a model id

`harness_events.py:391` `_served_model` reads `payload["model"]`; `harness_events.py:642`
`_observe_model` compares it to `requested_model` and, under `continuity_mode == "pinned"`,
raises `served_model_mismatch` and emits a `model.continuity_paused` terminal failure.

Cursor's `system`/`init` event carries the model's **display name**, not the id delegate passed
to `--model`. Verified live at `2026.09.02-c22c1a3` (invoked with `--model composer-2.5`):

```json
{"type":"system","subtype":"init","apiKeySource":"login","cwd":"/home/trey-agent/Code/delegate-agent",
 "session_id":"29e13e2f-2ddd-49c7-a5d5-8b1d6b672db5","model":"Composer 2.5","permissionMode":"default"}
```

The id/display-name split is systematic across the whole catalog, not one model:
`cursor-agent models` prints `composer-2.5 - Composer 2.5`, `cursor-grok-4.6-xhigh - Cursor Grok
4.6 Extra High`, and so on for all 211 entries.

Repro, no paid call — replaying the documented and live-confirmed event shapes through
`StreamAccumulator`:

```
--- continuity_mode=pinned ---
  session_id      : None
  continuity_viol : {'reason': 'served_model_mismatch', 'requestedModel': 'composer-2.5',
                     'servedModel': 'Composer 2.5', 'turn': 1, ...}
  terminal        : {'event': 'model.continuity_paused', 'status': 'failed',
                     'reason': 'pinned model composer-2.5 was replaced by Composer 2.5'}
  completion_text : None
  usage           : None
```

The violation short-circuits `_ingest_object` (`harness_events.py:549-550`), so the run loses the
session id, the assistant text, the completion, and the usage record — everything after the first
event. The same replay under the default `fungible` mode succeeds and yields
`completion_text='Done.'`, which is why this has stayed hidden: only pinned runs hit it.

**Smallest fix:** in `_observe_model`, skip the mismatch check when `self.harness == "cursor"`, or
compare against the catalog's `displayName` for the requested id (delegate already parses that map
in `parse_cursor_catalog`, which returns `{"composer-2.5": {"displayName": "Composer 2.5"}}`).
The second is strictly better because it keeps real Cursor model switches detectable.

### [B2] Every Cursor tool event is normalized as an unnamed, target-less `tool.started`, and `_ingest_cursor_tool` is dead code

`harness_events.py:612` dispatches Cursor tool activity on `event_type in ("tool_call.started",
"tool_call.completed")` — a **dotted** `type`. `event_type` is the raw `payload.get("type")`
(`harness_events.py:542`); nothing concatenates `subtype`. Cursor does not emit a dotted type.
Live capture at `2026.09.02-c22c1a3`:

```json
{"type":"tool_call","subtype":"started","call_id":"tool_27554611-...",
 "tool_call":{"readToolCall":{"args":{"path":"/home/trey-agent/Code/delegate-agent/CLAUDE.md"}},
 "hookAdditionalContexts":[],"toolCallId":"tool_27554611-...","startedAtMs":"1788758128695"},
 "model_call_id":"...","session_id":"...","timestamp_ms":1788758128721}
```

This matches the documented schema (`type: "tool_call"`, `subtype: "started" | "completed"`,
https://cursor.com/docs/cli/reference/output-format). So real events fall through to the *generic*
`_ingest_tool_call` at `harness_events.py:597`/`1325`, which reads the tool name from the payload's
**top level** (`tool` / `name` / `toolName` — all absent; the name is nested) and the target from
the top level too (also absent).

There is a second, independent layer to this: even if the dispatch were fixed,
`_ingest_cursor_tool` (`harness_events.py:1334`) reads `_string_field(tool_call, "name", "tool")`
and `_tool_target(tool_call)`, but the real `tool_call` object has no `name` key and no top-level
`args` — the tool is identified by the **key itself** (`readToolCall`, `writeToolCall`, …) with
`args` one level deeper. Both layers verified against the live payload:

```
DISPATCHED (real dotless type):
   tool.started | tool= 'tool' | target= None | status= None
   tool.started | tool= 'tool' | target= None | status= None
DEAD HANDLER, if it were reachable:
   tool.started | tool= 'tool' | target= None | status= None
   tool.completed | tool= 'tool' | target= None | status= 'success'
```

User-visible effect today: `--progress` and the run manifest show two anonymous `tool` entries per
file read instead of one `Read README.md` started/completed pair; `tool.completed` never appears
for Cursor, so nothing has a success status. No fixture covers this — `rg 'tool_call\.started'
tests/` returns nothing, and the four `{"type":"tool_call"...}` fixtures in `tests/` all use the
flat non-Cursor shape.

**Smallest fix:** dispatch on `(type, subtype)` for Cursor, and in `_ingest_cursor_tool` derive the
tool from the single `*ToolCall` key of the `tool_call` object, reading `args` from inside it.

---

## LATENT

### [L1] Cursor `safe` mode has no harness-level read-only enforcement, and Cursor now offers one

`build_cursor_argv` (`argv_builders.py:147`) always emits `-p --trust`
(`argv_builders.py:160`) and, for `safe`, simply omits `--approve-mcps --force`. The only
read-only signal is the `"Delegate review mode"` prompt prefix
(`argv_builders.py:40`, body at `argv_builders.py:31`). But Cursor documents `-p, --print` as *"Print
responses to console (for scripts or non-interactive use). **Has access to all tools, including
write and shell.**"* (https://cursor.com/docs/cli/reference/parameters). Safe mode is therefore
enforced by persuasion plus the copy-based workspace isolation, not by the harness.

Cursor now ships `--mode <mode>`: *"plan: read-only/planning (analyze, propose plans, no edits).
ask: Q&A style for explanations and questions (read-only)."* (`cursor-agent --help`;
https://cursor.com/docs/cli/reference/parameters). Delegate emits `--mode` nowhere — `rg -- '--mode'
src/delegate_agent/argv_builders.py` has no Cursor hit. This is latent rather than broken because
temporary safe isolation contains the blast radius, but it is the one harness where delegate's
read-only claim rests on prompt text alone.

### [L2] `thinking` events exist in print mode despite the docs saying they do not

https://cursor.com/docs/cli/reference/output-format states thinking events "are suppressed in
print mode and will not appear in any output format." False at `2026.09.02-c22c1a3` — my live
`--output-format stream-json` run emitted `{"type":"thinking","subtype":"delta","text":"...",
"timestamp_ms":...}` deltas and a `{"type":"thinking","subtype":"completed",...}`.

Delegate is safe today: `thinking` matches no branch and hits the deliberate drop at
`harness_events.py:637`. The latency is that the docs invite a future contributor to assume these
never arrive, and `structured_events_seen` is incremented for each one, so a Cursor run that
produced only thinking output would look structurally healthy.

### [L3] `--resume` with a fresh prompt in headless mode is undocumented

`build_cursor_argv` emits `--resume <session_id>` before the prompt
(`argv_builders.py:170-171`), and `workflows/runtime.py:53` lists `cursor` in
`STRUCTURED_RESUME_ENGINES`. Cursor documents `--resume [chatId]` only as *"Resume a chat
session"* / *"Select a session to resume"* — nothing about combining it with `-p` and a new
positional prompt. I confirmed the **argv parses** (delegate's exact flag order reaches model
validation and fails only on a deliberately bogus `--model`, exit 1), but not that the resumed
turn carries the new prompt. Verifying that needs two paid calls.

### [L4] The `agent`/`cursor-agent` name race is install-order-dependent, and the docs now favour the colliding name

Cursor's current docs never write `cursor-agent` — https://cursor.com/docs/cli/acp says *"Points
to the `agent` binary. The default install path is `~/.local/bin/agent`."* Cursor's installer calls
`agent` **primary** and `cursor-agent` **legacy**. So a Cursor reinstall on this box would reclaim
`~/.local/bin/agent` and break Grok, and delegate's `cursor` resolution would silently flip back to
the first candidate. Delegate's fingerprint makes each individual state correct, but the resolved
selector is a function of which vendor installed last.

Two stale references follow from this: `README.md:71` tells users to verify Cursor with
`command -v agent` (which prints Grok here), and `docs/agent-setup.md:34` runs
`command -v agent || echo "Cursor Agent CLI missing"` — a check that passes while pointing at the
wrong vendor. Also note `docs/cli-reference.md` and `README.md:71` say Cursor's default model is
"Cursor Composer"; `config.example.json` pins `composer-2.5`, which is live and correct, but
`cursor-agent`'s own default is `auto`.

Separately, delegate's Cursor version regex `^\d{4}\.\d{2}\.\d{2}-[0-9a-f]+$`
(`harness_discovery.py:94`) is the *only* unbranded pattern in the table, which is precisely why
the `_AMBIGUOUS_VERSION_BASENAMES` guard has to exist. That design is sound and well-commented; it
is worth keeping in mind that Cursor publishes no semver anywhere, so nothing tighter is available.

---

## SIMPLIFY

### [S1] Cursor accepts the prompt on stdin — the argv-transport carve-out can be deleted

`README.md:19-20` and `docs/cli-reference.md:158` both state that "Cursor Agent currently only
exposes positional prompt input." **That is false at `2026.09.02-c22c1a3`.** Verified live with no
argv prompt at all:

```
$ printf '%s\n' 'Reply with exactly the token ZQ7X and nothing else.' \
  | cursor-agent -p --trust --mode ask --model composer-2.5 --output-format stream-json
{"type":"user","message":{"role":"user","content":[{"type":"text",
  "text":"Reply with exactly the token ZQ7X and nothing else."}]},"session_id":"29e13e2f-..."}
{"type":"result","subtype":"success",...,"is_error":false,"result":"ZQ7X",...}
```

The `user` event echoes the piped text as the prompt and the model answers it; exit 0. The docs
corroborate that piped stdin is a first-class input path — https://cursor.com/docs/cli/reference/output-format
says `--output-format` "is only valid when printing (`--print`) or when print mode is inferred
(non-TTY stdout or **piped stdin**)", and the CLI changelog records *"Headless hang fixed. `-p` runs
no longer block when spawned with an open stdin pipe (Node, Python, CI runners)"*
(https://cursor.com/docs/cli/changelog).

This is the highest-value deletion available for this harness, because the carve-out is a
**security** compromise, not just code: delegate currently puts the full prompt in the child's
process argv, where any local process can read it, and can only redact it in delegate's *own*
output. `README.md:19-22` says so outright.

Deletable / changed:
- `prompt_transport.py:3` `CURSOR_PROMPT_REDACTION` and every consumer.
- `prompt_transport.py:44` drop `"cursor"` from `ARGV_PROMPT_TRANSPORT_ENGINES`, which also
  retires the 100 KiB `ARGV_PROMPT_GUARD_BYTES` warning path for Cursor
  (`prompt_transport.py:45`, `docs/cli-reference.md:564`).
- `argv_builders.py:173,175` stop appending `prompt` positionally.
- `request_build.py:289` the "load-bearing for cursor/droid/kimi" no-mutation comment narrows.
- Doc corrections: `README.md:19-22`, `docs/cli-reference.md:108,158-160`.

Cursor would join Codex/Claude/OpenCode/Pi on the stdin path, which delegate already implements.

### [S2] `--mode plan` / `--mode ask` replace the prompt-prefix-only safe contract

Per [L1]. Emitting `--mode plan` for `cursor safe` (and for `cursor call --read-only`) would move
the read-only boundary from prompt text into the harness. It does not let delegate delete the
prefix — the prefix also shapes the report format — but it converts a soft constraint into an
enforced one and would let `docs/cli-reference.md:198-200` stop describing Cursor as a harness with
no permission/edit-capability controls.

### [S3] `-p` and `--print` are both emitted; one is redundant

`argv_builders.py:160` appends `-p`, then `argv_builders.py:173` appends `--print` on the
stream-capture path. They are the same flag (`-p, --print`). Harmless — verified accepted together
(the run reached model validation and failed only on the bogus model) — but the text path
(`argv_builders.py:175`) emits only `-p`, so the two branches disagree for no reason. One-line fix.

---

## OK (verified)

- **`--workspace <path>`** — documented (https://cursor.com/docs/cli/reference/parameters); present in `cursor-agent --help`.
- **`-p` / `--print`** — documented; delegate's usage matches, including that `--output-format` requires it.
- **`--trust`** — documented as "Trust the workspace without prompting (headless mode only)"; needed, since without it a headless run can hit the trust wall and still exit 0.
- **`--force`** — documented (`--yolo` alias); delegate uses the long form for `work` and non-read-only `call`.
- **`--approve-mcps`** — present in `cursor-agent --help` and the parameters reference.
- **`--output-format stream-json` / `text`** — both documented and accepted.
- **`--model <id>`** — accepted; delegate's ids validated against the live catalog.
- **`--resume <chatId>` argv position** — delegate's exact flag order parses (reaches model validation).
- **No `-b` / `--background` flag exists**, and delegate emits none — absent from `cursor-agent --help` and from the parameters reference. Nothing to fix.
- **Exit codes** — success 0; invalid model 1 with the message on **stderr** and stdout empty. Matches https://cursor.com/docs/cli/reference/output-format ("the process exits with a non-zero code and writes an error message to stderr… No well-formed JSON object is emitted in failure cases"). Delegate's text-fallback ingestion handles the non-JSON stderr case.
- **`parse_cursor_catalog` against live `cursor-agent models`** — parses all 211 entries with **zero warnings**, `defaultModel: "auto"`, and correct route inference, e.g. `cursor-grok-4.6-xhigh → {"routeFamily":"cursor-grok-4.6","routeEffort":"xhigh","reasoning":{"supported":["xhigh"],"default":"xhigh","evidence":"inferred-route"}}`. The `<id> - <Display Name>` line format the parser expects is exactly what the binary prints. This is the module I most expected to be broken; it is clean.
- **`model_discovery.py:290` `parse_cursor_models_output`** — delegates to the same parser via `strip_ansi`; the live output is ANSI-free, so the strip is defensive and harmless.
- **`bundled_models.py:55-61` Cursor ids all still exist** — `composer-2.5`, `cursor-grok-4.6-xhigh`, `cursor-grok-4.6-xhigh-fast`, `gpt-5.5-high`, `claude-opus-4-8-thinking-high` each matched a live catalog line. Same for the fixed-effort map at `request_build.py:2604-2605`. (Note: there is no `composer-1` and no bare `grok-4.6` in Cursor's catalog; delegate does not reference either.)
- **`result` event handling** (`harness_events.py:937`) — reads `is_error`, `result`, and `usage`. The `usage` object is undocumented but real: live `{"inputTokens":…, "outputTokens":…, "cacheReadTokens":0, "cacheWriteTokens":0}`, normalized to `{'basis':'reported', …}` as `docs/cli-reference.md:1276` claims.
- **`session_id` capture** (`harness_events.py:712`) — Cursor emits `session_id` on both `system` and `result`; delegate reads both plus `chat_id`/`chatId` fallbacks. Confirmed captured in replay.
- **`assistant` / `user` content-block ingestion** — the live message shape `{"message":{"role":…,"content":[{"type":"text","text":…}]}}` produces the correct completion text.
- **No native structured output for Cursor** — `_native_schema` (`workflows/runtime.py:3751`) handles Codex only. Correct: Cursor ships no `--json-schema` equivalent, so there is no analogue of the Claude object-root defect here.
- **`cursor.binary` unsupported; `argvPrefix` is the wrapper contract** (`docs/configuration.md:335,340`) — matches how `selector_candidates` (`harness_discovery.py:504-509`) special-cases Cursor, and is what makes Trey's `estate-cursor` launcher work.

---

## Could not verify

- **Whether `--resume <chatId>` in `-p` mode delivers a new positional/stdin prompt to the resumed session.** Argv parses; semantics need two paid calls. See [L3].
- **Whether `--mode plan` actually blocks writes in headless mode.** Cursor documents it as read-only; I did not attempt a write under it (would need a paid call with write intent).
- **The exact exit code for auth failure, rate limit, or cancellation.** Cursor publishes no exit-code table anywhere; I confirmed only `0` (success) and `1` (invalid model). Delegate does not appear to branch on specific Cursor exit codes, so this is informational.
- **The CLI changelog does not publish version numbers** — https://cursor.com/docs/cli/changelog uses dated headings only (newest: "August 26, 2026 release"), so "latest version" comes from the installer script and is corroborated by `cursor-agent about --format json` reporting `up_to_date`.
- **Live-call disclosure:** three small `--mode ask` calls on `composer-2.5` were made directly against `cursor-agent` (not through `delegate`) to obtain the `init` model field, the tool-call payload, and the stdin-transport proof. No `delegate` invocation touched a live model; all delegate exercises were `--json dry-run` or in-process module calls.

## Sources

- https://cursor.com/install — installer script; pins `2026.09.02-c22c1a3`; line 129 creates both `agent` (primary) and `cursor-agent` (legacy) symlinks to `~/.local/share/cursor-agent/versions/<v>/cursor-agent`.
- https://cursor.com/docs/cli/reference/parameters — flag reference: `-p/--print` ("Has access to all tools, including write and shell"), `--output-format`, `--model`, `-f/--force`, `--mode plan|ask`, `--resume [chatId]`, `--continue`, `--workspace`, `--api-key`, `--trust`, `--approve-mcps`, `--stream-partial-output`. No `--background`.
- https://cursor.com/docs/cli/reference/output-format — stream-json event schema (`system`/`user`/`assistant`/`tool_call`/`result`, `subtype` values), the terminal `result` shape, the `json` single-object shape, the "no JSON on failure / non-zero exit / stderr" rule, and the "print mode is inferred… piped stdin" clause behind [S1]. Also the (now incorrect) claim that thinking events are suppressed.
- https://cursor.com/docs/cli/changelog — "Headless hang fixed. `-p` runs no longer block when spawned with an open stdin pipe"; dated headings only, no version numbers.
- https://cursor.com/docs/cli/acp — "Points to the `agent` binary. The default install path is `~/.local/bin/agent`."
- https://cursor.com/docs/cli/installation — `curl https://cursor.com/install -fsS | bash`; PATH guidance for `~/.local/bin`.
- https://cursor.com/docs/cli/headless — headless usage; `if [ $? -eq 0 ]` as the only exit-code guidance.
- Local: `cursor-agent --version` → `2026.09.02-c22c1a3`; `cursor-agent --help`; `cursor-agent models` (215 lines, 211 ids); `cursor-agent about --format json`; three live `-p --mode ask` stream-json runs; `python3 bin/delegate.py --json dry-run cursor work`; in-process `StreamAccumulator` and `harness_discovery.resolve_harness_selector` replays.
- 404 (checked, do not exist): `https://cursor.com/docs/models.md`, `https://cursor.com/docs/cli/reference/models.md` — Cursor publishes no CLI model-id list; the binary is the only authority.
