# Codex Harness and Policy Controls Implementation Plan

**Goal:** Add OpenAI Codex CLI as a first-class Delegate Agent harness and introduce granular, system-wide risk/network/trust policy controls that are safe by default but easy to tune.

**Architecture:** Keep Delegate as the orchestrator and run Codex through `codex exec` in non-interactive mode. Add a global policy resolver that computes mode-level defaults, built-in risk profiles, per-harness overrides, and CLI/JSON request intent before each harness-specific argv builder consumes only the policy fields it supports. Preserve existing Cursor/Droid semantics unless explicitly configured otherwise.

**Tech Stack:** Python standard library, `unittest`, existing Delegate run registry/tracking, OpenAI Codex CLI `codex exec --json`, current Cursor Agent and Factory Droid harnesses.

---

## Design Decisions From Review

- **Verified Codex target:** v1 targets `codex-cli 0.133.0`. Local `codex exec --help` confirms support for `-c/--config <key=value>`, `-m/--model`, `-p/--profile`, `-s/--sandbox`, `-C/--cd`, `--dangerously-bypass-hook-trust`, `--dangerously-bypass-approvals-and-sandbox`, `--color`, `--ephemeral`, `--json`, `--skip-git-repo-check`, `--ignore-user-config`, and `-o/--output-last-message`. Local `codex --help` confirms `-c/--config`, `--model`, `--profile`, `--sandbox`, `--dangerously-bypass-hook-trust`, `--dangerously-bypass-approvals-and-sandbox`, `-a/--ask-for-approval`, and `--search` are accepted before the subcommand. **`--ask-for-approval` and `--search` are global-only** (not accepted as `codex exec` flags), which is why the argv builder places them before `exec`. `-c key=value` is accepted both globally and on `exec`; v1 emits it after `exec` alongside the other sandbox-related flags.
- **Codex work mode keeps workspace containment:** default to `--sandbox workspace-write`, not `--dangerously-bypass-approvals-and-sandbox`.
- **Codex work mode gets network access by default:** use `-c sandbox_workspace_write.network_access=true` when the effective Codex sandbox is `workspace-write` and the effective work policy enables network.
- **Codex non-interactive runs do not prompt:** `build_codex_argv` always emits `--ask-for-approval never` unless `bypassApprovalsAndSandbox` is true. `approvalPolicy` is **not** exposed as a policy field in v1, because every non-`never` value (`untrusted`, `on-request`) escalates to the user and would hang tracked runs. If interactive Codex is ever needed, users can invoke `codex` directly.
- **Hook trust bypass is configurable, not hard-coded:** out-of-box open-source defaults remain safe, but a single policy profile can enable `--dangerously-bypass-hook-trust` for Codex work runs.
- **Full approval+sandbox bypass is explicit:** expose it through a named high-risk profile and granular booleans, but document that it is intended only when Delegate itself runs inside an externally isolated workspace/container/VM.
- **Policy is system-wide first, harness-specific second:** unsupported policy fields are ignored with clear `describe` metadata; harnesses do not invent unsafe equivalents.
- **Config-first risk controls:** v1 should support durable config/profile toggles rather than adding per-invocation `--network` / `--no-network` CLI flags. CLI policy overrides can be a later compatibility-preserving enhancement if real usage shows they are needed.

## Proposed Configuration Shape

Default embedded config after this feature:

```json
{
  "tracking": {
    "completionReport": {
      "defaultMode": "markdown"
    },
    "retention": {
      "enabled": true,
      "rawLogDays": 7
    }
  },
  "policy": {
    "profile": "safe",
    "work": {
      "networkAccess": true
    }
  },
  "cursor": {
    "argvPrefix": [
      "agent"
    ],
    "defaultModel": "composer-2.5"
  },
  "droid": {
    "binary": "droid",
    "models": {
      "minimax": "custom:OpenCode-Go-:-MiniMax-M2.7-8"
    }
  },
  "codex": {
    "binary": "codex",
    "defaultModel": null,
    "profile": null,
    "workSandbox": "workspace-write",
    "ephemeral": true,
    "ignoreUserConfig": false
  }
}
```

Built-in `policy.profile` values:

| Profile | Effect |
| --- | --- |
| `safe` | Safe mode has no network; work mode has workspace-write + network; no bypasses. |
| `trusted-hooks` | Same as `safe`, but work-mode policy defaults to `bypassHookTrust: true`. Harnesses without a hook-trust bypass ignore it and report it as unsupported. |
| `external-sandbox` | Work-mode policy defaults to `bypassApprovalsAndSandbox: true` and `bypassHookTrust: true`. Must be documented as only appropriate when Delegate is already externally sandboxed. |
| `custom` | No profile expansion; explicit fields fully control behavior. |

Important default-config rule: the embedded `DEFAULT_CONFIG` and `config.example.json` must be sparse for false/default policy fields. Do **not** pin `bypassHookTrust: false`, `bypassApprovalsAndSandbox: false`, `webSearch: false`, or a default `policy.harness.codex.work.bypassHookTrust: false` in embedded config, because those explicit false values would override built-in profiles after config merge. The resolver's `DEFAULT_MODE_POLICY` supplies safe false defaults; config files only need to set fields that intentionally differ from that baseline.

Example Trey/local power-user config:

```json
{
  "policy": {
    "profile": "trusted-hooks",
    "work": {
      "networkAccess": true,
      "bypassHookTrust": true,
      "bypassApprovalsAndSandbox": false
    }
  }
}
```

Example external-isolation automation config:

```json
{
  "policy": {
    "profile": "external-sandbox"
  }
}
```

## Codex Argv Mapping

Default tracked safe:

```bash
codex \
  --ask-for-approval never \
  exec \
  --cd <isolated-or-source-workspace> \
  --sandbox read-only \
  --color never \
  --json \
  --ephemeral \
  <prompt>
```

Default tracked work:

```bash
codex \
  --ask-for-approval never \
  exec \
  --cd <workspace> \
  --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=true \
  --color never \
  --json \
  --ephemeral \
  <prompt>
```

Trusted-hooks tracked work adds:

```bash
--dangerously-bypass-hook-trust
```

External-sandbox tracked work replaces approval+sandbox controls with:

```bash
codex \
  exec \
  --cd <workspace> \
  --dangerously-bypass-approvals-and-sandbox \
  --dangerously-bypass-hook-trust \
  --color never \
  --json \
  --ephemeral \
  <prompt>
```

