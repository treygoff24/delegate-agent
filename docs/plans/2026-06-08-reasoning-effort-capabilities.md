# Reasoning Effort Capabilities Implementation Plan

**Goal:** Add a provider-aware `--reasoning-effort` option to Delegate Agent that lets callers request child-agent reasoning depth without making false promises across harness, provider, and model differences.

**Architecture:** Treat reasoning effort as a resolved run capability, not as a universal enum. Delegate will resolve the effective harness and model first, load the applicable effort capability from config, a refreshed workspace cache, or a bundled fallback, then either emit the correct child-runtime argv or fail closed with a precise unsupported-effort error.

**Tech Stack:** Python standard library, existing Delegate config/parser/runner modules, `unittest`, repo-local `python3 bin/delegate.py`, child CLIs (`codex`, `droid`, Cursor `agent`) only for optional live introspection.

---

## Post-implementation amendments (2026-06-09)

A recall-focused review after the initial implementation surfaced several places
where the plan's letter produced bad operational outcomes. The code now deviates
from the original text as follows; where this section conflicts with the body
below, this section wins.

1. **Config defaults degrade softly; only explicit requests fail closed.** The
   plan applied fail-closed validation uniformly to the precedence chain. In
   practice that meant one config line (`codex.defaultReasoningEffort` with the
   shipped `defaultModel: null`, or `cursor.defaultReasoningEffort` without a
   `reasoningEffortModels` mapping) hard-failed *every* run of that engine with
   no per-run escape — config validation is shape-only, so nothing caught it
   before launch. A default is a preference, not a contract: an unsatisfiable
   config default now skips effort, records a warning in the dry-run payload /
   manifest / snapshot, and the run proceeds. An explicit `--reasoning-effort`
   or run-input `reasoningEffort` still fails closed exactly as planned.
2. **The workspace cache is validated at the disk-read boundary, not at every
   internal step.** The plan validated the cache payload inside merge and write
   (up to five times per refresh) but never at load. Consequence: one malformed
   cache entry shadowed valid bundled data and failed runs, and `capabilities
   refresh` raised on the corrupt *existing* file, so the only recovery was
   manual deletion. `load_reasoning_capability_cache` now validates and treats
   malformed payloads as absent; refresh therefore self-heals by overwriting.
   Validation runs once per real boundary: external `codex debug models` output
   at parse, the cache file at load, and the merged payload at write.
3. **`requested_effort`/`resolved_effort` collapsed to a single `effort`.**
   No code path ever resolved to a different value, so the pair was threaded
   through `ReasoningCapability`, `Request`, and `RunContext` for a distinction
   that could not occur. The JSON schema is unchanged — payloads still emit both
   `requestedReasoningEffort` and `resolvedReasoningEffort` (from one field) via
   a single shared helper (`reasoning.add_reasoning_payload_fields`) used by
   dry-run, manifest, and snapshot emission so they cannot drift.
4. **`reasoning.capabilities` harness keys are restricted to `codex`/`droid`.**
   The plan's validator accepted any harness key, but only codex and droid ever
   consult the table — a declared `cursor` entry (or a typo) validated cleanly
   and was silently inert. Unknown keys now fail validation with a pointer to
   `cursor.reasoningEffortModels`.
