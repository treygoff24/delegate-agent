# Grok Build CLI Full Parity Implementation Plan

**Goal:** Add first-class `delegate grok {safe,work}` support with the same run tracking, snapshots, run-output, worktree isolation, prompt redaction, reasoning metadata, help, docs, and validation guarantees as the existing Delegate harnesses.

**Architecture:** Treat Grok Build CLI as a modeless harness, like Claude/Kimi/Codex: Delegate owns workspace resolution, isolation, prompt file materialization, run registry, logs, snapshots, and completion-report extraction. Grok owns authentication, model selection, permissions, sandboxing, and agent execution. Full snapshot parity requires tracked Grok runs to use `--output-format streaming-json` only after Delegate has a tested Grok stream parser.

**Tech Stack:** Python stdlib-only Delegate CLI; xAI Grok Build CLI binary `grok`; unittest fixtures for argv, parser, stream parsing, snapshots, run-output, and worktree isolation.

**Status:** native plan review plus Delegate-Claude review incorporated; ready for implementation.

**Review note:** This file lives under gitignored `docs/plans/`; Delegate `safe` reviews will not see it through the isolated worktree unless the plan text is pasted inline, moved to a tracked path, or explicitly staged/tracked for review.

---

## Confirmed inputs and non-negotiable decisions

- The intended harness is **xAI Grok Build CLI** (`grok`), not Groq API/SDK.
- Local help verified binary path: `/Users/treygoff/.grok/bin/grok`.
- Local `grok --help` verified:
  - `--cwd <CWD>`
  - `--prompt-file <PATH>`
  - `-p, --single <PROMPT>`
  - `-m, --model <MODEL>`
  - `--reasoning-effort <EFFORT>`
  - `--effort <LEVEL>`
  - `--output-format plain|json|streaming-json`
  - `--permission-mode default|acceptEdits|auto|dontAsk|bypassPermissions|plan`
  - `--always-approve`
  - `--allow`, `--deny`, `--tools`, `--disallowed-tools`
  - `--sandbox <PROFILE>`
  - `--disable-web-search`
  - `--json-schema <SCHEMA>`
  - `--no-subagents`
- xAI docs mention `--no-auto-update`, but the installed local help did **not** show it. Do **not** emit `--no-auto-update` by default unless a live compatibility probe proves the installed binary accepts it.
- Use `--prompt-file`, not `-p`, so Delegate does not put prompts in process argv, dry-run output, or manifests.
- Do not use Grok `plan` mode for Delegate `safe`. Safe review must still inspect files. Use an isolated copy plus Grok read-only controls instead.
- For full Delegate parity, tracked Grok runs should use `streaming-json` after parser fixtures exist. `json` is simpler but gives degraded live snapshots and weaker completion-report recovery.
- For `--pass-through`, use `plain` output; raw event JSON is not a human pass-through experience.
- Full parity means all cross-harness Delegate features plus any Grok-native feature Delegate already has an analogous public surface for. Because Delegate already has `--output-schema` for Codex and Grok exposes `--json-schema`, the structured-output bridge is required unless live probing proves Grok cannot combine schema constraints with Delegate's snapshot/recovery contract.
- `delegate models` for Grok is **config-only** in v1. Do not shell out to `grok models`; Delegate discovery commands must stay fast, deterministic, and auth-independent.

## Full parity acceptance criteria

The implementation is not complete until all of these work:

1. `delegate grok safe ...` and `delegate grok work ...`.
2. `delegate --json dry-run grok safe ...` emits a prompt-redacted argv with `promptTransport: "file"`.
3. `delegate --json run --input-json task.grok.json` accepts `"engine": "grok"`.
4. `delegate snapshot <alias>` works during and after Grok runs.
5. Snapshot `current`, `assistantText`, and `completionReport` are meaningful for tracked Grok runs; `recentEvents` are meaningful where the Grok stream exposes stable event types, otherwise the documented limitation is tested.
6. `delegate run-output <alias> --completion-report` works from the written report and explicit completion event; assistant-text fallback recovery is enabled only if Task 0 proves Grok chunks are substantive rather than preamble.
7. `delegate run-output <alias> --stdout/--stderr/--raw` works generically.
8. `delegate --isolation worktree grok work ...` rewrites Grok's `--cwd` to the persistent worktree.
9. `--forbid-commit` works with Grok worktree runs.
10. Safe runs reject `--isolation none` unless a future Grok safe runtime mode is proven strong enough to protect the source checkout without Delegate isolation.
11. `delegate runs --harness grok`, `delegate snapshot --latest grok`, and `delegate worktree list --harness grok` work.
12. `delegate models`, `delegate capabilities`, `delegate describe`, and focused `delegate help grok` expose Grok accurately.
13. Missing-binary errors point to `grok.binary` and search `~/.grok/bin`.
14. Docs match actual behavior; no Claude-shaped `plan` mode language.
15. If live probing shows Grok `--json-schema` is compatible with tracked runs, `delegate grok ... --output-schema FILE` works; otherwise the plan must explicitly document and test why `engineCapabilities.grok.outputSchema` remains false.

---

## Task 0: Live compatibility fixture capture

**Parallel:** no
**Blocked by:** none
**Owned files:** `tests/fixtures/grok_streaming_json_smoke.jsonl`, `tests/fixtures/grok_final_json_smoke.json`, `tests/fixtures/grok_help_excerpt.txt`, `tests/fixtures/grok_version.txt`
**Invariants:** Do not commit secrets, full prompts with private data, account IDs, or large transcripts. Fixtures must be minimal and redacted.
**Out of scope:** Do not implement Delegate code in this task.

**Files:**
- Create: `tests/fixtures/grok_streaming_json_smoke.jsonl`
- Create: `tests/fixtures/grok_final_json_smoke.json`
- Create: `tests/fixtures/grok_help_excerpt.txt`
- Create: `tests/fixtures/grok_version.txt`

**Step 1: Reconfirm installed help**

Run:

```bash
grok --help
grok --version
grok agent --help
grok agent headless --help
grok agent stdio --help
```

Expected:

- Top-level `grok` has `--cwd`, `--prompt-file`, `--output-format`, `--permission-mode`, `--sandbox`, `--reasoning-effort`.
- `grok agent ...` remains lower-level and does not replace top-level `--prompt-file` for v1.
- If `--no-auto-update` is absent, keep it out of argv builders.

Save a minimal, redacted excerpt proving the flags and version used for these fixtures:

```text
tests/fixtures/grok_help_excerpt.txt
tests/fixtures/grok_version.txt
```

Create the shared fixture prompt before all live probes:

```bash
cat > /tmp/delegate-grok-fixture-prompt.txt <<'EOF'
Reply with exactly: delegate grok fixture ok
EOF
```

**Step 1b: Prove the exact reasoning flag and value enum**

The planned Delegate flag is Grok `--reasoning-effort`, but local help also exposes `--effort <LEVEL>`. Prove both the flag and the accepted value set before implementation; do not copy Claude's enum by assumption.