Non-Git workspaces add:

```bash
--skip-git-repo-check
```

If `policy.<mode>.webSearch` is true for Codex, add global:

```bash
--search
```

`networkAccess` and `webSearch` intentionally remain separate:
- `networkAccess` controls sandboxed subprocess egress, such as package manager installs, local MCP launchers, or commands that need HTTP.
- `webSearch` controls the native Codex/OpenAI web search tool where supported.
- Work mode defaults `networkAccess: true` and `webSearch: false` so implementation tasks can install/fetch dependencies without automatically giving the model a native search tool. Users can set `policy.work.webSearch: true`.

## Implementation Tasks

### Task 1: Add policy schema and resolver

**Parallel:** no
**Blocked by:** none
**Owned files:** `src/delegate_agent/config.py`, `tests/test_delegate_validation.py`, `config.example.json`
**Invariants:** Existing config precedence stays `embedded < global < workspace < explicit DELEGATE_CONFIG < cli overrides`; existing Cursor/Droid configs remain valid.
**Out of scope:** Do not add CLI flags yet; this task only adds config loading/validation/effective-policy helpers.

**Files:**
- Modify: `src/delegate_agent/config.py`
- Modify: `tests/test_delegate_validation.py`
- Modify: `config.example.json`

**Step 1: Write failing validation tests**

Add tests in `tests/test_delegate_validation.py`:

```python
def test_policy_default_profile_safe_resolves_work_network(self):
    config_mod = load_config_module()
    policy = config_mod.effective_policy(
        config_mod.DEFAULT_CONFIG,
        engine="codex",
        mode="work",
    )
    self.assertTrue(policy["networkAccess"])
    self.assertNotIn("approvalPolicy", policy)
    self.assertFalse(policy["bypassApprovalsAndSandbox"])
    self.assertFalse(policy["bypassHookTrust"])


def test_policy_default_profile_safe_resolves_safe_no_network_or_bypasses(self):
    config_mod = load_config_module()
    policy = config_mod.effective_policy(
        config_mod.DEFAULT_CONFIG,
        engine="codex",
        mode="safe",
    )
    self.assertFalse(policy["networkAccess"])
    self.assertFalse(policy["webSearch"])
    self.assertFalse(policy["bypassApprovalsAndSandbox"])
    self.assertFalse(policy["bypassHookTrust"])


def test_policy_rejects_approval_policy_field(self):
    # approvalPolicy was dropped in v1 because non-"never" values hang tracked runs.
    # Stale configs that still set it must fail validation, not silently no-op.
    config_mod = load_config_module()
    with self.assertRaises(config_mod.DelegateError):
        config_mod.validate_config(
            config_mod.deep_merge(
                config_mod.DEFAULT_CONFIG,
                {"policy": {"work": {"approvalPolicy": "on-request"}}},
            )
        )


def test_policy_trusted_hooks_profile_enables_codex_work_hook_bypass(self):
    config_mod = load_config_module()
    loaded = config_mod.deep_merge(
        config_mod.DEFAULT_CONFIG,
        {"policy": {"profile": "trusted-hooks"}},
    )
    policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
    self.assertTrue(policy["networkAccess"])
    self.assertTrue(policy["bypassHookTrust"])
    self.assertFalse(policy["bypassApprovalsAndSandbox"])


def test_policy_external_sandbox_profile_enables_full_codex_work_bypass(self):
    config_mod = load_config_module()
    loaded = config_mod.deep_merge(
        config_mod.DEFAULT_CONFIG,
        {"policy": {"profile": "external-sandbox"}},
    )
    policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
    self.assertTrue(policy["bypassApprovalsAndSandbox"])
    self.assertTrue(policy["bypassHookTrust"])


def test_policy_explicit_mode_override_beats_profile_defaults(self):
    config_mod = load_config_module()
    loaded = config_mod.deep_merge(
        config_mod.DEFAULT_CONFIG,
        {
            "policy": {
                "profile": "trusted-hooks",
                "work": {"bypassHookTrust": False},
            }
        },
    )
    policy = config_mod.effective_policy(loaded, engine="codex", mode="work")
    self.assertFalse(policy["bypassHookTrust"])
```

**Step 2: Run tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_delegate_validation -k policy
```

Expected: fails because `effective_policy` does not exist.

**Step 3: Implement policy defaults and validation**

In `src/delegate_agent/config.py`, add:

```python
POLICY_PROFILES = ("safe", "trusted-hooks", "external-sandbox", "custom")

DEFAULT_MODE_POLICY: JsonObject = {
    "networkAccess": False,
    "webSearch": False,
    "bypassApprovalsAndSandbox": False,
    "bypassHookTrust": False,
}
```

Add `policy` and `codex` to `DEFAULT_CONFIG`.

Add helpers:

```python
def _profile_policy(profile: str) -> JsonObject:
    if profile == "trusted-hooks":
        return {"work": {"bypassHookTrust": True}}
    if profile == "external-sandbox":
        return {
            "work": {
                "bypassApprovalsAndSandbox": True,
                "bypassHookTrust": True,
            }
        }
    return {}


def effective_policy(config: JsonObject, *, engine: str, mode: str) -> JsonObject:
    policy = config.get("policy", {})
    if not isinstance(policy, dict):
        policy = {}
    profile = policy.get("profile", "safe")
    profile_defaults = _profile_policy(profile if isinstance(profile, str) else "safe")
    profile_mode = profile_defaults.get(mode)
    mode_policy = deep_merge(
        DEFAULT_MODE_POLICY,
        profile_mode if isinstance(profile_mode, dict) else {},
    )
    explicit_mode = policy.get(mode)
    if isinstance(explicit_mode, dict):
        mode_policy = deep_merge(mode_policy, explicit_mode)
    harness = policy.get("harness")
    if isinstance(harness, dict):
        engine_policy = harness.get(engine)
        if isinstance(engine_policy, dict):
            mode_override = engine_policy.get(mode)
            if isinstance(mode_override, dict):
                mode_policy = deep_merge(mode_policy, mode_override)
    return mode_policy