5. **Effort strings additionally reject `"` and `\`.** The resolved effort is
   interpolated into a quoted Codex TOML override; the plan's
   whitespace-only rule let a quote-bearing declared value produce malformed
   `-c model_reasoning_effort="…"` argv.
6. **Per-model `default` in capability declarations is informational only.**
   The plan validated and displayed it but (deliberately) excluded it from the
   effort precedence chain; that exclusion is now documented rather than left
   implicit, since the field otherwise reads as a third default mechanism.
7. **Codex stdin prompt delivery failures are surfaced.** The prompt moved from
   argv to stdin during implementation; the writer thread originally swallowed
   `BrokenPipeError`/`OSError`, which could silently launch a work-mode run with
   an undelivered prompt. Failures now append a run warning and print to stderr.

---

## Design decisions

1. `--reasoning-effort LEVEL` is literal. Delegate must not silently coerce `xhigh` to `max`, `medium` to `high`, `none` to `off`, or `minimal` to `low`.
2. Reasoning effort changes only model thinking depth, cost, or latency. It must not change Delegate `safe`/`work` mode, sandboxing, approvals, Droid autonomy, Cursor force flags, or Codex policy.
3. Validation happens against the effective `(harness, model, transport)` tuple, not against a global enum.
4. Unknown or custom models fail closed for explicitly requested effort unless user config or a refreshed capability cache declares supported effort levels for that exact model. (Amended: config-sourced defaults degrade to a warning instead — see amendments above.)
5. Dry-run must expose the resolved effort plan and must never require child binaries.
6. Ordinary dry-runs and launches never invoke child binaries for capability discovery. Explicit `delegate capabilities refresh` may invoke child CLIs, validate the discovered data, and write a workspace-scoped cache under `.delegate/capabilities/reasoning.json`.
7. Capability source precedence is `config exact override > refreshed workspace cache > bundled fallback`. Config wins because users need a local escape hatch for private/custom models and stale bundled data.
8. Cursor has no standalone effort flag in the current CLI. Cursor support is model-selection based only when the user config explicitly maps effort levels to Cursor model IDs.

## Provider mappings

### Codex CLI

Current local `codex exec --help` does not expose a first-class reasoning flag. Codex does support config overrides, and local Codex config uses `model_reasoning_effort`.

Apply effort by adding this before `exec`:

```python
argv.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
```

Capability discovery:

```bash
codex debug models --bundled
codex debug models
```

Expected model catalog shape includes `slug`, `default_reasoning_level`, and `supported_reasoning_levels`.

### Droid

Current local `droid exec --help` exposes:

```text
-r, --reasoning-effort <level>  Reasoning effort (defaults per model)
```

Apply effort by adding:

```python
argv.extend(["--reasoning-effort", reasoning_effort])
```

Do not use `--auto`, `--skip-permissions-unsafe`, `--worker-reasoning-effort`, `--validator-reasoning-effort`, or `--spec-reasoning-effort` for ordinary Delegate runs.

Capability discovery for v1 can use a small bundled catalog plus optional parsing of `droid exec --help` model-detail lines. If Droid later exposes machine-readable model metadata, switch to that source.

### Cursor

Current local Cursor Agent exposes `--model` but no standalone reasoning-effort flag.

Support Cursor effort only through explicit config:

```json
{
  "cursor": {
    "reasoningEffortModels": {
      "low": "gpt-5",
      "medium": "gpt-5",
      "high": "sonnet-4-thinking",
      "xhigh": "sonnet-4-thinking"
    }
  }
}
```

If a Cursor effort is requested without a configured mapping, fail with `unsupported_reasoning_effort`.

## Public CLI and JSON contract

CLI:

```bash
delegate codex safe --reasoning-effort high "Review this repo."  # requires codex.defaultModel
delegate droid reviewer work --reasoning-effort xhigh "Implement and verify."
delegate cursor safe --reasoning-effort high "Investigate this diff."
delegate --json dry-run codex work --reasoning-effort medium "Implement..."  # requires codex.defaultModel
```

JSON run input:

```json
{
  "engine": "codex",
  "model": "gpt-5.5",
  "mode": "work",
  "reasoningEffort": "high",
  "cwd": "/path/to/workspace",
  "prompt": "Implement the scoped fix."
}
```

Effective effort precedence:

```text
per-run request value > provider config default > child runtime default
```

(Amended: a per-run request value fails closed when unsupported; an
unsatisfiable provider config default is skipped with a warning.)

Direct engine CLI requests and `run --input-json` requests are separate invocation shapes in v1. A direct engine command gets its per-run value from `--reasoning-effort`; JSON-run gets its per-run value from `reasoningEffort`. If effort is omitted at every Delegate layer, emit no effort-related argv and preserve current behavior.

## New error codes

- `missing_reasoning_effort`: `--reasoning-effort` was provided without a value.
- `invalid_reasoning_effort`: effort value is empty or malformed.
- `unsupported_reasoning_effort`: selected harness/model cannot apply the requested value.
- `invalid_reasoning_config`: config capability declaration is malformed.
- `capability_refresh_failed`: explicit capability refresh command could not inspect a child runtime.

## New manifest fields

Add these fields to run manifest and dry-run JSON payloads:

```json
{
  "requestedReasoningEffort": "high",
  "resolvedReasoningEffort": "high",
  "reasoningEffortSource": "cli",
  "reasoningCapabilitySource": "config|cache|bundled|none",
  "reasoningTransport": "codex-config|droid-flag|cursor-model-selection|none"
}
```

For Cursor model-selection, also preserve the normal `model` field so the chosen model is auditable.

## Task 1: Add reasoning capability model and resolver

**Parallel:** no
**Blocked by:** none
**Owned files:** `src/delegate_agent/reasoning.py`, `tests/test_reasoning_capabilities.py`
**Invariants:** Capability resolution must not run child binaries during ordinary dry-run tests. Unknown models fail closed for explicit effort requests unless config or a refreshed cache declares them; config defaults degrade to a warning.
**Out of scope:** Child argv construction, CLI parser changes, docs.

**Files:**
- Create: `src/delegate_agent/reasoning.py`
- Create: `tests/test_reasoning_capabilities.py`

**Step 1: Write failing unit tests**

Add tests for:

```python
def test_codex_known_model_accepts_supported_effort():
    capability = resolve_reasoning_capability(
        harness="codex",
        model="gpt-5.5",
        requested_effort="high",
        config={},
    )
    self.assertEqual(capability.transport, "codex-config")

def test_codex_effort_requires_resolved_model():
    with self.assertRaises(ReasoningCapabilityError) as ctx:
        resolve_reasoning_capability(
            harness="codex",
            model=None,
            requested_effort="high",
            config={},
        )
    self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

def test_droid_model_rejects_unsupported_effort():
    with self.assertRaises(ReasoningCapabilityError) as ctx:
        resolve_reasoning_capability(
            harness="droid",
            model="glm-5.1",
            requested_effort="medium",
            config={},
        )
    self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

def test_custom_model_requires_configured_levels():
    with self.assertRaises(ReasoningCapabilityError):
        resolve_reasoning_capability(
            harness="droid",
            model="custom:unknown",
            requested_effort="high",
            config={},
        )

def test_custom_model_accepts_configured_levels():
    config = {
        "reasoning": {
            "capabilities": {
                "droid": {
                    "custom:unknown": {"supported": ["off", "high"], "default": "off"}
                }
            }
        }
    }
    capability = resolve_reasoning_capability(
        harness="droid",
        model="custom:unknown",
        requested_effort="high",
        config=config,
    )
    self.assertEqual(capability.source, "config")

def test_config_source_overrides_cache_and_bundled():
    config = {
        "reasoning": {
            "capabilities": {
                "droid": {
                    "glm-5.1": {"supported": ["off"], "default": "off"}
                }
            }
        }
    }
    cache = {
        "harnesses": {
            "droid": {
                "models": {
                    "glm-5.1": {"supported": ["high"], "default": "high"}
                }
            }
        }
    }
    capability = resolve_reasoning_capability(
        harness="droid",
        model="glm-5.1",
        requested_effort="off",
        config=config,
        cache=cache,
    )
    self.assertEqual(capability.source, "config")