First prove which flag is the right semantic mapping:

```bash
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-fixture-prompt.txt \
  --output-format json \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search \
  --reasoning-effort max > /tmp/delegate-grok-reasoning-max.json
```

Expected:

- Exit code `0`.
- If this fails but `--effort max` works, switch the implementation plan to emit `--effort`, rename the reasoning transport to `grok-effort-flag`, and update help/docs/tests before coding.

Then capture the authoritative valid enum for the chosen flag. Probe each candidate separately and record the exact accepted set in `tests/fixtures/grok_help_excerpt.txt`:

```bash
CHOSEN_EFFORT_FLAG=--reasoning-effort  # or --effort if the probe above proves that is the correct Grok flag
for effort in low medium high xhigh max; do
  grok \
    --cwd /Users/treygoff/Code/delegate-agent \
    --prompt-file /tmp/delegate-grok-fixture-prompt.txt \
    --output-format json \
    --permission-mode dontAsk \
    --sandbox read-only \
    --disable-web-search \
    "$CHOSEN_EFFORT_FLAG" "$effort" >/tmp/delegate-grok-reasoning-"$effort".json \
    && echo "accepted: $effort" \
    || echo "rejected: $effort"
done
```

If the CLI error/help text contains an enum outside this candidate set, probe those values too and capture the exact text instead of inferring. Task 1's `GROK_NATIVE_EFFORTS` must match this proven set verbatim.

Also run one invalid-value probe with the exact chosen flag:

```bash
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-fixture-prompt.txt \
  --output-format json \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search \
  --reasoning-effort definitely-invalid
```

Expected:

- Non-zero exit or clear CLI validation error. If invalid values are silently accepted, Delegate must still preflight-validate against its static effort enum.
- Acceptance line for this task: valid effort enum captured verbatim in `tests/fixtures/grok_help_excerpt.txt`.

**Step 2: Capture final JSON fixture**

Run:

```bash
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-fixture-prompt.txt \
  --output-format json \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search > /tmp/delegate-grok-final.json
```

Expected:

- Exit code `0`.
- JSON contains the assistant's final text or a stable field that contains it.
- No secrets in output.

Redact and save only the minimal representative object to:

```text
tests/fixtures/grok_final_json_smoke.json
```

**Step 3: Capture streaming JSON fixture**

Run:

```bash
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-fixture-prompt.txt \
  --output-format streaming-json \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search > /tmp/delegate-grok-stream.jsonl
```

Expected:

- Exit code `0`.
- Newline-delimited JSON.
- At least one event contains assistant text or final result text.
- Tool/read events, if present, have stable-enough fields to normalize into Delegate events.

Redact and save a minimal representative stream to:

```text
tests/fixtures/grok_streaming_json_smoke.jsonl
```

**Step 4: Confirm safe inspect mode**

Run a prompt requiring read-only inspection:

```bash
cat > /tmp/delegate-grok-readonly-prompt.txt <<'EOF'
Read README.md and report the first heading. Do not edit files.
EOF
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-readonly-prompt.txt \
  --output-format json \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search
```

Expected:

- Grok can read workspace files without prompting.
- No files change.

If this fails because `dontAsk` denies reads, test the narrowest non-prompting permission shape using `--allow` rules. If no non-prompting read-only shape works, stop and do not implement `safe` as `plan`; report that Grok cannot currently satisfy Delegate safe-review parity.

**Step 5: Probe structured output compatibility**

Create a tiny schema:

```bash
cat > /tmp/delegate-grok-schema.json <<'EOF'
{"type":"object","properties":{"heading":{"type":"string"}},"required":["heading"],"additionalProperties":false}
EOF
```

Probe schema plus tracked stream output:

```bash
grok \
  --cwd /Users/treygoff/Code/delegate-agent \
  --prompt-file /tmp/delegate-grok-readonly-prompt.txt \
  --output-format streaming-json \
  --json-schema "$(cat /tmp/delegate-grok-schema.json)" \
  --permission-mode dontAsk \
  --sandbox read-only \
  --disable-web-search > /tmp/delegate-grok-schema-stream.jsonl
```

Expected:

- If this works and still emits parseable events/final JSON, Task 8 must implement `delegate grok --output-schema FILE`.
- If Grok forces final `json` or rejects the combination, Task 8 must choose the least-bad supported path and document the snapshot tradeoff in tests/docs.

**Verification plan:**

- Primary command: the four commands above.
- Secondary check: `git status --short` remains unchanged except for fixture files intentionally added.

---

## Task 1: Engine vocabulary, config, and parser

**Parallel:** no
**Blocked by:** Task 0
**Owned files:** `src/delegate_agent/constants.py`, `src/delegate_agent/config.py`, `src/delegate_agent/cli_parser.py`, `src/delegate_agent/request_build.py`, `src/delegate_agent/reasoning.py`, `config.example.json`, `tests/test_delegate_parser.py`, `tests/test_delegate_validation.py`, `tests/test_config_commands.py`, `tests/test_reasoning_capabilities.py`
**Invariants:** Existing engine names and JSON input semantics must remain backward-compatible. Do not add new runtime dependencies.
**Out of scope:** Stream parsing and docs.

**Files:**
- Modify: `src/delegate_agent/constants.py`
- Modify: `src/delegate_agent/config.py`
- Modify: `src/delegate_agent/cli_parser.py`
- Modify: `src/delegate_agent/request_build.py`
- Modify: `src/delegate_agent/reasoning.py`
- Modify: `config.example.json`
- Test: `tests/test_delegate_parser.py`
- Test: `tests/test_delegate_validation.py`
- Test: `tests/test_config_commands.py`
- Test: `tests/test_reasoning_capabilities.py`

**Step 1: Add failing parser/config tests**

Add tests asserting:

- `parse_cli(["grok", "safe", "review"])` returns launch engine `grok`, mode `safe`.
- `parse_cli(["grok", "safe", "--prompt-file", "task.md"])` returns launch engine `grok`, mode `safe`, prompt file `task.md`.
- `parse_cli(["dry-run", "grok", "work", "fix"])` returns dry-run launch engine `grok`.
- `parse_cli(["dry-run", "grok", "safe", "--prompt-file", "task.md"])` preserves prompt-file parsing.
- `request_from_input_json` accepts `"engine": "grok"` with `"model": "grok-code-fast"` as a direct model override.
- `delegate_config.validate_config(delegate_config.embedded_default_config())` accepts a `grok` section.
- `grok.defaultReasoningEffort` validates against static effort strings.
- `grok.workPermissionMode: "bypassPermissions"` is rejected; bypass must be policy-scoped.