```

Precedence must be:

```text
built-in DEFAULT_MODE_POLICY
< profile defaults
< explicit policy.safe/work
< explicit policy.harness.<engine>.<mode>
```

This means users can select `trusted-hooks` and still turn off hook bypass with:

```json
{
  "policy": {
    "profile": "trusted-hooks",
    "work": {
      "bypassHookTrust": false
    }
  }
}
```

Add validation for:
- `policy` object.
- `policy.profile` in `safe|trusted-hooks|external-sandbox|custom`.
- mode policy fields have expected boolean types.
- Reject unknown keys in `policy.<mode>` and `policy.harness.<engine>.<mode>` (including the now-removed `approvalPolicy`) so stale configs surface a clear validation error rather than silently no-op'ing.
- `codex.binary` non-empty string.
- `codex.defaultModel` string or null.
- `codex.profile` string or null.
- `codex.workSandbox` in `read-only|workspace-write|danger-full-access`. `safeSandbox` is **not** a config field in v1 — Codex safe always uses `read-only` because the safe-mode prompt prefix asserts "do not edit." Allowing a writable safe sandbox would put the prompt and the sandbox in disagreement. If a writable safe sandbox is ever needed, it should land as an explicit follow-up that also revises the prompt prefix.
- `codex.ephemeral` and `codex.ignoreUserConfig` booleans.

**Step 4: Update config example**

Add sparse `policy` and `codex` sections to `config.example.json`, keeping existing `tracking`, `cursor`, and `droid`. Do not include false/default policy fields in the example; otherwise a user who copies the example and changes only `policy.profile` to `trusted-hooks` would accidentally pin hook bypass off.

**Step 5: Verify**

Run:

```bash
python3 -m unittest tests.test_delegate_validation
```

Expected: pass.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_validation`
- Secondary command: `python3 -m unittest discover -s tests`

### Task 2: Add Codex CLI parsing and request construction

**Parallel:** no
**Blocked by:** Task 1
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_parser.py`, `tests/test_delegate_commands.py`, `tests/test_snapshot_commands.py`, `examples/task.codex.json`
**Invariants:** Existing command grammar remains backward compatible; global flags must still appear before subcommands. Existing `describe`/`models` JSON payloads stay additive — no key removed, no key renamed.
**Out of scope:** Do not execute real Codex in tests; use fake binaries.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_parser.py` (existing `test_json_describe_shape` at ~L92 needs an additional `self.assertIn("codex", payload["modeMapping"])` assertion plus a policy-metadata assertion; do not remove the existing Cursor assertion)
- Modify: `tests/test_delegate_commands.py` (existing `test_describe_preserves_safe_read_only_modes` at ~L150 needs a parallel `modeMapping.codex.safe` assertion that verifies `--sandbox read-only` and the isolation note; existing `safeNotes`/Cursor/Droid assertions must remain untouched)
- Modify: `tests/test_snapshot_commands.py` (add a Codex snapshot fixture mirroring the existing Cursor one at ~L98, with `"model": null` to exercise the optional-model code path end-to-end)
- Create: `examples/task.codex.json`

**Step 1: Add parser tests**

Add to `tests/test_delegate_parser.py`:

```python
def test_codex_direct_commands_parse(self):
    parsed = self.delegate.parse_cli(["codex", "work", "implement"])
    self.assertEqual(parsed.subcommand, "codex")
    self.assertEqual(parsed.engine, "codex")
    self.assertEqual(parsed.mode, "work")


def test_dry_run_codex_parses(self):
    parsed = self.delegate.parse_cli(["dry-run", "codex", "safe", "review"])
    self.assertEqual(parsed.subcommand, "codex")
    self.assertTrue(parsed.dry_run)
```

Implement `parse_codex` as the Cursor-shaped grammar, not the Droid-shaped grammar:

```text
delegate codex safe <prompt>
delegate codex work <prompt>
delegate dry-run codex safe <prompt>
delegate dry-run codex work <prompt>
```

No positional `MODEL_ALIAS` is accepted for Codex v1. Model selection comes from config or JSON input only.

**Step 2: Add argv tests**

Add to `tests/test_delegate_commands.py`:

```python
def test_codex_work_default_argv_uses_workspace_sandbox_with_network(self):
    policy = self.delegate.delegate_config.effective_policy(
        self.delegate.DEFAULT_CONFIG,
        engine="codex",
        mode="work",
    )
    argv = self.delegate.build_codex_argv(
        self.delegate.DEFAULT_CONFIG["codex"],
        "work",
        "/repo",
        None,
        "hello",
        policy,
        workspace_kind="git",
    )
    self.assertIn("--ask-for-approval", argv)
    self.assertIn("never", argv)
    self.assertIn("--sandbox", argv)
    self.assertIn("workspace-write", argv)
    self.assertIn("-c", argv)
    self.assertIn("sandbox_workspace_write.network_access=true", argv)
    self.assertIn("--json", argv)
    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)


def test_codex_work_trusted_hooks_argv_adds_hook_bypass_only(self):
    config = self.delegate.delegate_config.deep_merge(
        self.delegate.DEFAULT_CONFIG,
        {"policy": {"profile": "trusted-hooks"}},
    )
    policy = self.delegate.delegate_config.effective_policy(
        config,
        engine="codex",
        mode="work",
    )
    argv = self.delegate.build_codex_argv(
        config["codex"],
        "work",
        "/repo",
        None,
        "hello",
        policy,
        workspace_kind="git",
    )
    self.assertIn("--dangerously-bypass-hook-trust", argv)
    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
    self.assertIn("--sandbox", argv)
```

**Step 3: Run targeted tests to verify failure**