def test_cache_source_overrides_bundled_for_custom_model():
    cache = {
        "harnesses": {
            "droid": {
                "models": {
                    "custom:cached": {"supported": ["high"], "default": "high"}
                }
            }
        }
    }
    capability = resolve_reasoning_capability(
        harness="droid",
        model="custom:cached",
        requested_effort="high",
        config={},
        cache=cache,
    )
    self.assertEqual(capability.source, "cache")
```

**Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_reasoning_capabilities
```

Expected: import or assertion failures because the resolver does not exist.

**Step 3: Implement the resolver**

Create dataclasses:

```python
@dataclass(frozen=True)
class ReasoningCapability:
    harness: str
    model: str
    requested_effort: str
    resolved_effort: str
    supported_efforts: tuple[str, ...]
    default_effort: str | None
    transport: str
    source: str


class ReasoningCapabilityError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
```

Implement:

```python
def normalize_effort(value: object) -> str:
    ...

def resolve_reasoning_capability(
    *,
    harness: str,
    model: str | None,
    requested_effort: str | None,
    config: JsonObject,
    cache: JsonObject | None = None,
) -> ReasoningCapability | None:
    ...
```

The function returns `None` when `requested_effort` is `None`. Provider defaults are resolved before this function is called. If an effort is requested for Codex or Droid and `model` is `None`, raise `unsupported_reasoning_effort` because the plan cannot validate the provider/model/harness tuple. (Amended: the caller catches this for config-sourced defaults and downgrades it to a run warning.)

**Step 4: Add bundled static fallback**

Start with known current examples:

```python
BUNDLED_REASONING_CAPABILITIES = {
    "codex": {
        "gpt-5.5": {"supported": ("low", "medium", "high", "xhigh"), "default": "medium"},
        "gpt-5.4": {"supported": ("low", "medium", "high", "xhigh"), "default": "medium"},
        "gpt-5.4-mini": {"supported": ("low", "medium", "high", "xhigh"), "default": "high"},
        "gpt-5.3-codex": {"supported": ("low", "medium", "high", "xhigh"), "default": "medium"}
    },
    "droid": {
        "claude-opus-4-8": {"supported": ("off", "low", "medium", "high", "xhigh", "max"), "default": "high"},
        "claude-sonnet-4-6": {"supported": ("off", "low", "medium", "high", "max"), "default": "high"},
        "gpt-5.5": {"supported": ("low", "medium", "high", "xhigh"), "default": "medium"},
        "gemini-3.5-flash": {"supported": ("minimal", "low", "medium", "high"), "default": "high"},
        "glm-5.1": {"supported": ("off", "high"), "default": "high"},
        "minimax-m2.7": {"supported": ("high",), "default": "high"}
    }
}
```

Add a module comment that the bundled catalog is a fallback only and must not override config or refreshed cache sources.

**Step 5: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.test_reasoning_capabilities
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_reasoning_capabilities`
- Secondary check: `python3 -m compileall -q src tests bin`

## Task 2: Extend config validation for reasoning declarations

**Parallel:** no
**Blocked by:** Task 1
**Owned files:** `src/delegate_agent/config.py`, `config.example.json`, `docs/configuration.md`, `tests/test_delegate_validation.py`
**Invariants:** Existing configs without a `reasoning` section remain valid. Public example config must contain no private model IDs.
**Out of scope:** CLI parser and argv construction.

**Files:**
- Modify: `src/delegate_agent/config.py`
- Modify: `config.example.json`
- Modify: `docs/configuration.md`
- Modify: `tests/test_delegate_validation.py`

**Step 1: Write failing config tests**

Add tests for:

```python
def test_reasoning_config_rejects_empty_effort_level(self):
    config = copy.deepcopy(delegate_config.DEFAULT_CONFIG)
    config["reasoning"] = {
        "capabilities": {"droid": {"custom:x": {"supported": [""], "default": ""}}}
    }
    with self.assertRaises(delegate_config.ConfigError) as ctx:
        delegate_config.validate_config(config)
    self.assertEqual(ctx.exception.error, "invalid_reasoning_config")

def test_cursor_reasoning_effort_models_must_be_strings(self):
    config = copy.deepcopy(delegate_config.DEFAULT_CONFIG)
    config["cursor"] = dict(config["cursor"])
    config["cursor"]["reasoningEffortModels"] = {"high": 123}
    with self.assertRaises(delegate_config.ConfigError) as ctx:
        delegate_config.validate_config(config)
    self.assertEqual(ctx.exception.error, "invalid_cursor_config")