Run:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_validation tests.test_config_commands tests.test_reasoning_capabilities
```

Expected: FAIL with unknown engine/config errors.

**Step 2: Add engine vocabulary**

In `src/delegate_agent/constants.py`, append `grok` to `KNOWN_ENGINES`:

```python
KNOWN_ENGINES = ("cursor", "droid", "codex", "kimi", "claude", "grok")
```

`ENGINES_PROSE` derives from this tuple.

**Step 3: Add config defaults**

In `src/delegate_agent/config.py`, add:

```python
"grok": {
    "binary": "grok",
    "defaultModel": None,
    "defaultReasoningEffort": None,
    "workPermissionMode": "auto",
    "safePermissionMode": "dontAsk",
    "safeSandbox": "read-only",
    "workSandbox": None,
    "disableWebSearch": True,
    "noSubagents": False,
},
```

Do not add `noAutoUpdate` unless Task 0 proves the installed binary accepts `--no-auto-update`.

**Step 4: Add minimal Grok reasoning constants and validator**

In `src/delegate_agent/reasoning.py`, add the static Grok effort facts early so config validation and request building do not depend on a later task:

```python
TRANSPORT_GROK_REASONING_EFFORT_FLAG = "grok-reasoning-effort-flag"
# Populate with the exact strings proven in Task 0, not copied from Claude.
GROK_NATIVE_EFFORTS = ("<task-0-effort>",)

def resolve_grok_native_effort(
    requested_effort: str | None,
    *,
    alias: str | None = None,
    model: str | None = None,
) -> str | None:
    ...
```

If Task 0 proves Grok requires `--effort` instead of `--reasoning-effort`, use:

```python
TRANSPORT_GROK_REASONING_EFFORT_FLAG = "grok-effort-flag"
```

and update all later task text/tests before implementation.

`resolve_grok_native_effort` must validate against the standalone `GROK_NATIVE_EFFORTS` tuple. Do **not** reference `REASONING_PROFILES["grok"]` here; Task 5 adds that profile row later.

Add tests for:

- valid values from the exact Task 0 enum.
- invalid value fails with `unsupported_reasoning_effort`.
- invalid string shape fails with `invalid_reasoning_effort`.

**Step 5: Validate Grok config**

Add:

```python
GROK_PERMISSION_MODES = ("acceptEdits", "auto", "default", "dontAsk", "plan")
GROK_BYPASS_PERMISSION_MODE = "bypassPermissions"
GROK_SAFE_SANDBOX_VALUES = ("read-only", "strict")
GROK_WORK_SANDBOX_VALUES = ("workspace", "devbox", "read-only", "strict")
```

Add `_validate_grok_section`:

- `grok.binary`: required non-empty string.
- `grok.defaultModel`: optional string.
- `grok.defaultReasoningEffort`: optional static Grok effort string via `reasoning.resolve_grok_native_effort`; invalid configured defaults hard-reject at config-validation time, matching Claude's config-source convention.
- `grok.safePermissionMode`: allowed `dontAsk`, `default`, or `auto`; reject `plan` as the default safe strategy.
- `grok.workPermissionMode`: allowed Grok permission modes except `bypassPermissions`.
- `grok.safeSandbox`: `read-only` or `strict`.
- `grok.workSandbox`: null or one of `workspace`, `devbox`, `read-only`, `strict`.
- `grok.disableWebSearch`: bool.
- `grok.noSubagents`: bool.

Call `_validate_grok_section(config.get("grok"))` from `validate_config`.

**Step 6: Parser dispatch**

In `src/delegate_agent/cli_parser.py`:

- Add `grok` to `AUTH_PROFILE_SUBCOMMANDS`.
- Add `grok` to the modeless-engine branch:

```python
if subcommand in ("cursor", "codex", "kimi", "claude", "grok"):
```

- Add `grok` to `parse_dry_run` modeless branch.
- Update error strings that list dry-run engines.

**Step 7: JSON input model semantics**

In `src/delegate_agent/request_build.py`, update JSON input validation:

```python
elif engine in ("codex", "kimi", "claude", "grok"):
```

`model` is a direct Grok model id, not a Delegate alias.

**Step 8: Update starter config**

In `config.example.json`, add the same `grok` block with placeholder-free defaults.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_validation tests.test_config_commands tests.test_reasoning_capabilities
```

- Secondary command:

```bash
python3 -m compileall -q src tests bin
```

Expected: PASS.

---

## Task 2: Prompt file transport and argv builder

**Parallel:** yes
**Blocked by:** Task 1
**Owned files:** `src/delegate_agent/prompt_transport.py`, `src/delegate_agent/argv_builders.py`, `src/delegate_agent/request_build.py`, `src/delegate_agent/argv_utils.py`, `src/delegate_agent/cli.py`, `tests/test_engine_argv.py`, `tests/test_execution_argv_and_prompt.py`, `tests/test_execution_dry_run.py`, `tests/test_snapshot_redaction.py`
**Invariants:** Prompt text must never appear in public argv, dry-run JSON, or manifest argv. Worktree runs must rewrite Grok's `--cwd`.
**Out of scope:** Stream parsing and docs.

**Files:**
- Modify: `src/delegate_agent/prompt_transport.py`
- Modify: `src/delegate_agent/argv_builders.py`
- Modify: `src/delegate_agent/request_build.py`
- Modify: `src/delegate_agent/argv_utils.py`
- Modify: `src/delegate_agent/cli.py`
- Test: `tests/test_engine_argv.py`
- Test: `tests/test_execution_argv_and_prompt.py`
- Test: `tests/test_execution_dry_run.py`
- Test: `tests/test_snapshot_redaction.py`

**Step 1: Add failing argv/request tests**

Add tests asserting:

- Safe argv includes `grok`, `--cwd <workspace>`, `--prompt-file <delegate placeholder>`, `--output-format streaming-json`, `--permission-mode dontAsk`, `--sandbox read-only`.
- Safe argv includes `--disable-web-search` when `grok.disableWebSearch` is true.
- Work argv includes `--permission-mode auto`.
- A config with global `policy.profile: "external-sandbox"` does **not** emit Grok `bypassPermissions` or `--always-approve`.
- A config with `policy.harness.grok.work.bypassApprovalsAndSandbox: true` does emit Grok `bypassPermissions` and `--always-approve`.
- Pass-through argv uses `--output-format plain`.
- Public dry-run argv displays `<prompt file>`, not the actual prompt or temp path.
- Request has `prompt_transport == "file"` and `prompt_file_text` equal to the exact effective prompt.
- For safe mode, `prompt_file_text` contains `Delegate Grok safe mode`.
- Worktree dry-run rewrites Grok `--cwd` to planned worktree path.
- Snapshot/dry-run redaction tests prove the raw prompt and temp prompt-file path do not appear in public dry-run JSON, manifest public argv, snapshot JSON, progress fields, or run-output diagnostics.

Run:

```bash
python3 -m unittest tests.test_engine_argv tests.test_execution_argv_and_prompt tests.test_execution_dry_run tests.test_snapshot_redaction
```

Expected: FAIL.

**Step 2: Generalize prompt-file display constants**

In `src/delegate_agent/prompt_transport.py`, add generic names while preserving Droid aliases:

```python
PROMPT_FILE_ARG_PLACEHOLDER = "<delegate-prompt-file>"
PROMPT_FILE_DISPLAY = "<prompt file>"
DROID_PROMPT_FILE_ARG_PLACEHOLDER = PROMPT_FILE_ARG_PLACEHOLDER
DROID_PROMPT_FILE_DISPLAY = PROMPT_FILE_DISPLAY
```

In `src/delegate_agent/cli.py`, replace the unconditional tracked-run references to `DROID_PROMPT_FILE_ARG_PLACEHOLDER` with `PROMPT_FILE_ARG_PLACEHOLDER`; keep the Droid-named constant only as a backward-compatible alias. This prevents future de-aliasing from breaking Grok prompt-file materialization.

**Step 3: Add Grok safe prefix**

In `src/delegate_agent/argv_builders.py`, add:

```python
"grok": "Delegate Grok safe mode",
```

to `_SAFE_REVIEW_LABEL_BY_ENGINE`.

Update `effective_prompt` in `src/delegate_agent/request_build.py` so Grok safe gets provider-specific safe framing in the same way as Codex/Droid/Claude. The load-bearing edit is adding `grok` to the existing file/stdin prompt-prefix whitelist:

```python
if engine in {"codex", "droid", "claude", "grok"}:
    ...
```

This is load-bearing for prompt-file transport: `build_grok_argv()` cannot be the only prefix owner because it does not write the temp prompt file and the prompt is not passed in argv. `_grok_request_parts` must set `prompt_file_text = build.prompt`, i.e. the exact effective prompt after safe-prefix insertion and dirty-tree note handling that Grok will read from disk.

Add a fake harness test that opens the actual `--prompt-file` path it receives and asserts it contains `Delegate Grok safe mode` for safe runs.

**Step 4: Build Grok argv**

Add `build_grok_argv` to `src/delegate_agent/argv_builders.py`:

```python
def build_grok_argv(
    grok: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    prompt: str,
    policy: JsonObject,
    *,
    stream_capture: bool = True,
    reasoning_effort: str | None = None,
    allow_bypass_permissions: bool = False,
    prompt_transport: str = PROMPT_TRANSPORT_FILE,
) -> list[str]:
    ...
```

Required behavior:

- Always start with `[grok["binary"], "--cwd", workspace]`.
- Tracked mode: `--output-format streaming-json`.
- Pass-through mode: `--output-format plain`.
- Safe:
  - do not rely on argv-builder prompt mutation for safety; safe framing is owned by `request_build.effective_prompt` and delivered through `prompt_file_text`.
  - emit `--permission-mode <grok.safePermissionMode>`; default `dontAsk`.
  - emit `--sandbox <grok.safeSandbox>`; default `read-only`.
- Work:
  - emit `--permission-mode <grok.workPermissionMode>`; default `auto`.
  - if `allow_bypass_permissions` is true, emit `--permission-mode bypassPermissions` and `--always-approve`.
  - if `grok.workSandbox` is a string, emit `--sandbox <value>`.
- If the effective policy's `webSearch` is not true and `grok.disableWebSearch` is true, emit `--disable-web-search`.
- If `grok.noSubagents` is true, emit `--no-subagents`.
- If `model` is set, emit `--model <model>`.
- If `reasoning_effort` is set, emit the Task 0-proven Grok effort flag (`--reasoning-effort` or `--effort`) with the resolved level.
- If Task 0 proves `--no-auto-update`, optionally emit it behind a config field added in Task 1; otherwise do not emit.
- Prompt transport:
  - For `PROMPT_TRANSPORT_FILE`, append `--prompt-file <delegate-prompt-file>`.
  - Reject argv/stdin transport for Grok v1.

Do not add arbitrary `additionalArgs`.

**Step 5: Add request parts**

In `src/delegate_agent/request_build.py`:

- Import `build_grok_argv`.
- Add `_grok_harness_bypass_enabled` mirroring Claude's harness-scoped policy helper.
- `_grok_harness_bypass_enabled` must read only `policy.harness.grok.work.bypassApprovalsAndSandbox`, not the effective merged work policy. This prevents global `policy.profile: "external-sandbox"` from silently broadening Grok permissions.
- Compute `policy = delegate_config.effective_policy(build.config, engine="grok", mode=build.mode)` and pass that effective policy into `build_grok_argv`; otherwise `policy.work.webSearch` and `policy.harness.grok.*.webSearch` will not drive `--disable-web-search`.
- Confirm `build_grok_argv` does not additionally require `policy.bypassApprovalsAndSandbox` for bypass mode; the single harness-scoped `_grok_harness_bypass_enabled` gate is intentional.
- Add `_grok_request_parts`.
- Resolve model:
  - direct `model`/`model_alias` if present.
  - else `grok.defaultModel`.
  - omit `--model` if unresolved.
- Resolve effort through `reasoning.resolve_grok_native_effort` from Task 1.
- Set:

```python
prompt_transport=PROMPT_TRANSPORT_FILE
prompt_file_text=build.prompt  # exact effective prompt after safe-prefix insertion and dirty-tree note handling
display_argv=[PROMPT_FILE_DISPLAY if item == PROMPT_FILE_ARG_PLACEHOLDER else item for item in argv]
```

- Add `"grok": _grok_request_parts` to `ENGINE_REQUEST_PARTS_BUILDERS`.

**Step 6: Add workspace argv rewrite**

In `src/delegate_agent/argv_utils.py`, add:

```python
"grok": "--cwd",
```

This is required for persistent worktree isolation and dry-run planned argv.

**Step 7: Missing binary diagnostics**

In `src/delegate_agent/cli.py`:

- Add `~/.grok/bin` to `MISSING_BINARY_PROBE_DIRS`.
- Update `_binary_config_key`:

```python
if engine in {"codex", "droid", "kimi", "claude", "grok"}:
    return f"{engine}.binary"
```

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_engine_argv tests.test_execution_argv_and_prompt tests.test_execution_dry_run tests.test_snapshot_redaction
```

- Secondary command:

```bash
python3 bin/delegate.py --json dry-run grok safe "Review this repo. Do not edit files."
```

Expected:

- PASS.
- Dry-run JSON has `promptTransport: "file"` and no prompt text in `argv`.

---

## Task 3: Safe isolation, worktree lifecycle, and commit policy parity

**Parallel:** yes
**Blocked by:** Task 2
**Owned files:** `src/delegate_agent/config.py`, `src/delegate_agent/worktree_execution.py`, `tests/test_safe_workspace_isolation.py`, `tests/test_execution_worktree_run.py`, `tests/test_execution_worktree_preflight.py`, `tests/test_execution_worktree_failure_cleanup.py`
**Invariants:** Grok safe must not mutate the source checkout. Persistent worktree runs must be inspectable/removable through Delegate commands only.
**Out of scope:** Stream parser and docs.

**Files:**
- Modify: `src/delegate_agent/config.py`
- Modify only if needed: `src/delegate_agent/worktree_execution.py`
- Test: `tests/test_safe_workspace_isolation.py`
- Test: `tests/test_execution_worktree_run.py`
- Test: `tests/test_execution_worktree_preflight.py`
- Test: `tests/test_execution_worktree_failure_cleanup.py`

**Step 1: Add failing isolation tests**

Add tests asserting:

- `delegate --isolation none grok safe ...` fails.
- `delegate --json dry-run grok safe ...` reports `isolatedWorkspace: true`.
- Safe dry-run planned isolation mirrors other safe-isolation-required harnesses.
- `delegate --isolation worktree grok work --forbid-commit ...` creates planned branch/path metadata in dry-run.
- Persistent worktree execution rewrites `--cwd` to the execution worktree.
- Pre-launch worktree failure snapshots include planned fields for Grok.

Run:

```bash
python3 -m unittest tests.test_safe_workspace_isolation tests.test_execution_worktree_run tests.test_execution_worktree_preflight tests.test_execution_worktree_failure_cleanup
```

Expected: FAIL until Grok is wired.

**Step 2: Require safe isolation**

In `src/delegate_agent/config.py`:

```python
SAFE_ISOLATION_REQUIRED_ENGINES = frozenset({"cursor", "droid", "kimi", "claude", "grok"})
```

**Step 3: Confirm generic worktree execution works**

Because Task 2 adds `argv_utils.WORKSPACE_FLAG_BY_ENGINE["grok"] = "--cwd"`, the generic worktree code should work without Grok-specific branches. Only touch `src/delegate_agent/worktree_execution.py` if a test proves the generic path is insufficient.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_safe_workspace_isolation tests.test_execution_worktree_run tests.test_execution_worktree_preflight tests.test_execution_worktree_failure_cleanup
```

- Secondary command:

```bash
python3 bin/delegate.py --json --isolation worktree dry-run grok work --forbid-commit "Implement a no-op and report status."
```

Expected: PASS and planned argv uses the planned worktree path after `--cwd`.

---

## Task 4: Grok stream parsing, snapshots, and run-output parity

**Parallel:** no
**Blocked by:** Task 0, Task 2
**Owned files:** `src/delegate_agent/harness_events.py`, `src/delegate_agent/run_output_commands.py`, `tests/test_harness_events.py`, `tests/test_runner_capture.py`, `tests/test_snapshot_run_output.py`, `tests/test_snapshot_view.py`, `tests/test_snapshot_redaction.py`, `tests/fixtures/grok_streaming_json_smoke.jsonl`, `tests/fixtures/grok_final_json_smoke.json`
**Invariants:** Do not regress Codex/Cursor/Droid/Kimi/Claude stream parsing. Grok parser branches must not change dispatch for existing `type` values unless tests prove the shared shape is identical. Bounded snapshot limits must still apply.
**Out of scope:** CLI parser/config/docs.

**Files:**
- Modify: `src/delegate_agent/harness_events.py`
- Modify only if needed: `src/delegate_agent/run_output_commands.py`
- Test: `tests/test_harness_events.py`
- Test: `tests/test_runner_capture.py`
- Test: `tests/test_snapshot_run_output.py`
- Test: `tests/test_snapshot_view.py`
- Test: `tests/test_snapshot_redaction.py`
- Fixture: `tests/fixtures/grok_streaming_json_smoke.jsonl`
- Fixture: `tests/fixtures/grok_final_json_smoke.json`

**Step 1: Add failing stream parser tests**

Using the Task 0 fixtures, add tests asserting:

- Streaming Grok assistant deltas/chunks append to `StreamAccumulator.assistant_text`.
- Grok final result/completion event populates `StreamAccumulator.completion_text`.
- Tool activity normalizes to `NormalizedEvent(kind="tool.started"|"tool.completed", ...)` when the stream exposes tool events.
- `current` becomes a useful short status, not raw JSON.
- Unknown Grok event objects are ignored or safely summarized; they do not crash parsing.

Run:

```bash
python3 -m unittest tests.test_harness_events tests.test_runner_capture
```

Expected: FAIL until parser support exists.

**Step 2: Implement Grok event ingestion**

In `src/delegate_agent/harness_events.py`, extend `StreamAccumulator._ingest_object` with the smallest set of Grok-specific branches proven by fixtures.

Acceptable extraction strategy:

- Prefer a final/completion/result field for `completion_text`.
- Treat assistant role/content/text fields as recoverable assistant text.
- Normalize tool events only when fields are stable and useful.
- Fall back to bounded text event for unknown objects.

Do not guess large event taxonomies not present in fixtures.

Because `StreamAccumulator._ingest_object` is shared by all harnesses and dispatches primarily on object shape/type, do not add broad branches for generic `assistant`, `message`, or `result` objects unless the existing Cursor/Claude/Droid/Kimi/Codex fixture suite proves no regression. Prefer Grok-unique `type` values or Grok-specific field combinations from the Task 0 fixture.

**Step 3: Add Grok to assistant fallback recovery**

If fixture tests prove Grok assistant chunks are substantive and not just preamble, update:

```python
ASSISTANT_RECOVERY_HARNESSES = frozenset({"cursor", "droid", "kimi", "claude", "grok"})
```

If Grok streams contain preamble that is unsafe as a fallback, do **not** add it; instead require explicit `completion_text` for report recovery and document the limitation. Full parity requires a recoverable explicit completion path, but not unsafe assistant-fallback recovery.

If Task 0 proves Grok streams do not expose stable tool/recent-event data, document the degraded contract: `assistantText` and `completionReport` must work, while `recentEvents` may be sparse or limited to bounded text summaries. Add tests for the documented behavior rather than asserting non-existent tool events.

**Step 4: Snapshot/run-output integration tests**

Add tests with a fake `grok` executable that writes the fixture stream to stdout and exits `0`.

Assert:

- `delegate --json grok safe ...` records a run.
- `snapshot` includes `harness: "grok"`, `status`, `current`, `assistantText`, and `completionReport` metadata, with `recentEvents` following the Task 0/Task 4 stream contract.
- `run-output --completion-report` returns the expected final text.
- `run-output --stdout --tail 20` returns bounded raw stream output.
- Snapshot redaction does not expose the original prompt text, prompt-file temp path, or prompt-file contents.

Run:

```bash
python3 -m unittest tests.test_snapshot_run_output tests.test_snapshot_view tests.test_runner_capture tests.test_snapshot_redaction
```