Run:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_commands
```

Expected: fails because Codex parser/builder does not exist.

**Step 4: Implement parser and request wiring**

In `src/delegate_agent/cli.py`:
- Add `codex` to `HELP`.
- Add `parse_codex(rest, json_mode, cwd, dry_run, pass_through, completion_report)`.
- Update `parse_cli` for direct `codex`.
- Update `parse_dry_run` accepted engines to `cursor`, `droid`, `codex`.
- Update `request_from_parsed`'s execution-engine guard from `("cursor", "droid")` to `("cursor", "droid", "codex")`.
- Update `VALID_ENGINES = {"cursor", "droid", "codex"}` or equivalent.
- Update `KNOWN_HARNESSES = ("cursor", "droid", "codex")`.
- Update `request_from_input_json` to accept `engine == "codex"`.
- Update `RUN_INPUT_KEYS` semantics so `model` is optional for Codex.
- Update the `Request` dataclass `model` field from `str` to `str | None`. Existing Cursor/Droid requests still pass strings; Codex requests pass `None` when neither JSON input nor `codex.defaultModel` supplies a model, and `build_codex_argv` omits `--model` in that case.
- Update `delegate_agent.runner.RunContext.model` from `str` to `str | None`, and make sure manifest/snapshot JSON writers preserve `null` for Codex runs without a model instead of coercing it to an empty string.

**Audit checklist — every site that reads `request.model` / `ctx.model` must accept `None` without `str()`-coercion to `""` or `"None"`:**

  | File | Line(s) (as of this plan) | Site | Required behavior |
  | --- | --- | --- | --- |
  | `src/delegate_agent/cli.py` | ~1249 | `cursor_safe_isolated_request` re-wraps `Request` passing `request.model` through | Pass `None` through unchanged. |
  | `src/delegate_agent/cli.py` | ~1314 | dry-run JSON payload `{"model": request.model, ...}` | Serialize as JSON `null` (no coercion). |
  | `src/delegate_agent/cli.py` | ~1354 | `RunContext(... model=request.model ...)` construction | Pass `None` through unchanged. |
  | `src/delegate_agent/cli.py` | ~1395 | Isolated-request branch `{"model": isolated_request.model}` | Serialize as JSON `null`. |
  | `src/delegate_agent/runner.py` | ~64 | `RunContext.model: str` dataclass field | Change to `str | None`. |
  | `src/delegate_agent/runner.py` | ~132 | `build_manifest` writes `"model": ctx.model` | Allow `None` → JSON `null`; do not stringify. |
  | `src/delegate_agent/runner.py` | ~192 | snapshot JSON writer `"model": ctx.model` | Allow `None` → JSON `null`. |
  | `src/delegate_agent/runner.py` | ~295 | completion-report metadata `"model": ctx.model` | Allow `None` → JSON `null`. |
  | `tests/test_delegate_validation.py` | ~131, ~153, ~175 | Existing Droid JSON-input fixtures `"model": "minimax"` | No change; Droid still requires `model`. |
  | `tests/test_delegate_execution.py` | ~216 | Existing Droid execution fixture | No change. |
  | `tests/test_snapshot_commands.py` | ~98 | Existing Cursor snapshot fixture with `"model": "composer-2.5"` | No change; add a parallel Codex fixture that exercises `model: null` round-tripping through snapshot output. |

Run `grep -rn "\.model\b\|\"model\":" src/delegate_agent tests/` once at the start of Task 2 implementation; any newly-introduced site since this plan was written must be audited against the same rule before edits land.
- Update `main()`'s retention-pass execution subcommand set to include `"codex"` so tracked Codex runs get the same archive-only retention behavior as Cursor/Droid.
- Update `HELP` and `agent-help` text with `delegate codex safe|work` examples.
- Keep direct CLI model selection out of scope for v1. Codex direct CLI uses config `codex.defaultModel` / `codex.profile`; JSON input may provide optional `"model"`.

JSON input model rules:

| Engine | JSON `"model"` behavior |
| --- | --- |
| `cursor` | Ignore or reject per existing behavior; Cursor continues to use `cursor.defaultModel`. Do not introduce Cursor model override semantics in this feature. |
| `droid` | Required; it is the Droid model alias and must exist in `droid.models`. |
| `codex` | Optional. If omitted or `null`, use `codex.defaultModel`; if that is also `null`, pass `None` and omit `--model`. If a non-empty string is provided, pass it to `build_codex_argv` and emit global `--model <value>`. |

**`profile` is config-only for v1.** JSON input may carry `engine`, `mode`, `cwd`, `prompt`, and `model`. It must **not** accept a top-level `"profile"` key for Codex requests — `RUN_INPUT_KEYS` should reject it with a clear error so users do not assume per-request profile selection works. Codex `--profile` is sourced exclusively from `codex.profile` in config. If per-request profile selection becomes needed, add it as a separate compatibility-preserving change with its own validation, tests, and `describe` metadata.

Add `build_codex_argv`:

```python
def build_codex_argv(
    codex: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    prompt: str,
    policy: JsonObject,
    *,
    workspace_kind: str,
    stream_capture: bool = True,
) -> list[str]:
    binary = str(codex["binary"])
    argv = [binary]
    if policy.get("webSearch") is True:
        argv.append("--search")
    if policy.get("bypassApprovalsAndSandbox") is not True:
        # Always "never" for tracked non-interactive runs; see Design Decisions.
        argv.extend(["--ask-for-approval", "never"])
    if codex.get("profile"):
        argv.extend(["--profile", str(codex["profile"])])
    if model:
        argv.extend(["--model", model])
    argv.append("exec")
    argv.extend(["--cd", workspace])
    if codex.get("ignoreUserConfig") is True:
        argv.append("--ignore-user-config")
    if workspace_kind != "git":
        argv.append("--skip-git-repo-check")
    if policy.get("bypassApprovalsAndSandbox") is True:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = codex["workSandbox"] if mode == MODE_WORK else "read-only"
        argv.extend(["--sandbox", str(sandbox)])
        if mode == MODE_WORK and sandbox == "workspace-write" and policy.get("networkAccess") is True:
            argv.extend(["-c", "sandbox_workspace_write.network_access=true"])
    if policy.get("bypassHookTrust") is True:
        argv.append("--dangerously-bypass-hook-trust")
    if stream_capture:
        argv.extend(["--color", "never", "--json"])
        if codex.get("ephemeral", True) is True:
            argv.append("--ephemeral")
    argv.append(prompt)
    return argv