def test_provider_default_reasoning_effort_must_be_string_or_null(self):
    config = copy.deepcopy(delegate_config.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultReasoningEffort"] = 1
    with self.assertRaises(delegate_config.ConfigError) as ctx:
        delegate_config.validate_config(config)
    self.assertEqual(ctx.exception.error, "invalid_codex_config")

def test_existing_config_without_reasoning_fields_still_validates(self):
    config = copy.deepcopy(delegate_config.DEFAULT_CONFIG)
    config.pop("reasoning", None)
    config["codex"] = dict(config["codex"])
    config["codex"].pop("defaultReasoningEffort", None)
    config["droid"] = dict(config["droid"])
    config["droid"].pop("defaultReasoningEffort", None)
    config["cursor"] = dict(config["cursor"])
    config["cursor"].pop("defaultReasoningEffort", None)
    config["cursor"].pop("reasoningEffortModels", None)
    delegate_config.validate_config(config)
```

**Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_delegate_validation
```

Expected: FAIL because the new negative validation cases are not enforced yet.

**Step 3: Update default config**

Add:

```python
"reasoning": {
    "capabilities": {},
},
```

Provider defaults:

```python
"codex": {
    ...
    "defaultReasoningEffort": None,
},
"droid": {
    ...
    "defaultReasoningEffort": None,
},
"cursor": {
    ...
    "defaultReasoningEffort": None,
    "reasoningEffortModels": {},
},
```

**Step 4: Add validation helpers**

Add helpers in `src/delegate_agent/config.py`:

```python
def _validate_reasoning_effort_value(value: JsonValue, *, path: str) -> None:
    ...

def _validate_reasoning_section(reasoning: JsonValue) -> None:
    ...
```

Validation rules:
- `defaultReasoningEffort` may be `null` or a non-empty string.
- `cursor.reasoningEffortModels` is a map of non-empty string effort to non-empty string model.
- `reasoning.capabilities.<harness>.<model>.supported` is a non-empty list of non-empty strings.
- `default`, when present, must be one of `supported`.
- Provider defaults are request defaults only. They still must pass model-level validation later; config validation only checks shape.

**Step 5: Document config**

In `docs/configuration.md`, add a section explaining custom model declarations:

```json
{
  "reasoning": {
    "capabilities": {
      "droid": {
        "custom:Provider-:-Model-0": {
          "supported": ["off", "high"],
          "default": "off"
        }
      }
    }
  }
}
```

**Step 6: Run tests**

Run:

```bash
python3 -m unittest tests.test_delegate_validation
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_validation`
- Secondary check: `python3 bin/delegate.py --json describe`

## Task 3: Parse CLI and JSON reasoning effort

**Parallel:** no
**Blocked by:** Task 2
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_parser.py`, `tests/test_delegate_help_cli.py`, `tests/test_delegate_commands.py`, `examples/task.codex.json`, `examples/task.droid.json`, `examples/task.cursor.json`
**Invariants:** Help token behavior remains unchanged. `--reasoning-effort` must be parsed before direct prompt text; after prompt capture begins it is prompt text.
**Out of scope:** Provider argv application and live capability refresh.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_parser.py`
- Modify: `tests/test_delegate_help_cli.py`
- Modify: `tests/test_delegate_commands.py`
- Modify: `examples/task.codex.json`
- Modify: `examples/task.droid.json`
- Modify: `examples/task.cursor.json`

**Step 1: Write parser tests**

Add tests:

```python
def test_codex_reasoning_effort_before_prompt(self):
    parsed = self.delegate.parse_cli(["codex", "safe", "--reasoning-effort", "high", "review"])
    self.assertEqual(parsed.reasoning_effort, "high")
    self.assertEqual(parsed.prompt_parts, ["review"])

def test_droid_reasoning_effort_after_alias_and_mode(self):
    parsed = self.delegate.parse_cli([
        "droid", "reviewer", "safe", "--reasoning-effort", "high", "review"
    ])
    self.assertEqual(parsed.engine, "droid")
    self.assertEqual(parsed.model_alias, "reviewer")
    self.assertEqual(parsed.reasoning_effort, "high")
    self.assertEqual(parsed.prompt_parts, ["review"])

def test_dry_run_droid_reasoning_effort(self):
    parsed = self.delegate.parse_cli([
        "dry-run", "droid", "reviewer", "safe", "--reasoning-effort", "high", "review"
    ])
    self.assertTrue(parsed.dry_run)
    self.assertEqual(parsed.engine, "droid")
    self.assertEqual(parsed.reasoning_effort, "high")

def test_reasoning_effort_after_prompt_is_prompt_text(self):
    parsed = self.delegate.parse_cli(["codex", "safe", "review", "--reasoning-effort", "high"])
    self.assertIsNone(parsed.reasoning_effort)
    self.assertEqual(parsed.prompt_parts, ["review", "--reasoning-effort", "high"])

def test_reasoning_effort_requires_value(self):
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.parse_cli(["codex", "safe", "--reasoning-effort"])
    self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

def test_reasoning_effort_rejects_option_looking_value(self):
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.parse_cli(["codex", "safe", "--reasoning-effort", "--prompt-file", "task.md"])
    self.assertEqual(ctx.exception.error, "missing_reasoning_effort")

def test_reasoning_effort_rejects_help_token_as_value(self):
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.parse_cli(["codex", "safe", "--reasoning-effort", "--help"])
    self.assertEqual(ctx.exception.error, "missing_reasoning_effort")
```

**Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_help_cli tests.test_delegate_commands
```

Expected: FAIL because parser field and flag do not exist.

**Step 3: Update parsed/request data**

Add to `ParsedCommand`:

```python
reasoning_effort: str | None = None
```

Add to `Request`:

```python
reasoning_effort: str | None = None
reasoning_transport: str | None = None
reasoning_effort_source: str | None = None
reasoning_capability_source: str | None = None
```

Extend:

```python
RUN_INPUT_KEYS = {"engine", "mode", "model", "cwd", "prompt", "isolation", "reasoningEffort"}
```

**Step 4: Parse prompt-tail options**

Replace `parse_prompt_tail(rest)` with a helper that returns prompt file, reasoning effort, and prompt parts:

```python
def parse_prompt_tail(rest: list[str]) -> tuple[str | None, str | None, list[str]]:
    ...
```

Handle `--reasoning-effort` as a pre-prompt option only. Unlike `--prompt-file`, once direct prompt capture begins, any later `--reasoning-effort` token is ordinary prompt text and must not be rejected or parsed as an option.

**Step 5: Parse JSON input**

In `request_from_input_json`, validate:

```python
reasoning_effort = raw.get("reasoningEffort")
if reasoning_effort is not None and not isinstance(reasoning_effort, str):
    raise DelegateError("invalid_reasoning_effort", "reasoningEffort must be a non-empty string.")
```

Reject `""`.

**Step 6: Update examples**

Add `"reasoningEffort": "high"` to one example only, preferably `examples/task.codex.json`, and leave the other examples without it to demonstrate optional behavior.

**Step 7: Run tests**

Run:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_help_cli tests.test_delegate_commands
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_parser tests.test_delegate_help_cli tests.test_delegate_commands`
- Manual smoke: none for this parser-only task; provider dry-run smoke belongs in Task 4 after argv support exists.

## Task 4: Apply reasoning effort in provider argv builders

**Parallel:** no
**Blocked by:** Task 3
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_commands.py`, `tests/test_delegate_execution.py`
**Invariants:** No-effort argv remains unchanged. Reasoning effort must not modify safe/work policy fields.
**Out of scope:** Capability refresh command and docs.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_commands.py`
- Modify: `tests/test_delegate_execution.py`

**Step 1: Write failing argv tests**

Add tests:

```python
def test_codex_reasoning_effort_argv_uses_config_override(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultModel"] = "gpt-5.5"
    request = self.delegate.build_request(
        "codex", "safe", None, "/repo", "hello", config, True,
        reasoning_effort="high",
    )
    self.assertIn("-c", request.argv)
    self.assertIn('model_reasoning_effort="high"', request.argv)

def test_codex_reasoning_effort_fails_without_model(self):
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.build_request(
            "codex", "safe", None, "/repo", "hello", self.delegate.DEFAULT_CONFIG, True,
            reasoning_effort="high",
        )
    self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

def test_droid_reasoning_effort_argv_uses_flag(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["droid"]["models"] = {"reviewer": "gpt-5.5"}
    request = self.delegate.build_request(
        "droid", "safe", "reviewer", "/repo", "hello", config, True,
        reasoning_effort="xhigh",
    )
    self.assertIn("--reasoning-effort", request.argv)
    self.assertIn("xhigh", request.argv)

def test_build_request_uses_cache_declared_custom_model_capability(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["droid"]["models"] = {"reviewer": "custom:cached"}
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / ".delegate" / "capabilities" / "reasoning.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps({
            "schema": 1,
            "harnesses": {
                "droid": {
                    "models": {
                        "custom:cached": {"supported": ["high"], "default": "high"}
                    }
                }
            }
        }))
        request = self.delegate.build_request(
            "droid", "safe", "reviewer", tmp, "hello", config, True,
            reasoning_effort="high",
        )
    self.assertIn("--reasoning-effort", request.argv)

def test_cursor_reasoning_effort_requires_mapping(self):
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.build_request(
            "cursor", "safe", None, "/repo", "hello", self.delegate.DEFAULT_CONFIG, True,
            reasoning_effort="high",
        )
    self.assertEqual(ctx.exception.error, "unsupported_reasoning_effort")

def test_cursor_reasoning_effort_uses_configured_model_mapping(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["cursor"] = dict(config["cursor"])
    config["cursor"]["reasoningEffortModels"] = {"high": "sonnet-4-thinking"}
    request = self.delegate.build_request(
        "cursor", "safe", None, "/repo", "hello", config, True,
        reasoning_effort="high",
    )
    self.assertEqual(request.model, "sonnet-4-thinking")
    self.assertIn("--model", request.argv)
    self.assertIn("sonnet-4-thinking", request.argv)

def test_codex_default_reasoning_effort_is_used_when_request_omits_effort(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultModel"] = "gpt-5.5"
    config["codex"]["defaultReasoningEffort"] = "medium"
    request = self.delegate.build_request(
        "codex", "safe", None, "/repo", "hello", config, True,
        reasoning_effort=None,
    )
    self.assertEqual(request.reasoning_effort_source, "config")
    self.assertIn('model_reasoning_effort="medium"', request.argv)

def test_request_from_parsed_threads_cli_reasoning_effort(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultModel"] = "gpt-5.5"
    with tempfile.TemporaryDirectory() as tmp:
        parsed = self.delegate.parse_cli([
            "--cwd", tmp, "codex", "safe", "--reasoning-effort", "high", "review"
        ])
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        payload = self.delegate.dry_run_payload(request)
    self.assertEqual(payload["requestedReasoningEffort"], "high")
    self.assertEqual(payload["reasoningEffortSource"], "cli")
    self.assertIn('model_reasoning_effort="high"', payload["argv"])

def test_request_from_input_json_threads_reasoning_effort(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task.json"
        task.write_text(json.dumps({
            "engine": "codex",
            "mode": "safe",
            "model": "gpt-5.5",
            "cwd": tmp,
            "reasoningEffort": "high",
            "prompt": "review",
        }))
        parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
        request = self.delegate.request_from_input_json(parsed, config)
    self.assertEqual(request.reasoning_effort_source, "input-json")
    self.assertIn('model_reasoning_effort="high"', request.argv)

def test_input_json_effort_overrides_provider_default(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultReasoningEffort"] = "medium"
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task.json"
        task.write_text(json.dumps({
            "engine": "codex",
            "mode": "safe",
            "model": "gpt-5.5",
            "cwd": tmp,
            "reasoningEffort": "high",
            "prompt": "review",
        }))
        parsed = self.delegate.ParsedCommand("run", json_mode=True, input_json=str(task))
        request = self.delegate.request_from_input_json(parsed, config)
    self.assertIn('model_reasoning_effort="high"', request.argv)
    self.assertNotIn('model_reasoning_effort="medium"', request.argv)

def test_per_run_effort_overrides_provider_default(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultModel"] = "gpt-5.5"
    config["codex"]["defaultReasoningEffort"] = "medium"
    with tempfile.TemporaryDirectory() as tmp:
        parsed = self.delegate.parse_cli([
            "--cwd", tmp, "codex", "safe", "--reasoning-effort", "high", "review"
        ])
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
    self.assertIn('model_reasoning_effort="high"', request.argv)
    self.assertNotIn('model_reasoning_effort="medium"', request.argv)
```

**Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_delegate_commands tests.test_delegate_execution
```

Expected: FAIL because `build_request` and argv builders do not accept effort.

**Step 3: Resolve effective effort and effective model**

Add provider-default source tracking:

```python
def provider_default_reasoning_effort(config: JsonObject, engine: str) -> str | None:
    ...
```

For Codex and Droid, resolve the effective model first, then resolve capability:

```python
requested_effort = reasoning_effort or provider_default_reasoning_effort(config, engine)
capability = resolve_reasoning_capability(
    harness=engine,
    model=model,
    requested_effort=requested_effort,
    config=config,
    cache=load_reasoning_capability_cache(resolved.path),
)
```

Wrap `ReasoningCapabilityError` as `DelegateError`.

Update real request construction paths:

```python
return build_request(
    parsed.engine,
    parsed.mode,
    parsed.model_alias,
    workspace,
    prompt,
    config,
    parsed.dry_run,
    reasoning_effort=parsed.reasoning_effort,
    ...
)
```

And for JSON input:

```python
return build_request(
    str(engine),
    str(mode),
    model_alias,
    workspace,
    prompt,
    config,
    dry_run=False,
    reasoning_effort=reasoning_effort,
    ...
)
```

For Cursor, handle effort before final model selection because transport is model-selection:

```python
requested_effort = reasoning_effort or provider_default_reasoning_effort(config, "cursor")
if requested_effort:
    model = cursor_model_for_effort(config, requested_effort)
else:
    model = cursor["defaultModel"]
```

If effort is requested or defaulted and no Cursor model mapping exists, raise `unsupported_reasoning_effort`.

**Step 4: Update argv builders**

Add optional `reasoning_capability: ReasoningCapability | None` to:

- `build_cursor_argv`
- `build_droid_argv`
- `build_codex_argv`

Apply by transport:

```python
if reasoning_capability and reasoning_capability.transport == "codex-config":
    argv.extend(["-c", f'model_reasoning_effort="{reasoning_capability.resolved_effort}"'])
```

For Codex, insert `-c` before `exec`.

For Droid, insert `--reasoning-effort LEVEL` after `exec` and before the prompt.

For Cursor model-selection, select the mapped model before building `--model`.

**Step 5: Preserve no-effort argv**

Add regression tests that existing no-effort argv tests still pass without adding effort flags.

**Step 6: Run tests**

Run:

```bash
python3 -m unittest tests.test_delegate_commands tests.test_delegate_execution
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_commands tests.test_delegate_execution`
- Manual smoke:
  `printf '{"codex":{"defaultModel":"gpt-5.5"}}\n' > /tmp/delegate-codex-reasoning.json`
  then `DELEGATE_CONFIG=/tmp/delegate-codex-reasoning.json python3 bin/delegate.py --json dry-run codex safe --reasoning-effort high "Review only."`

## Task 5: Add capabilities reporting and workspace cache refresh

**Parallel:** no
**Blocked by:** Task 4
**Owned files:** `src/delegate_agent/cli.py`, `src/delegate_agent/command_help.py`, `src/delegate_agent/reasoning.py`, `tests/test_command_help.py`, `tests/test_delegate_help_cli.py`, `tests/test_delegate_commands.py`, `docs/cli-reference.md`
**Invariants:** `delegate --json capabilities` must not require child binaries. Explicit refresh may invoke child CLIs but must fail cleanly.
**Out of scope:** Changing launch behavior beyond using the already implemented resolver.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `src/delegate_agent/command_help.py`
- Modify: `src/delegate_agent/reasoning.py`
- Modify: `tests/test_command_help.py`
- Modify: `tests/test_delegate_help_cli.py`
- Modify: `tests/test_delegate_commands.py`
- Modify: `docs/cli-reference.md`

**Step 1: Write failing command tests**

Add tests:

```python
def write_fake_executable(self, name: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> Path:
    ...

# The helper returns the fake executable directory. Tests prepend that directory
# to PATH; they do not prepend the executable file path itself.

def test_capabilities_json_reports_reasoning_matrix(self):
    code, out, err = self.run_main(["--json", "capabilities"])
    self.assertEqual(code, 0, err)
    payload = json.loads(out)
    self.assertTrue(payload["ok"])
    self.assertIn("reasoning", payload)
    self.assertIn("codex", payload["reasoning"]["harnesses"])

def test_capabilities_json_reports_cache_source_when_cache_exists(self):
    with tempfile.TemporaryDirectory() as workspace:
        cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps({
            "schema": 1,
            "harnesses": {
                "codex": {
                    "models": {
                        "gpt-test": {"supported": ["low"], "default": "low"}
                    }
                }
            }
        }))
        code, out, err = self.run_main(["--cwd", workspace, "--json", "capabilities"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(
            payload["reasoning"]["harnesses"]["codex"]["models"]["gpt-test"]["source"],
            "cache",
        )

def test_capabilities_refresh_writes_valid_cache_from_fake_codex(self):
    with tempfile.TemporaryDirectory() as workspace:
        fake_bin = self.write_fake_executable(
            "codex",
            stdout=json.dumps({
                "models": [{
                    "slug": "gpt-refresh",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
                }]
            }),
        )
        code, out, err = self.run_main(
            ["--cwd", workspace, "--json", "capabilities", "refresh"],
            path_prefix=fake_bin,
        )
        self.assertEqual(code, 0, err)
        cache = json.loads(
            (Path(workspace) / ".delegate" / "capabilities" / "reasoning.json").read_text()
        )
        self.assertIn("gpt-refresh", cache["harnesses"]["codex"]["models"])

def test_capabilities_refresh_invalid_data_does_not_mutate_existing_cache(self):
    with tempfile.TemporaryDirectory() as workspace:
        cache_path = Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text('{"schema":1,"harnesses":{"codex":{"models":{"old":{"supported":["low"],"default":"low"}}}}}')
        fake_bin = self.write_fake_executable("codex", stdout='{"models":[{"slug":"bad"}]}')
        code, out, err = self.run_main(
            ["--cwd", workspace, "--json", "capabilities", "refresh"],
            path_prefix=fake_bin,
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], "capability_refresh_failed")
        self.assertIn("old", json.loads(cache_path.read_text())["harnesses"]["codex"]["models"])

def test_capabilities_refresh_subprocess_failure_reports_error(self):
    with tempfile.TemporaryDirectory() as workspace:
        fake_bin = self.write_fake_executable("codex", exit_code=1, stderr="boom")
        code, out, err = self.run_main(
            ["--cwd", workspace, "--json", "capabilities", "refresh"],
            path_prefix=fake_bin,
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], "capability_refresh_failed")
```

**Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli tests.test_delegate_commands
```

Expected: FAIL because `capabilities` is unknown.

**Step 3: Add command**

Support:

```bash
delegate capabilities
delegate --json capabilities
delegate capabilities refresh
```

`capabilities` reads config, workspace cache, and bundled capabilities using the same precedence as the resolver. `capabilities refresh` may attempt:

```bash
codex debug models
droid exec --help
agent models
```

Store refreshed data under workspace-local `.delegate/capabilities/reasoning.json` only if the command succeeds and the schema validates. This cache is runtime state, not a source-controlled artifact. Ordinary dry-runs and launches read it but do not create or mutate it.

**Step 4: Wire cache reads into resolver and capabilities output**

Add helpers in `src/delegate_agent/reasoning.py`:

```python
def reasoning_capability_cache_path(workspace: str) -> Path:
    return Path(workspace) / ".delegate" / "capabilities" / "reasoning.json"

def load_reasoning_capability_cache(workspace: str) -> JsonObject | None:
    ...

def build_reasoning_capabilities_payload(config: JsonObject, cache: JsonObject | None) -> JsonObject:
    ...
```

The resolver and `delegate --json capabilities` must both consume the same merged view. Source labels must be `config`, `cache`, or `bundled`.

**Step 5: Add payload**

Return:

```json
{
  "ok": true,
  "reasoning": {
    "harnesses": {
      "codex": {
        "transport": "codex-config",
        "models": {
          "gpt-5.5": {
            "supported": ["low", "medium", "high", "xhigh"],
            "default": "medium",
            "source": "bundled"
          }
        }
      }
    }
  }
}
```

**Step 6: Document help**

Add a `capabilities` `CommandSpec` in `src/delegate_agent/command_help.py`.

**Step 7: Run tests**

Run:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli tests.test_delegate_commands
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_command_help tests.test_delegate_help_cli tests.test_delegate_commands`
- Manual smoke: `python3 bin/delegate.py --json capabilities`

## Task 6: Persist reasoning metadata in manifests, dry-run output, snapshots

**Parallel:** no
**Blocked by:** Task 4
**Owned files:** `src/delegate_agent/runner.py`, `src/delegate_agent/cli.py`, `src/delegate_agent/rendering.py`, `tests/test_delegate_execution.py`, `tests/test_snapshot_commands.py`
**Invariants:** Existing manifest schema readers tolerate missing fields from old runs. Secret redaction behavior remains unchanged.
**Out of scope:** Capability refresh.

**Files:**
- Modify: `src/delegate_agent/runner.py`
- Modify: `src/delegate_agent/cli.py`
- Modify: `src/delegate_agent/rendering.py`
- Modify: `tests/test_delegate_execution.py`
- Modify: `tests/test_snapshot_commands.py`

**Step 1: Write failing dry-run and manifest tests**

Add tests:

```python
def test_dry_run_reports_reasoning_fields(self):
    config = copy.deepcopy(self.delegate.DEFAULT_CONFIG)
    config["codex"] = dict(config["codex"])
    config["codex"]["defaultModel"] = "gpt-5.5"
    with tempfile.TemporaryDirectory() as tmp:
        parsed = self.delegate.parse_cli([
            "--cwd", tmp, "--json", "dry-run", "codex", "safe",
            "--reasoning-effort", "high", "review",
        ])
        request = self.delegate.request_from_parsed(parsed, config, io.StringIO(""))
        payload = self.delegate.dry_run_payload(request)
    self.assertEqual(payload["requestedReasoningEffort"], "high")
    self.assertEqual(payload["resolvedReasoningEffort"], "high")
    self.assertEqual(payload["reasoningTransport"], "codex-config")

def test_manifest_includes_reasoning_fields(self):
    ...
```

**Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_delegate_execution tests.test_snapshot_commands
```

Expected: FAIL because metadata is absent.

**Step 3: Extend runner context**

Add fields to `delegate_runner.RunContext`:

```python
requested_reasoning_effort: str | None = None
resolved_reasoning_effort: str | None = None
reasoning_effort_source: str | None = None
reasoning_capability_source: str | None = None
reasoning_transport: str | None = None
```

Add corresponding manifest keys only when values are not `None`.

**Step 4: Extend dry-run payload**

Where dry-run JSON is emitted, include the same optional keys.

**Step 5: Extend snapshot rendering**

Update `build_snapshot` and any `delegate_rendering.merge_snapshot_view` whitelist so `snapshot` and `run-output` views can expose the new manifest keys without breaking old runs that do not have them.

**Step 6: Run tests**

Run:

```bash
python3 -m unittest tests.test_delegate_execution tests.test_snapshot_commands
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_execution tests.test_snapshot_commands`
- Manual smoke:
  `printf '{"codex":{"defaultModel":"gpt-5.5"}}\n' > /tmp/delegate-codex-reasoning.json`
  then `DELEGATE_CONFIG=/tmp/delegate-codex-reasoning.json python3 bin/delegate.py --json dry-run codex safe --reasoning-effort high "Review only."`

## Task 7: Update help, docs, and examples

**Parallel:** no
**Blocked by:** Tasks 3, 4, 5
**Owned files:** `README.md`, `docs/cli-reference.md`, `docs/configuration.md`, `docs/agent-setup.md`, `docs/troubleshooting.md`, `docs/security-model.md`, `src/delegate_agent/command_help.py`, `tests/test_command_help.py`, `tests/test_delegate_help_cli.py`
**Invariants:** Docs must say effort is model-specific and fail-closed for explicit requests (config defaults warn and skip). Do not imply PyPI publication or private model availability.
**Out of scope:** Code behavior changes.

**Files:**
- Modify: `README.md`
- Modify: `docs/cli-reference.md`
- Modify: `docs/configuration.md`
- Modify: `docs/agent-setup.md`
- Modify: `docs/troubleshooting.md`
- Modify: `docs/security-model.md`
- Modify: `src/delegate_agent/command_help.py`
- Modify: `tests/test_command_help.py`
- Modify: `tests/test_delegate_help_cli.py`

**Step 1: Update command help**

Add `--reasoning-effort LEVEL` to `cursor`, `codex`, `droid`, and `dry-run` specs.

Add notes:

```text
Reasoning effort is validated against the resolved harness and model. Unsupported explicit values fail closed; unsatisfiable config defaults warn and are skipped.
```

**Step 2: Update CLI reference**

Document examples:

```bash
delegate codex safe --reasoning-effort high "Review only."  # requires codex.defaultModel
delegate droid reviewer work --reasoning-effort xhigh "Implement and verify."
delegate --json capabilities
```

Document unsupported examples:

```text
Droid glm-5.1 + medium fails because glm-5.1 supports off|high.
Cursor high fails unless cursor.reasoningEffortModels.high is configured.
Codex high fails if neither run input nor config resolves a Codex model.
```

**Step 3: Update security model**

State explicitly:

```text
Reasoning effort does not change Delegate runtime permissions, sandbox mode, network policy, or edit capability.
```

**Step 4: Update troubleshooting**

Add entries for:

- `unsupported_reasoning_effort`
- stale bundled capabilities
- custom model capability declarations

**Step 5: Run docs/help tests**

Run:

```bash
python3 -m unittest tests.test_command_help tests.test_delegate_help_cli
```

Expected: PASS.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_command_help tests.test_delegate_help_cli`
- Secondary check: `python3 bin/delegate.py --json codex --help`

## Task 8: Full validation and regression pass

**Parallel:** no
**Blocked by:** Tasks 1-7
**Owned files:** none
**Invariants:** Required validation should not need real Cursor, Droid, Codex, Claude, or Kimi binaries except optional live capability refresh smoke.
**Out of scope:** Packaging and release.

**Files:**
- Test only; no planned edits.

**Step 1: Run narrow reasoning tests**

Run:

```bash
python3 -m unittest tests.test_reasoning_capabilities
```

Expected: PASS.

**Step 2: Run full unit suite**

Run:

```bash
python3 -m unittest discover -s tests
```

Expected: PASS.

**Step 3: Run compile check**

Run:

```bash
python3 -m compileall -q src tests bin
```

Expected: no output and exit 0.

**Step 4: Run lint/format checks when dev deps are available**

Run:

```bash
ruff check .
ruff format --check .
```

Expected: PASS.

If `ruff` is unavailable, report that dev dependencies are missing and do not claim lint was run.

**Step 5: Run dry-run smoke tests**

Run:

```bash
printf '{"codex":{"defaultModel":"gpt-5.5"}}\n' > /tmp/delegate-codex-reasoning.json
DELEGATE_CONFIG=/tmp/delegate-codex-reasoning.json python3 bin/delegate.py --json dry-run codex safe --reasoning-effort high "Review only."
python3 bin/delegate.py --json capabilities
```

Expected:
- First command includes Codex `-c model_reasoning_effort="high"` in planned argv.
- Second command reports reasoning capability data and source.

**Step 6: Optional live introspection smoke**

Only run if the relevant child binaries are present:

```bash
command -v codex >/dev/null && codex debug models --bundled >/tmp/delegate-codex-models.json
command -v droid >/dev/null && droid exec --help >/tmp/delegate-droid-help.txt
```

Expected: exit 0. Do not fail implementation if optional binaries are absent.

**Verification plan:**
- Primary command: `python3 -m unittest discover -s tests`
- Secondary checks: `python3 -m compileall -q src tests bin`, `ruff check .`, `ruff format --check .`

## Acceptance criteria

1. A Codex run with a resolved model from run input or `codex.defaultModel` validates that model and emits `-c model_reasoning_effort="high"` before `exec`.
2. `delegate droid ALIAS ... --reasoning-effort high` validates the resolved Droid model and emits `--reasoning-effort high`.
3. `delegate cursor ... --reasoning-effort high` fails closed unless `cursor.reasoningEffortModels.high` is configured.
4. Unsupported model/effort pairs fail before launch with `unsupported_reasoning_effort`.
5. Custom model effort support is configurable without editing Delegate source.
6. Dry-run, manifest, and snapshot output record requested effort, resolved effort, transport, effort source, and capability source.
7. Docs and help clearly state model-specific validation and no silent coercion.
8. Existing no-effort behavior remains unchanged.

## Rollback plan

If a provider mapping proves unstable after merge:

1. Leave parser and manifest fields intact.
2. Disable only the affected harness transport by marking it unsupported in the capability resolver.
3. Keep dry-run and `capabilities` reporting available so users can see why the harness is disabled.
4. Add a troubleshooting note with the affected runtime version and the expected fix.

## Open implementation notes

1. Prefer `codex debug models --bundled` for deterministic tests and `codex debug models` for user-initiated refresh.
2. If parsing `droid exec --help` proves brittle, do not block v1 on live Droid parsing. Use bundled plus config and mark Droid live source as unavailable.
3. The exact TOML quoting for Codex should be tested through argv construction, not shell string matching.
4. Avoid adding a broad `--reasoning-profile fastest|balanced|deepest` in v1. That is a separate abstraction and would require semantic mapping policy.