Expected: PASS.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_harness_events tests.test_runner_capture tests.test_snapshot_run_output tests.test_snapshot_view tests.test_snapshot_redaction
```

- Secondary live smoke after implementation:

```bash
python3 bin/delegate.py --json grok safe --prompt-file /tmp/delegate-grok-readonly-prompt.txt
python3 bin/delegate.py --json snapshot --latest grok
python3 bin/delegate.py run-output grok --completion-report
```

Expected:

- PASS.
- Snapshot and run-output are meaningful without reading `.delegate` files by hand.

---

## Task 5: Reasoning capabilities and config-only model summary

**Parallel:** yes
**Blocked by:** Task 1
**Owned files:** `src/delegate_agent/reasoning.py`, `src/delegate_agent/capability_commands.py`, `src/delegate_agent/describe_payload.py`, `tests/test_reasoning_capabilities.py`, `tests/test_capability_commands.py`
**Invariants:** Existing reasoning JSON for Codex/Droid/Cursor/Claude/Kimi must not change except for adding Grok. Keep key order stable where tests pin it.
**Out of scope:** Argv builder except for consuming the resolved effort. Live `grok models` inventory is out of scope for v1; `delegate models` is config-only.

**Files:**
- Modify: `src/delegate_agent/reasoning.py`
- Modify only if needed: `src/delegate_agent/capability_commands.py`
- Modify: `src/delegate_agent/describe_payload.py`
- Test: `tests/test_reasoning_capabilities.py`
- Test: `tests/test_capability_commands.py`

**Step 1: Add failing reasoning tests**

Add tests asserting:

- `delegate capabilities` includes the exact Grok static efforts proven in Task 0.
- Grok reasoning transport is the exact Task 1 constant for the Task 0-proven flag (`grok-reasoning-effort-flag` or `grok-effort-flag`).
- Invalid explicit Grok effort fails before launch.
- `grok.defaultReasoningEffort` is accepted when valid and hard-rejected during config validation when invalid, matching Claude's config-source convention.
- `models --summary` includes Grok default model and reasoning summary from config/static facts only.
- No test or code path shells out to `grok models`.

Run:

```bash
python3 -m unittest tests.test_reasoning_capabilities tests.test_capability_commands
```

Expected: FAIL.

**Step 2: Extend reasoning profiles for rendered payloads**

Task 1 already added `TRANSPORT_GROK_REASONING_EFFORT_FLAG`, `GROK_NATIVE_EFFORTS`, and `resolve_grok_native_effort`. This task adds the Grok row to `REASONING_PROFILES` and updates renderers so `capabilities` and `models --summary` expose those facts.

**Step 3: Summary/capabilities output**

Update summary/capabilities renderers to include Grok as a static-enum harness.

Expected Grok capability payload shape:

```json
{
  "supported": ["<exact Task 0 effort enum>"],
  "source": "native-static",
  "transport": "<Task 1 Grok reasoning transport>"
}
```

**Step 4: Confirm request metadata**

In `src/delegate_agent/request_build.py`, `_grok_request_parts` should call `resolve_grok_native_effort` and set:

- `requestedReasoningEffort`
- `resolvedReasoningEffort`
- `reasoningEffortSource`
- `reasoningTransport`

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_reasoning_capabilities tests.test_capability_commands
```

- Secondary command:

```bash
python3 bin/delegate.py --json capabilities
python3 bin/delegate.py --json models --summary
```

Expected: PASS and Grok appears in both outputs.

---

## Task 6: Describe, models, help, and command surface

**Parallel:** yes
**Blocked by:** Task 1, Task 2, Task 5
**Owned files:** `src/delegate_agent/describe_payload.py`, `src/delegate_agent/command_help.py`, `tests/test_command_help.py`, `tests/test_delegate_help_cli.py`, `tests/test_inspection_commands.py`, `tests/test_capability_commands.py`
**Invariants:** Machine-readable describe/help output remains valid and complete. Existing command specs do not lose options.
**Out of scope:** Markdown docs.

**Files:**
- Modify: `src/delegate_agent/describe_payload.py`
- Modify: `src/delegate_agent/command_help.py`
- Test: `tests/test_command_help.py`
- Test: `tests/test_delegate_help_cli.py`
- Test: `tests/test_inspection_commands.py`
- Test: `tests/test_capability_commands.py`

**Step 1: Add failing describe/help tests**

Assert:

- `delegate --json describe --summary` includes `grok` in `engines`.
- `describe.promptTransports.grok == "file"`.
- `describe.isolation.safeNoneAllowed.grok is False`.
- `describe.modeMapping.grok.safe` and `.work` include expected argv shapes.
- `describe.modeMapping.grok.safeNotes` states that the isolated workspace is the effective write boundary and Grok sandbox/permission flags are advisory defense-in-depth.
- `describe.policyFieldSupport.grok.webSearch` is true if `--disable-web-search` mapping is implemented.
- `describe.engineCapabilities.grok.outputSchema` is false unless Task 8 implements `--json-schema` as a separate Grok feature.
- `delegate --json help grok` returns a command spec.
- Overview usage includes `grok`.
- `dry-run` help includes Grok.

Run:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli tests.test_inspection_commands tests.test_capability_commands
```

Expected: FAIL.

**Step 2: Update command help**

In `src/delegate_agent/command_help.py`:

- Add `grok` `CommandSpec`.
- Update `dry-run` summary/usage/arguments/see_also.
- Update overview usage literals.

Grok help notes must say:

- Prompt uses Delegate temp file via Grok `--prompt-file`.
- Tracked mode uses `streaming-json` for snapshots/run-output.
- Pass-through uses `plain`.
- Safe mode uses Delegate isolated copy plus Grok read-only sandbox/permission controls; it does not use Grok `plan` mode.
- `--reasoning-effort` maps to the Task 0-proven Grok effort flag (`--reasoning-effort` or `--effort`).
- Existing Droid examples must not use `grok` as a Droid model alias after this change; update examples like `delegate --json dry-run droid grok safe ...` to a non-colliding placeholder such as `delegate --json dry-run droid reviewer safe ...`, and add one note distinguishing the top-level Grok engine from any Droid-served Grok model alias.

**Step 3: Update describe payload**

In `src/delegate_agent/describe_payload.py`, update all hard-coded harness maps:

- `engines`
- `policyFieldSupport`
- `engineCapabilities`
- `promptTransports`
- `promptSources`
- `promptTransforms`
- `safeNoneAllowed`
- `modeMapping`
- `effectivePolicy` if the payload exposes per-engine policy previews
- `models_payload`
- `models_summary_payload`
- text renderers and agent-help literals, including the hard-coded `engines:` literal, `_emit_models_text`, and `emit_agent_help`.

Do not add a nonexistent `promptInArgv` key; the current describe surface uses prompt transports/sources/transforms instead.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli tests.test_inspection_commands tests.test_capability_commands
```

- Secondary commands:

```bash
python3 bin/delegate.py --json describe --summary
python3 bin/delegate.py --json help grok
python3 bin/delegate.py agent-help
```

Expected: PASS and Grok appears consistently.

---

## Task 7: Run registry filters, snapshots, run-output, and worktree commands

**Parallel:** yes
**Blocked by:** Task 1, Task 3, Task 4
**Owned files:** `src/delegate_agent/inspection_commands.py`, `src/delegate_agent/worktree_commands.py`, `src/delegate_agent/worktree_gc.py`, `src/delegate_agent/worktree_records.py`, `src/delegate_agent/cli_parser.py`, `tests/test_run_registry.py`, `tests/test_inspection_commands.py`, `tests/test_snapshot_run_output.py`, `tests/test_worktree_list_show.py`, `tests/test_worktree_remove.py`, `tests/test_worktree_prune_gc.py`
**Invariants:** Adding Grok to `KNOWN_ENGINES` should make most filters generic; do not add special cases unless tests prove a gap.
**Out of scope:** Harness argv/request building.