```

Flag placement rule:
- Place these Codex flags before `exec`: `--search`, `--ask-for-approval`, `--profile`, `--model`.
- Place these exec flags after `exec`: `--cd`, `--skip-git-repo-check`, `--ignore-user-config`, `--sandbox`, `-c sandbox_workspace_write.network_access=true`, `--dangerously-bypass-*`, `--color`, `--json`, `--ephemeral`.
- `--model` and `--profile` are accepted both globally and by `codex exec` in 0.133.0; v1 should place them before `exec` for consistency with the rest of Delegate's command builders.
- Do not pass `--ask-for-approval` when `bypassApprovalsAndSandbox` is true.

In discovery output, update:
- `models_payload()` to include `codex.defaultModel`, `codex.profile`, and `codex.binary`.
- `emit_models()` to print a Codex section in non-JSON mode.
- `describe_payload()` to include `codex` in `engines`, `modeMapping.codex.safe`, `modeMapping.codex.work`, effective policy metadata, supported/unsupported policy fields by harness, and the available policy profiles.
- `emit_describe()` human output to list `cursor, droid, codex`.

Policy support matrix for `describe_payload()`:

| Harness | `networkAccess` | `webSearch` | `bypassApprovalsAndSandbox` | `bypassHookTrust` |
| --- | --- | --- | --- | --- |
| `codex` | Supported for `workspace-write` via `-c sandbox_workspace_write.network_access=true` | Supported via global `--search` | Supported via `--dangerously-bypass-approvals-and-sandbox` | Supported via `--dangerously-bypass-hook-trust` |
| `cursor` | Unsupported/no-op in v1; existing Cursor work semantics stay unchanged | Unsupported/no-op in v1 | Unsupported/no-op in v1; do not invent a Cursor equivalent | Unsupported/no-op in v1 |
| `droid` | Unsupported/no-op in v1; existing Droid semantics stay unchanged | Unsupported/no-op in v1 | Unsupported/no-op in v1; do not invent a Droid equivalent | Unsupported/no-op in v1 |

`--ask-for-approval` is not a policy field. Codex tracked runs always emit `--ask-for-approval never` (or omit it entirely when `bypassApprovalsAndSandbox` is true).

Unsupported/no-op fields must appear as metadata, not as silently applied behavior.

In `build_request`, add Codex handling:
- model = JSON/model override if present, else `codex.defaultModel`, else empty/null.
- call `delegate_config.effective_policy(config, engine="codex", mode=mode)`.
- pass `workspace_kind`.

**Step 5: Add example JSON**

Create `examples/task.codex.json`:

```json
{
  "engine": "codex",
  "mode": "work",
  "cwd": "/path/to/workspace",
  "prompt": "Implement the scoped task. Verify with the named checks. Report changed files."
}
```

Add a JSON-input regression test:

```python
def test_run_input_json_codex_allows_omitted_model(self):
    # write examples/task.codex.json-shaped temp input with engine=codex and no model key
    # parse/run request construction should succeed
    # request.model is None when codex.defaultModel is null
    # request.argv omits --model
```

**Step 6: Verify**

Run:

```bash
python3 -m unittest tests.test_delegate_parser tests.test_delegate_commands
```

Expected: pass.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_parser tests.test_delegate_commands`
- Secondary command: `python3 bin/delegate.py --json dry-run codex work "hello"`

Additional required command tests:

```python
def test_codex_work_web_search_argv_when_enabled(self):
    config = self.delegate.delegate_config.deep_merge(
        self.delegate.DEFAULT_CONFIG,
        {"policy": {"work": {"webSearch": True}}},
    )
    policy = self.delegate.delegate_config.effective_policy(
        config,
        engine="codex",
        mode="work",
    )
    argv = self.delegate.build_codex_argv(
        config["codex"],
        "work",
        "/repo",
        None,
        "hello",
        policy,
        workspace_kind="git",
    )
    self.assertIn("--search", argv[: argv.index("exec")])


def test_codex_default_model_null_omits_model_flag(self):
    policy = self.delegate.delegate_config.effective_policy(
        self.delegate.DEFAULT_CONFIG,
        engine="codex",
        mode="work",
    )
    argv = self.delegate.build_codex_argv(
        self.delegate.DEFAULT_CONFIG["codex"],
        "work",
        "/repo",
        None,
        "hello",
        policy,
        workspace_kind="git",
    )
    self.assertNotIn("--model", argv)


def test_codex_dry_run_model_null_is_allowed(self):
    request = self.delegate.build_request(
        "codex",
        "work",
        None,
        "/repo",
        "hello",
        self.delegate.DEFAULT_CONFIG,
        dry_run=True,
    )
    payload = self.delegate.dry_run_payload(request)
    self.assertIsNone(payload["model"])
    self.assertNotIn("--model", payload["argv"])


```

### Task 3: Add Codex safe isolation and safe-mode prompt prefix

**Parallel:** no
**Blocked by:** Task 2
**Owned files:** `src/delegate_agent/cli.py`, `tests/test_delegate_execution.py`, `README.md`
**Invariants:** Cursor safe isolation behavior remains unchanged; Codex work mode runs in the real workspace.
**Out of scope:** Do not isolate Droid safe in this feature.

**Files:**
- Modify: `src/delegate_agent/cli.py`
- Modify: `tests/test_delegate_execution.py`
- Modify: `README.md`

**Step 1: Add Codex safe prompt prefix**

Add:

```python
CODEX_SAFE_REVIEW_PREFIX = (
    "Delegate Codex safe mode (code review/investigation only): "
    "Do not edit, create, or delete files. "
    "Report findings with file path, line reference, severity, and rationale. "
    "If a write is blocked, do not retry it.\n\n"
)
```

Add:

```python
def prefix_codex_safe_prompt(prompt: str) -> str:
    if CODEX_SAFE_REVIEW_PREFIX in prompt:
        return prompt
    return f"{CODEX_SAFE_REVIEW_PREFIX}{prompt}"
```

**Apply it in `effective_prompt`, not in `build_codex_argv`.** Today `effective_prompt` wraps the user prompt with `prepend_skill_review_instructions(...)` first (the always-on harness scaffolding from commit `3e685cb`) and then optionally appends completion-report instructions. The Codex safe prefix must land **between** the skill-review prefix and the user prompt so that skill-review framing remains the outermost wrapper the agent reads, and so the safe-mode "do not edit" rule sits adjacent to the actual task it constrains.

Updated `effective_prompt` signature and order:

```python
def effective_prompt(
    prompt: str,
    *,
    engine: str,
    mode: str,
    completion_report_mode: str,
) -> str:
    prompt = delegate_runner.prepend_skill_review_instructions(prompt)
    if engine == "codex" and mode == MODE_SAFE:
        # Inject AFTER skill-review so skill-review stays the outermost framing.
        prompt = inject_after_skill_review(prompt, CODEX_SAFE_REVIEW_PREFIX)
    if completion_report_mode == delegate_config.COMPLETION_REPORT_MODE_MARKDOWN:
        return delegate_runner.append_completion_report_instructions(prompt)
    return prompt
```

`inject_after_skill_review` finds the skill-review block's end marker (defined in `delegate_runner`) and inserts the Codex safe prefix right after it. If no marker is present (skill-review somehow disabled), fall back to prepending the prefix and let the existing tests catch the regression.

Final prompt layering for `codex safe`, top-to-bottom:

1. Skill-review instructions (always-on)
2. `CODEX_SAFE_REVIEW_PREFIX`
3. Original user prompt
4. Completion-report instructions (if enabled)

`build_codex_argv` does **not** mutate the prompt; it receives the fully-layered string and passes it through as the final positional arg.

Add the safe-mode argv test in this task, after the prefix exists:

```python
def test_codex_safe_default_argv_uses_read_only_sandbox_without_network_or_bypasses(self):
    policy = self.delegate.delegate_config.effective_policy(
        self.delegate.DEFAULT_CONFIG,
        engine="codex",
        mode="safe",
    )
    argv = self.delegate.build_codex_argv(
        self.delegate.DEFAULT_CONFIG["codex"],
        "safe",
        "/repo",
        None,
        "review only",
        policy,
        workspace_kind="git",
    )
    self.assertIn("--ask-for-approval", argv[: argv.index("exec")])
    self.assertIn("never", argv[: argv.index("exec")])
    self.assertIn("--sandbox", argv)
    self.assertIn("read-only", argv)
    self.assertNotIn("sandbox_workspace_write.network_access=true", argv)
    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
    self.assertNotIn("--dangerously-bypass-hook-trust", argv)
    # build_codex_argv no longer injects the safe prefix itself; that happens
    # in effective_prompt. This argv-only test stops at the structural check;
    # prompt-layer assertions live in test_delegate_execution and the
    # effective_prompt unit tests added in Task 3 Step 1.
```

**Step 2: Encode Codex safe isolation**

Implement Codex safe with the same hard boundary as Cursor safe:
- Git workspace: detached temp worktree + dirty/untracked snapshot.
- Non-Git workspace: temp directory copy.
- Source workspace reported as `cwd`.
- Temp copy reported as `executionCwd`.
- `isolatedWorkspace: true`.

Refactor `cursor_safe_isolated_request` into a generic helper:

```python
def replace_argv_after_flag(argv: list[str], flag: str, value: str) -> list[str]:
    updated = list(argv)
    for index, token in enumerate(updated):
        if token == flag and index + 1 < len(updated):
            updated[index + 1] = value
            return updated
    return updated


def replace_safe_workspace_arg(request: Request, isolated_workspace: str) -> list[str]:
    if request.engine == "cursor":
        return replace_argv_after_flag(request.argv, "--workspace", isolated_workspace)
    if request.engine == "codex":
        return replace_argv_after_flag(request.argv, "--cd", isolated_workspace)
    return list(request.argv)


@contextmanager
def safe_isolated_request(request: Request) -> Iterator[Request]:
    if request.mode != MODE_SAFE or request.engine not in ("cursor", "codex"):
        yield request
        return
    ...
```

Keep Cursor-only `.cursor/cli.json` writing behind `if request.engine == "cursor"`. Rename shared temp directories from `delegate-cursor-safe-*` to `delegate-safe-*` when the isolation helper becomes generic. The old prefix is referenced only in this repo (`src/delegate_agent/cli.py` mkdtemp calls and `tests/test_delegate_execution.py:44` cleanup glob) — no installed runtime, agent prompt, or external monitoring depends on it, so a flat rename is safe. Grep `delegate-cursor-safe` across the whole repo as part of Task 3 Step 2 and confirm all hits move to `delegate-safe` together.

Update safe-isolation test helpers and cleanup assertions:
- Rename helper functions such as `cursor_safe_temp_dirs()` to generic names where appropriate.
- Update globs from `delegate-cursor-safe-*` to `delegate-safe-*`.
- Keep backward-looking cleanup tests focused on the new prefix so leaked Codex safe temp directories fail tests.

Update the `execute_request()` call site to use `safe_isolated_request(request)` instead of `cursor_safe_isolated_request(request)`. The refactor is incomplete until the live execution path uses the generic helper.

Update `dry_run_payload()` so both `cursor safe` and `codex safe` report:

```json
{
  "isolatedWorkspace": true,
  "isolation": "Execution uses a temporary detached git worktree or directory copy; the original workspace is not modified."
}
```

**Step 3: Add Codex safe mutation test**

Adapt the existing Cursor safe fake-agent mutation test with fake `codex`. The fake `codex` should tolerate Delegate's planned argv shape, discover the workspace from `--cd <dir>`, write `mutated-by-codex.txt` into that execution cwd, and emit a small JSONL assistant message so tracked output remains parseable:

```python
def test_codex_safe_git_execution_does_not_mutate_original_workspace(self):
    # fake codex writes mutated-by-codex.txt in its cwd
    # delegate codex safe should complete
    # original repo must not contain mutated-by-codex.txt
    # JSON output reports cwd as the source repo and executionCwd as a different temp copy
    # JSON output reports isolatedWorkspace: true
    # argv seen by fake codex includes --sandbox read-only
    # argv seen by fake codex has --cd set to executionCwd, not the source repo
    # final argv prompt contains BOTH the mandatory skill-review prefix and CODEX_SAFE_REVIEW_PREFIX,
    # and skill-review appears BEFORE CODEX_SAFE_REVIEW_PREFIX in the final string
    # (i.e. final_prompt.index(skill_review_marker) < final_prompt.index(CODEX_SAFE_REVIEW_PREFIX))
```

Also add:

```python
def test_codex_safe_dry_run_reports_isolated_workspace(self):
    # dry-run codex safe returns isolatedWorkspace: true and the same isolation note as cursor safe
```

**Step 4: Verify**

Run:

```bash
python3 -m unittest tests.test_delegate_execution
```