**Files:**
- Modify only if tests reveal a generic gap: `src/delegate_agent/inspection_commands.py`
- Modify only if tests reveal a generic gap: `src/delegate_agent/worktree_commands.py`
- Modify only if tests reveal a generic gap: `src/delegate_agent/worktree_gc.py`
- Modify only if tests reveal a generic gap: `src/delegate_agent/worktree_records.py`
- Modify only if tests reveal a generic gap: `src/delegate_agent/cli_parser.py`
- Test: `tests/test_run_registry.py`
- Test: `tests/test_inspection_commands.py`
- Test: `tests/test_snapshot_run_output.py`
- Test: `tests/test_worktree_list_show.py`
- Test: `tests/test_worktree_remove.py`
- Test: `tests/test_worktree_prune_gc.py`

**Step 1: Add filter tests**

Add tests proving:

- `runs --harness grok` accepts Grok.
- `snapshot --latest grok` resolves latest Grok run.
- `worktree list --harness grok` filters persistent Grok worktrees.
- `worktree prune --harness grok` previews/removes only Grok worktrees.

Run:

```bash
python3 -m unittest tests.test_run_registry tests.test_inspection_commands tests.test_snapshot_run_output tests.test_worktree_list_show tests.test_worktree_remove tests.test_worktree_prune_gc
```

Expected: PASS after `KNOWN_ENGINES` and worktree argv rewrite are correct. If it fails, patch the smallest generic site from the owned implementation files above.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_run_registry tests.test_inspection_commands tests.test_snapshot_run_output tests.test_worktree_list_show tests.test_worktree_remove tests.test_worktree_prune_gc
```

- Secondary command:

```bash
python3 bin/delegate.py --json runs --harness grok --limit 5
```

Expected: PASS; no unknown-harness errors.

---

## Task 8: Structured output bridge or explicit unsupported contract

**Parallel:** no
**Blocked by:** Task 6
**Owned files:** `src/delegate_agent/request_build.py`, `src/delegate_agent/argv_builders.py`, `src/delegate_agent/describe_payload.py`, `src/delegate_agent/command_help.py`, `tests/test_engine_argv.py`, `tests/test_execution_dry_run.py`
**Invariants:** Do not break existing Codex `--output-schema` behavior. Do not claim schema parity unless Grok `--json-schema` is tested with the tracked output mode.
**Out of scope:** Arbitrary schema generation or schema inference.

**Files:**
- Modify: `src/delegate_agent/request_build.py`
- Modify: `src/delegate_agent/argv_builders.py`
- Modify: `src/delegate_agent/describe_payload.py`
- Modify: `src/delegate_agent/command_help.py`
- Test: `tests/test_engine_argv.py`
- Test: `tests/test_execution_dry_run.py`

**Step 1: Use Task 0 result to choose the contract**

If Task 0 proves `--json-schema` works with `--output-format streaming-json`, implement schema support for Grok.

If Task 0 proves Grok only supports `--json-schema` with final `json`, decide explicitly:

- either support Grok schema runs with final `json` and document the reduced live-snapshot richness for schema-constrained Grok runs, or
- keep `--output-schema` rejected for Grok with a precise error and `describe.engineCapabilities.grok.outputSchema = false`.

Do not leave this ambiguous.

**Step 2A: Implement if compatible**

- Extend `resolve_output_schema` to allow `engine == "grok"`.
- Preserve Codex behavior: Codex receives an absolute schema file path.
- For Grok, read the schema file text and emit it as Grok `--json-schema <SCHEMA_JSON>` if live probing confirms Grok expects a JSON string rather than a path.
- Decide the exact path-to-string owner: `resolve_output_schema` may still validate/normalize the schema path, but Grok request/argv building must read the file contents before emitting `--json-schema`.
- Add request/model plumbing while documenting that the schema contents are visible in Grok public argv/dry-run/manifest if the child CLI requires inline schema JSON. This is a size/visibility tradeoff, not a prompt secret; keep prompt redaction separate.
- Update `describe.engineCapabilities.grok.outputSchema = true`.
- Update `delegate help grok` and docs.

**Step 2B: Explicit unsupported contract if incompatible**

- Keep `resolve_output_schema` rejecting Grok.
- Add a Grok-specific error message that names the live incompatibility found in Task 0.
- Keep `describe.engineCapabilities.grok.outputSchema = false`.
- Add tests proving Grok rejects `--output-schema` cleanly.

**Verification plan:**

```bash
python3 -m unittest tests.test_engine_argv tests.test_execution_dry_run
```

---

## Task 9: Documentation and examples

**Parallel:** yes
**Blocked by:** Task 1 through Task 7
**Owned files:** `README.md`, `docs/cli-reference.md`, `docs/configuration.md`, `docs/security-model.md`, `docs/troubleshooting.md`, `docs/worktrees.md`, `docs/agent-setup.md`, `docs/live-runtime.md`, `docs/publishing-checklist.md`, `CHANGELOG.md`, `examples/task.grok.json`
**Invariants:** Docs must match actual tested argv shapes. Do not mention `--no-auto-update` as emitted unless implemented. Do not describe safe mode as Grok `plan` mode.
**Out of scope:** Code changes.

**Files:**
- Modify: `README.md`
- Modify: `docs/cli-reference.md`
- Modify: `docs/configuration.md`
- Modify: `docs/security-model.md`
- Modify: `docs/troubleshooting.md`
- Modify: `docs/worktrees.md`
- Modify: `docs/agent-setup.md`
- Modify: `docs/live-runtime.md`
- Modify: `docs/publishing-checklist.md`
- Modify: `CHANGELOG.md`
- Create: `examples/task.grok.json`

**Step 1: Add docs updates**

Update:

- Install/auth prerequisites: `command -v grok`; auth owned by Grok; CI can use `XAI_API_KEY` in shell, not Delegate profiles.
- Prompt transport matrix: Grok uses prompt file.
- Safe/work examples:

```bash
delegate grok safe "Review this repository. Do not edit files."
delegate grok work "Implement the scoped change and run tests."
delegate --isolation worktree grok work --forbid-commit "Make the change without committing."
```

- Safe mode: Delegate isolated copy plus Grok read-only sandbox/permission controls.
- Work mode: `auto` by default; bypass only through harness-scoped policy.
- Snapshot/run-output: use same commands as all harnesses.
- Config reference for `grok`.
- Help/docs example cleanup: replace any existing `delegate droid grok ...` examples with a non-colliding Droid alias such as `delegate droid reviewer ...`, unless the text is explicitly discussing the difference between a top-level Grok engine and a Droid-served Grok model alias.
- Troubleshooting:
  - missing binary at `~/.grok/bin/grok`.
  - auth via Grok login or `XAI_API_KEY`.
  - safe read denied: check permission/sandbox compatibility.
  - no completion report: inspect `run-output --stdout --tail`.

**Step 2: Add JSON example**

Create `examples/task.grok.json`:

```json
{
  "engine": "grok",
  "mode": "safe",
  "model": null,
  "cwd": ".",
  "prompt": "Review this repository for correctness risks. Do not edit files."
}
```

**Step 3: Search for drift**

Run:

```bash
rg -n "cursor, droid, codex, kimi, or claude|cursor/codex/droid/kimi/claude|Cursor, Droid, OpenAI Codex, Claude Code, or Kimi|codex/kimi/claude|prompt.*stdin|prompt.*argv|safe mode" README.md docs src tests examples
```

Patch stale lists and prompt-transport claims.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli
```