Expected: pass; no temp safe directories remain after completion.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_execution`
- Secondary command: `python3 -m unittest discover -s tests`

### Task 4: Add Codex execution and tracked-run coverage

**Parallel:** no
**Blocked by:** Task 3
**Owned files:** `src/delegate_agent/harness_events.py`, `src/delegate_agent/runner.py`, `tests/test_runner_capture.py`, `tests/test_end_to_end_tracking.py`, `tests/test_delegate_execution.py`
**Invariants:** Existing Cursor/Droid tracked runs, pass-through behavior, completion reports, and retention behavior must not regress.
**Out of scope:** Do not add resume support for Codex exec sessions in v1.

**Files:**
- Modify: `src/delegate_agent/harness_events.py`
- Modify: `src/delegate_agent/runner.py` only if Codex JSONL requires final-message fallback support
- Modify: `tests/test_runner_capture.py`
- Modify: `tests/test_end_to_end_tracking.py`
- Modify: `tests/test_delegate_execution.py`

**Step 1: Add fake Codex binary to execution tests**

In fake-bin helpers, add `codex` script:

```bash
#!/usr/bin/env bash
printf '{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Codex completed"}]}\n'
printf '{"type":"tool_call","tool":"shell","args":{"command":"python3 -m unittest"}}\n'
exit "${FAKE_EXIT:-0}"
```

**Step 2: Add end-to-end tracked Codex test**

Add to `tests/test_end_to_end_tracking.py`:

```python
def test_codex_work_tracked_run_bounded_json(self):
    completed = self.run_cli(["codex", "work", "codex e2e"])
    self.assertEqual(completed.returncode, 0, completed.stderr)
    self.assertIn("delegate run codex completed", completed.stdout)
    self.assertIn("snapshot: delegate snapshot codex", completed.stdout)
    snapshot = self.run_cli(["snapshot", "codex"])
    self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
    self.assertIn("Codex completed", snapshot.stdout)
```

**Step 3: Add missing binary test**

Add to `tests/test_delegate_execution.py`:

```python
def test_codex_missing_binary_exit_3(self):
    request = self.delegate.Request(
        "codex",
        "work",
        "/repo",
        "hello",
        ["delegate-definitely-missing-codex", "exec", "hello"],
        "",
    )
    with self.assertRaises(self.delegate.DelegateError) as ctx:
        self.delegate.ensure_binary(request.argv)
    self.assertEqual(ctx.exception.exit_code, 3)
```

**Step 4: Improve JSONL accumulator only if needed**

If fake/live Codex JSONL emits final assistant text under fields not currently handled, update `_extract_text` in `src/delegate_agent/harness_events.py` to read common Codex content item keys:

```python
elif isinstance(item, dict):
    for key in ("text", "output_text", "content"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
            break
```

If Codex final output uses a non-`message` event type, add a narrow parser branch with fixtures.

**Step 5: Verify**

Run:

```bash
python3 -m unittest tests.test_delegate_execution tests.test_runner_capture tests.test_end_to_end_tracking
```

Expected: pass.

**Verification plan:**
- Primary command: `python3 -m unittest tests.test_delegate_execution tests.test_runner_capture tests.test_end_to_end_tracking`
- Secondary command: `python3 -m unittest discover -s tests`

### Task 5: Document policy controls and Codex harness UX

**Parallel:** no
**Blocked by:** Tasks 1-4
**Owned files:** `README.md`, `docs/development.md`, `docs/live-runtime.md`, `CONTEXT.md`, `AGENTS.md`
**Invariants:** Docs must not imply Delegate mutates `~/.delegate` or installed shims; docs must clearly flag dangerous profiles.
**Out of scope:** Do not document unimplemented app-server, cloud, or resume support.

**Files:**
- Modify: `README.md`
- Modify: `docs/development.md`
- Modify: `docs/live-runtime.md`
- Modify: `CONTEXT.md`
- Modify: `AGENTS.md`

**Step 1: README command table**

Add Codex examples:

```bash
delegate codex safe "Review this workspace. Do not edit files."
delegate codex work "Implement the scoped fix, run the named check, and report changed files."
```

Add safety model row:

| Mode | Intent | Codex flags |
| --- | --- | --- |
| `safe` | Read-only review/analysis | isolated workspace + `codex exec --sandbox read-only --ask-for-approval never` |
| `work` | File-writing execution | real workspace + `codex exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true --ask-for-approval never` |

Add a short `delegate models` paragraph explaining that Codex does not expose a fixed model alias list through Delegate in v1; instead, Delegate reports `codex.defaultModel`, `codex.profile`, and `codex.binary` from config.

**Step 2: Config docs**

Document:
- `policy.profile`.
- `policy.safe` / `policy.work`.
- `policy.harness.<engine>.<mode>` overrides.
- `codex.binary`, `codex.defaultModel`, `codex.profile`, `codex.workSandbox`, `ephemeral`, `ignoreUserConfig`.
- `codex.workSandbox` behavior (safe always uses `read-only` — not configurable):
  - `workspace-write` plus `networkAccess: true` emits `-c sandbox_workspace_write.network_access=true`.
  - `read-only` for work mode is allowed and never emits workspace-write network config.
  - `danger-full-access` as a sandbox setting is distinct from `bypassApprovalsAndSandbox`; document it as advanced/high-risk and do not emit the workspace-write network config for it.

Explicit warning:

```markdown
`external-sandbox` is not a convenience mode. It disables Codex approvals and sandboxing and should only be used when Delegate itself is already running inside a container, VM, disposable worktree, or similarly hardened environment with controlled filesystem, credentials, and egress.
```

**Step 3: CONTEXT glossary**

Add terms:
- Policy Profile
- Effective Policy
- Harness Policy Override
- Dangerous Bypass
- Hook Trust Bypass
- Network Access
- Native Web Search

**Step 4: AGENTS.md operator guidance**

Update this repo's `AGENTS.md` Delegate section so the always-on operator contract matches the implementation:

```markdown
`delegate codex safe` runs Codex in an isolated temporary workspace and passes `codex exec --sandbox read-only --ask-for-approval never`.

`delegate codex work` runs in the real workspace and defaults to `codex exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true --ask-for-approval never`.

Policy profiles can enable Codex hook-trust bypass or external-sandbox bypass, but the default profile keeps sandboxing on. Do not promote repo changes into `~/.delegate` unless explicitly asked.
```

**Step 5: Future enhancements note**

Add a brief non-goal/future note:

```markdown
Codex `--output-last-message` is available in codex-cli 0.133.0 and may be useful for a future completion-extraction improvement. v1 uses JSONL capture to stay aligned with Delegate's existing tracked-run pipeline.
```

**Step 6: Verify docs**

Run:

```bash
rg -n "codex|policy.profile|trusted-hooks|external-sandbox|dangerously" README.md docs CONTEXT.md AGENTS.md config.example.json
```

Expected: references exist and warnings are visible.

**Verification plan:**
- Primary command: `rg -n "codex|policy.profile|trusted-hooks|external-sandbox|dangerously" README.md docs CONTEXT.md AGENTS.md config.example.json`
- Secondary command: `python3 -m unittest discover -s tests`

### Task 6: Final integration verification and live dry-run smoke

**Parallel:** no
**Blocked by:** Tasks 1-5
**Owned files:** none beyond prior tasks
**Invariants:** Do not promote repo changes into `~/.delegate`; do not mutate installed Delegate shims.
**Out of scope:** No real long-running Codex implementation task is required for this plan.

**Files:**
- No new owned files.

**Step 1: Full test suite**

Before implementation or final smoke, confirm the local Codex CLI still matches the planned target:

```bash
codex --version
codex --help
codex exec --help
```

Expected: version is `codex-cli 0.133.0` or the relevant flag assumptions are rechecked and the plan adjusted before implementation.

Run:

```bash
python3 -m unittest discover -s tests
```

Expected: pass.

**Step 2: Repo-local describe smoke**

Run:

```bash
python3 bin/delegate.py --json describe
```

Expected:
- `engines` includes `codex`.
- `modeMapping.codex.safe` and `modeMapping.codex.work` show effective safe/work mappings.
- `policyProfiles` or equivalent metadata lists `safe`, `trusted-hooks`, `external-sandbox`, `custom`.

**Step 3: Repo-local models smoke**

Run:

```bash
python3 bin/delegate.py --json models
```

Expected:
- JSON `ok: true`.
- `codex` section reports `binary`, `defaultModel`, and `profile`.
- Existing Cursor/Droid model payloads are unchanged except for the added Codex section.

**Step 4: Repo-local dry-run smoke**

Run:

```bash
python3 bin/delegate.py --json dry-run codex work "hello"
```

Expected:
- JSON `ok: true`.
- `engine: "codex"`.
- `argv` includes `codex`, `--ask-for-approval`, `never`, `exec`, `--sandbox`, `workspace-write`, `sandbox_workspace_write.network_access=true`, `--json`.
- `argv` does not include full dangerous bypass by default.

**Step 5: Repo-local safe dry-run smoke**

Run:

```bash
python3 bin/delegate.py --json dry-run codex safe "review only"
```

Expected:
- JSON `ok: true`.
- `isolatedWorkspace: true`.
- `argv` includes `exec`, `--cd`, `--sandbox`, `read-only`, `--ask-for-approval`, `never`.
- `argv` does not include `sandbox_workspace_write.network_access=true` or either dangerous bypass flag by default.

**Step 6: Trusted-hooks config smoke**

Create a temporary config outside the repo:

```bash
tmp_config="$(mktemp)"
python3 - <<'PY' "$tmp_config"
import json, sys
path = sys.argv[1]
json.dump({"policy": {"profile": "trusted-hooks"}}, open(path, "w"))
PY
DELEGATE_CONFIG="$tmp_config" python3 bin/delegate.py --json dry-run codex work "hello"
```

Expected:
- `argv` includes `--dangerously-bypass-hook-trust`.
- `argv` does not include `--dangerously-bypass-approvals-and-sandbox`.

**Step 7: External-sandbox config smoke**

Create a temporary config:

```bash
tmp_config="$(mktemp)"
python3 - <<'PY' "$tmp_config"
import json, sys
path = sys.argv[1]
json.dump({"policy": {"profile": "external-sandbox"}}, open(path, "w"))
PY
DELEGATE_CONFIG="$tmp_config" python3 bin/delegate.py --json dry-run codex work "hello"
```

Expected:
- `argv` includes `--dangerously-bypass-hook-trust`.
- `argv` includes `--dangerously-bypass-approvals-and-sandbox`.
- `argv` does not include `--sandbox`.

**Verification plan:**
- Primary command: `python3 -m unittest discover -s tests`
- Secondary commands:
  - `python3 bin/delegate.py --json describe`
  - `python3 bin/delegate.py --json models`
  - `python3 bin/delegate.py --json dry-run codex work "hello"`
  - `python3 bin/delegate.py --json dry-run codex safe "review only"`
  - temp-config dry-runs for `trusted-hooks` and `external-sandbox`

## Acceptance Criteria

- `delegate codex safe ...` and `delegate codex work ...` parse, dry-run, and execute through the repo-local CLI.
- `delegate --json run --input-json examples/task.codex.json` supports `engine: "codex"`.
- Codex tracked runs use bounded Delegate output by default and are inspectable through `delegate snapshot`, `delegate runs`, and `delegate run-output`.
- Codex work mode defaults to workspace-write sandbox with network enabled and no full sandbox bypass.
- `policy.profile: "trusted-hooks"` enables Codex hook trust bypass without disabling sandboxing.
- `policy.profile: "external-sandbox"` enables both dangerous Codex bypass flags and is clearly documented as high-risk.
- Codex safe mode rewrites `--cd` to the isolated execution workspace and reports `isolatedWorkspace: true` in dry-run/tracked metadata.
- `delegate models` and `delegate describe` expose Codex and policy metadata without changing existing Cursor/Droid payload semantics beyond additive fields.
- Existing Cursor/Droid tests and behavior remain green.
- No code path writes to or promotes `~/.delegate` or installed Delegate shims.

## Resolved Implementation Defaults

1. `trusted-hooks` enables hook-trust bypass for Codex **work only**. Safe remains maximally conservative unless a user adds an explicit harness override.
2. `webSearch` defaults to **false** for Codex work. Shell/subprocess network access defaults to **true** for work, and users can enable native Codex web search with `policy.work.webSearch: true`.
3. Codex `safe` uses isolated workspace **and** `--sandbox read-only` for consistent Delegate safe semantics and defense in depth.
4. v1 does **not** add one-off CLI policy override flags; durable config/profile controls are the supported interface.

## Final Review Checklist

- No parser ambiguity with Droid `MODEL_ALIAS`.
- No policy field silently disables existing Cursor safe isolation.
- No full dangerous bypass is enabled by default.
- Codex work network access appears in dry-run argv by default.
- Codex safe isolation rewrites `--cd` to the temporary execution workspace, not the source workspace.
- Hook trust bypass is one-setting configurable.
- Unsupported policy fields are visible in `describe` as unsupported/no-op rather than silently implied.
- All docs distinguish workspace containment, approval policy, network access, hook trust, and full dangerous bypass.