- Secondary command:

```bash
rg -n "grok" README.md docs/cli-reference.md docs/configuration.md docs/security-model.md examples/task.grok.json
```

Expected: PASS and docs include Grok in the important surfaces.

---

## Task 10: End-to-end fake harness and live smoke

**Parallel:** no
**Blocked by:** Task 1 through Task 9
**Owned files:** `tests/test_end_to_end_tracking.py`, `tests/test_harness_events.py`, `tests/fixtures/grok_streaming_json_smoke.jsonl`
**Invariants:** Fake harness tests must not require real Grok auth. Live smoke is separate and may be skipped only with a clear reason.
**Out of scope:** New features.

**Files:**
- Modify: `tests/test_end_to_end_tracking.py`
- Modify: `tests/test_harness_events.py`
- Fixture: `tests/fixtures/grok_streaming_json_smoke.jsonl`

**Step 1: Add fake Grok executable test**

Create a fake `grok` in the test temp PATH that:

- records argv to a temp file.
- prints `tests/fixtures/grok_streaming_json_smoke.jsonl` to stdout.
- exits `0`.

Assert end-to-end:

- `delegate --json grok safe ...` exits `0`.
- The fake harness reads the received `--prompt-file` and proves it contains `Delegate Grok safe mode`.
- manifest public argv has `<prompt file>`, not prompt text.
- `stdout.log` has fixture output.
- `snapshot` reports Grok fields and assistant text.
- `run-output --completion-report` returns final text.

**Step 1b: Add fake Grok worktree test**

Using the same fake `grok`, run:

```bash
python3 bin/delegate.py --json --isolation worktree grok work --forbid-commit "No-op worktree smoke."
```

The fake harness should:

- record argv.
- read and record the prompt-file contents.
- print the Grok streaming fixture.
- make no source-checkout edits.
- create no commits.

Assert:

- Exit code `0`.
- recorded `--cwd` points at the Delegate-created worktree, not the source checkout.
- prompt-file transport works in work mode.
- `--forbid-commit` passes because the fake harness created no commits.
- source checkout is untouched except ignored `.delegate` metadata.

**Step 2: Run full local unit suite**

Run:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
```

Expected: PASS.

**Step 3: Live dry-run checks**

Run:

```bash
python3 bin/delegate.py --json dry-run grok safe "Review this repo. Do not edit files."
python3 bin/delegate.py --json --isolation worktree dry-run grok work --forbid-commit "No-op."
python3 bin/delegate.py --json describe --summary
python3 bin/delegate.py --json capabilities
python3 bin/delegate.py --json help grok
```

Expected:

- PASS.
- No prompt text in public argv.
- Worktree dry-run rewrites planned `--cwd`.
- Grok is present in describe/capabilities/help.
- If Task 8 implemented schema support, Grok schema dry-run emits the expected `--json-schema` shape; if unsupported, Grok schema dry-run fails with the expected unsupported error.

**Step 4: Live Grok execution smoke**

Only run when the installed `grok` is authenticated and safe to call:

```bash
cat > /tmp/delegate-grok-live-smoke.txt <<'EOF'
Read README.md and report the first heading. Do not edit files.
EOF
python3 bin/delegate.py --json grok safe --prompt-file /tmp/delegate-grok-live-smoke.txt
python3 bin/delegate.py --json snapshot --latest grok
python3 bin/delegate.py run-output grok --completion-report
```

Expected:

- Exit code `0`.
- Source checkout unchanged except `.delegate` run metadata.
- Snapshot has meaningful `assistantText`; `recentEvents` match the Task 4 stream contract and may be sparse if Grok does not expose stable event data.
- `run-output --completion-report` has the final report.
- If Task 8 implemented schema support, run one tiny live schema smoke; otherwise verify the documented unsupported error.

**Verification plan:**

- Primary command:

```bash
python3 -m unittest discover -s tests
```

- Secondary commands:

```bash
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
```

---

## Parallelization map

- Sequential critical path: Task 0 → Task 1 → Task 2 → Task 4 → Task 10.
- Can run after Task 1:
  - Task 5.
- Can run after Task 2:
  - Task 3.
- Can run after Task 1/2/5:
  - Task 6.
- Can run after Task 1/3/4:
  - Task 7.
- Required after Task 6:
  - Task 8.
- Can run after core code stabilizes:
  - Task 9.

Parallel owned-file collision rule:

- Task 1 and Task 5 both touch `src/delegate_agent/reasoning.py`; Task 1 owns the constants/helper, Task 5 owns payload rendering. Do not parallelize those edits in the same file.
- Task 2 and Task 8 both touch `src/delegate_agent/request_build.py` and `src/delegate_agent/argv_builders.py`; run Task 2 first.
- Task 5 and Task 6 both touch `src/delegate_agent/describe_payload.py`; run Task 5 first or split the exact functions.
- Task 6 and Task 9 both touch user-facing command lists; run Task 6 first, then docs.
- Task 4 and Task 10 both touch Grok stream fixtures/tests; Task 10 should only add E2E coverage after Task 4 parser tests pass.

Owned-files duplicate check:

```bash
rg '\*\*Owned files:\*\*' docs/plans/2026-06-29-grok-build-cli-full-parity.md \
  | sed 's/.*\*\*Owned files:\*\* *//' \
  | tr ',' '\n' \
  | sed 's/`//g' \
  | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
  | rg -v '^$' \
  | sort \
  | uniq -d
```

Expected duplicate files are called out in the parallelization map; do not parallelize those tasks without splitting them further.

## Final gate

Run:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests bin
ruff check .
ruff format --check .
```

Then, when local Grok auth is available:

```bash
python3 bin/delegate.py --json grok safe --prompt-file /tmp/delegate-grok-live-smoke.txt
python3 bin/delegate.py --json snapshot --latest grok
python3 bin/delegate.py run-output grok --completion-report
git status --short
```

Expected:

- All checks pass.
- Live smoke produces a tracked Grok run.
- Snapshot and run-output work without manually reading logs.
- Source checkout is unchanged except intended code/docs and `.delegate` metadata ignored by Git.
