"""Per-engine command-line construction.

Given a resolved config section, mode, workspace, model, and prompt, build the
exact argv each harness CLI (cursor / droid / kimi / claude / codex) expects.
The builders stay deliberately explicit rather than table-driven: prompt
transport, sandbox flags, MCP handling, and reasoning wiring differ materially
per engine, and the safe-mode flag sets here are security-load-bearing — codex
never emits its bypass flags in safe mode, cursor/droid/kimi/claude apply their
read-only flag sets. Preserve those branches exactly.
"""

from __future__ import annotations

from delegate_agent import reasoning
from delegate_agent.constants import MODE_CALL, MODE_SAFE, MODE_WORK, validate_mode
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_instructions import SKILL_REVIEW_PREFIX
from delegate_agent.prompt_transport import (
    CURSOR_PROMPT_REDACTION,
    DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_FILE_ARG_PLACEHOLDER,
    PROMPT_TRANSPORT_ARGV,
    PROMPT_TRANSPORT_FILE,
    PROMPT_TRANSPORT_STDIN,
)

# Each engine's safe-review prefix is one shared body behind a per-engine label.
# Cursor uses "Delegate review mode"; the rest use "Delegate <Engine> safe mode".
_SAFE_REVIEW_BODY = (
    "(read-only review/investigation): "
    "Inspect, map, and reason about the workspace. "
    "You may propose patches or commands in text, but do not edit, create, delete, "
    "format, commit, or otherwise mutate files or repo state. "
    "If a write is blocked, do not retry or work around it; continue with a "
    "text-only report.\n\n"
)
_SAFE_REVIEW_LABEL_BY_ENGINE = {
    "cursor": "Delegate review mode",
    "codex": "Delegate Codex safe mode",
    "claude": "Delegate Claude safe mode",
    "grok": "Delegate Grok safe mode",
    "devin": "Delegate Devin safe mode",
    "opencode": "Delegate OpenCode safe mode",
    "pi": "Delegate Pi safe mode",
    "droid": "Delegate Droid safe mode",
    "kimi": "Delegate Kimi safe mode",
}
SAFE_REVIEW_PREFIX_BY_ENGINE: dict[str, str] = {
    engine: f"{label} {_SAFE_REVIEW_BODY}" for engine, label in _SAFE_REVIEW_LABEL_BY_ENGINE.items()
}

CLAUDE_SAFE_TOOLS = "Read,Grep,Glob,Bash"

CLAUDE_SAFE_ALLOWED_TOOLS = (
    "Bash(git diff:*),Bash(git status:*),Bash(git show:*),Bash(git log:*),"
    "Bash(rg:*),Bash(grep:*),Bash(ls:*)"
)


def redacted_prompt_argv(argv: list[str], replacement: str = CURSOR_PROMPT_REDACTION) -> list[str]:
    if not argv:
        return []
    redacted = list(argv)
    redacted[-1] = replacement
    return redacted


def _prefix_safe_prompt(prompt: str, engine: str) -> str:
    """Idempotently prepend the engine's safe-review prefix."""
    safe_prefix = SAFE_REVIEW_PREFIX_BY_ENGINE[engine]
    if prompt.startswith(safe_prefix):
        return prompt
    return f"{safe_prefix}{prompt}"


def prefix_cursor_safe_prompt(prompt: str) -> str:
    return _prefix_safe_prompt(prompt, "cursor")


def prefix_kimi_safe_prompt(prompt: str) -> str:
    return _prefix_safe_prompt(prompt, "kimi")


def prefix_droid_safe_prompt(prompt: str) -> str:
    safe_prefix = SAFE_REVIEW_PREFIX_BY_ENGINE["droid"]
    if prompt.startswith(safe_prefix):
        return prompt
    skill_prefix = SKILL_REVIEW_PREFIX
    if prompt.startswith(skill_prefix):
        insert_at = len(skill_prefix)
        if prompt[insert_at:].startswith(safe_prefix):
            return prompt
        return prompt[:insert_at] + safe_prefix + prompt[insert_at:]
    return f"{safe_prefix}{prompt}"


def _harness_bypass_enabled(config: JsonObject, mode: str, engine: str) -> bool:
    """Return true only for explicit harness-scoped bypass policy.

    Delegate's historical policy profiles were Codex-oriented. Requiring the
    bypass to live under policy.harness.<engine>.work prevents a global
    external-sandbox profile from silently broadening native harness permissions.
    """
    if mode != MODE_WORK:
        return False
    policy = config.get("policy")
    if not isinstance(policy, dict):
        return False
    harnesses = policy.get("harness")
    if not isinstance(harnesses, dict):
        return False
    engine_policy = harnesses.get(engine)
    if not isinstance(engine_policy, dict):
        return False
    work_policy = engine_policy.get(MODE_WORK)
    return isinstance(work_policy, dict) and work_policy.get("bypassApprovalsAndSandbox") is True


def _claude_harness_bypass_enabled(config: JsonObject, mode: str) -> bool:
    return _harness_bypass_enabled(config, mode, "claude")


def _grok_harness_bypass_enabled(config: JsonObject, mode: str) -> bool:
    return _harness_bypass_enabled(config, mode, "grok")


def _reject_pure(engine: str, mode: str, pure: bool, *, supported: bool = False) -> None:
    if not pure:
        return
    if mode != MODE_CALL or not supported:
        raise DelegateError("unsupported_pure_call", f"{engine} does not support pure call mode.")


def build_cursor_argv(
    prefix: list[str],
    mode: str,
    workspace: str,
    model: str,
    prompt: str,
    *,
    stream_capture: bool = True,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("cursor", mode, pure)
    argv = [*prefix, "--workspace", workspace, "-p", "--trust"]
    if mode == MODE_WORK:
        argv.extend(["--approve-mcps", "--force"])
    elif mode == MODE_SAFE:
        prompt = prefix_cursor_safe_prompt(prompt)
    elif mode == MODE_CALL:
        # Call defaults to work-level capability ("work minus a repo"); --read-only
        # drops the write flags for the stateless judge/completion contract.
        if not call_read_only:
            argv.extend(["--approve-mcps", "--force"])
    else:
        validate_mode(mode)
    if stream_capture:
        argv.extend(["--model", model, "--print", "--output-format", "stream-json", prompt])
    else:
        argv.extend(["--model", model, "--output-format", "text", prompt])
    return argv


def build_droid_argv(
    binary: str,
    mode: str,
    workspace: str,
    model: str,
    prompt: str,
    *,
    stream_capture: bool = True,
    reasoning_capability: reasoning.ReasoningCapability | None = None,
    prompt_transport: str = PROMPT_TRANSPORT_ARGV,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("droid", mode, pure)
    argv = [binary, "exec", "--cwd", workspace]
    if mode == MODE_WORK:
        argv.append("--skip-permissions-unsafe")
    elif mode == MODE_SAFE:
        prompt = prefix_droid_safe_prompt(prompt)
    elif mode == MODE_CALL:
        if not call_read_only:
            argv.append("--skip-permissions-unsafe")
    else:
        validate_mode(mode)
    if reasoning_capability is not None:
        argv.extend(["--reasoning-effort", reasoning_capability.effort])
    if stream_capture:
        argv.extend(["--model", model, "--output-format", "stream-json"])
    else:
        argv.extend(["--model", model])
    if prompt_transport == PROMPT_TRANSPORT_ARGV:
        argv.append(prompt)
    elif prompt_transport == PROMPT_TRANSPORT_FILE:
        argv.extend(["--file", DROID_PROMPT_FILE_ARG_PLACEHOLDER])
    elif prompt_transport != PROMPT_TRANSPORT_STDIN:
        raise DelegateError(
            "invalid_prompt_transport",
            f"Unsupported Droid prompt transport: {prompt_transport}",
        )
    return argv


def build_kimi_argv(
    kimi: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    prompt: str,
    *,
    stream_capture: bool = True,
    pure: bool = False,
) -> list[str]:
    _reject_pure("kimi", mode, pure)
    argv = [str(kimi["binary"])]
    if mode == MODE_SAFE:
        prompt = prefix_kimi_safe_prompt(prompt)
    elif mode not in (MODE_WORK, MODE_CALL):
        validate_mode(mode)
    if model:
        argv.extend(["--model", model])
    if stream_capture:
        argv.extend(["--output-format", "stream-json"])
    argv.extend(["--prompt", prompt])
    return argv


def build_claude_argv(
    claude: JsonObject,
    mode: str,
    model: str | None,
    policy: JsonObject,
    *,
    stream_capture: bool = True,
    reasoning_effort: str | None = None,
    allow_bypass_permissions: bool = False,
    call_read_only: bool = False,
    pure: bool = False,
    output_schema: str | None = None,
) -> list[str]:
    _reject_pure("claude", mode, pure, supported=True)
    if pure or output_schema is not None:
        output_format = "json"
    elif stream_capture:
        output_format = "stream-json"
    else:
        output_format = "text"
    argv = [
        str(claude["binary"]),
        "-p",
        "--output-format",
        output_format,
        "--input-format",
        "text",
    ]
    if pure:
        argv.extend(
            [
                "--safe-mode",
                "--tools",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ]
        )
    elif mode == MODE_SAFE:
        argv.extend(
            [
                "--permission-mode",
                "plan",
                "--tools",
                CLAUDE_SAFE_TOOLS,
                "--allowedTools",
                CLAUDE_SAFE_ALLOWED_TOOLS,
                "--strict-mcp-config",
            ]
        )
    elif mode == MODE_WORK:
        permission_mode = (
            "bypassPermissions"
            if allow_bypass_permissions and policy.get("bypassApprovalsAndSandbox") is True
            else str(claude.get("workPermissionMode", "auto"))
        )
        argv.extend(["--permission-mode", permission_mode])
    elif mode == MODE_CALL:
        if call_read_only:
            argv.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--tools",
                    CLAUDE_SAFE_TOOLS,
                    "--allowedTools",
                    CLAUDE_SAFE_ALLOWED_TOOLS,
                    "--strict-mcp-config",
                ]
            )
        else:
            argv.extend(["--permission-mode", str(claude.get("workPermissionMode", "auto"))])
    else:
        validate_mode(mode)
    if not pure and claude.get("noSessionPersistence", True) is True:
        argv.append("--no-session-persistence")
    if not pure and claude.get("bare", False) is True:
        argv.append("--bare")
    if output_schema is not None:
        argv.extend(["--json-schema", output_schema])
    if model:
        argv.extend(["--model", model])
    if reasoning_effort is not None:
        argv.extend(["--effort", reasoning_effort])
    return argv


def build_grok_argv(
    grok: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    policy: JsonObject,
    *,
    stream_capture: bool = True,
    reasoning_effort: str | None = None,
    allow_bypass_permissions: bool = False,
    prompt_transport: str = PROMPT_TRANSPORT_FILE,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("grok", mode, pure)
    argv = [str(grok["binary"]), "--cwd", workspace]
    if stream_capture:
        argv.extend(["--output-format", "streaming-json"])
    else:
        argv.extend(["--output-format", "plain"])
    if mode == MODE_SAFE:
        argv.extend(
            [
                "--permission-mode",
                str(grok.get("safePermissionMode", "dontAsk")),
                "--sandbox",
                str(grok.get("safeSandbox", "read-only")),
            ]
        )
    elif mode == MODE_WORK:
        if allow_bypass_permissions and policy.get("bypassApprovalsAndSandbox") is True:
            argv.extend(["--permission-mode", "bypassPermissions", "--always-approve"])
        else:
            argv.extend(["--permission-mode", str(grok.get("workPermissionMode", "auto"))])
        work_sandbox = grok.get("workSandbox")
        if isinstance(work_sandbox, str) and work_sandbox:
            argv.extend(["--sandbox", work_sandbox])
    elif mode == MODE_CALL:
        if call_read_only:
            argv.extend(
                [
                    "--permission-mode",
                    str(grok.get("safePermissionMode", "dontAsk")),
                    "--sandbox",
                    str(grok.get("safeSandbox", "read-only")),
                ]
            )
        else:
            argv.extend(["--permission-mode", str(grok.get("workPermissionMode", "auto"))])
            work_sandbox = grok.get("workSandbox")
            if isinstance(work_sandbox, str) and work_sandbox:
                argv.extend(["--sandbox", work_sandbox])
    else:
        validate_mode(mode)
    if policy.get("webSearch") is not True and grok.get("disableWebSearch", True) is True:
        argv.append("--disable-web-search")
    if grok.get("noSubagents", False) is True:
        argv.append("--no-subagents")
    if model:
        argv.extend(["--model", model])
    if reasoning_effort is not None:
        argv.extend(["--effort", reasoning_effort])
    if prompt_transport != PROMPT_TRANSPORT_FILE:
        raise DelegateError(
            "invalid_prompt_transport",
            f"Unsupported Grok prompt transport: {prompt_transport}",
        )
    argv.extend(["--prompt-file", PROMPT_FILE_ARG_PLACEHOLDER])
    return argv


def build_devin_argv(
    devin: JsonObject,
    mode: str,
    model: str | None,
    *,
    prompt_transport: str = PROMPT_TRANSPORT_FILE,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("devin", mode, pure)
    if mode == MODE_SAFE:
        raise DelegateError(
            "unsupported_mode",
            "Devin safe mode is unsupported: Devin may perform read-only filesystem "
            "surveys through the generic exec tool, which Delegate cannot allow without "
            "weakening the read-only boundary. Use another harness in safe mode for "
            "filesystem review.",
        )
    if mode not in (MODE_WORK, MODE_CALL):
        validate_mode(mode)
    argv = [str(devin["binary"])]
    if model:
        argv.extend(["--model", model])
    read_only = mode == MODE_CALL and call_read_only
    if read_only:
        argv.extend(
            [
                "--agent-config",
                DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER,
                "--permission-mode",
                "auto",
            ]
        )
    else:
        argv.extend(["--permission-mode", "dangerous"])
    if prompt_transport != PROMPT_TRANSPORT_FILE:
        raise DelegateError(
            "invalid_prompt_transport",
            f"Unsupported Devin prompt transport: {prompt_transport}",
        )
    argv.extend(["--prompt-file", PROMPT_FILE_ARG_PLACEHOLDER, "-p"])
    return argv


def build_opencode_argv(
    opencode: JsonObject,
    mode: str,
    workspace: str,
    model: str | None,
    agent: str | None,
    variant: str | None,
    *,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("opencode", mode, pure)
    read_only = mode == MODE_SAFE or (mode == MODE_CALL and (call_read_only or pure))
    argv = [str(opencode["binary"])]
    if read_only:
        argv.append("--pure")
    argv.extend(["run", "--format", "json", "--print-logs", "--dir", workspace])
    if model:
        argv.extend(["--model", model])
    if agent:
        argv.extend(["--agent", agent])
    if variant:
        argv.extend(["--variant", variant])
    if mode == MODE_WORK:
        argv.append("--auto")
    elif mode not in (MODE_SAFE, MODE_CALL):
        validate_mode(mode)
    return argv


def _build_pi_family_argv(
    section: JsonObject,
    engine: str,
    mode: str,
    model: str | None,
    thinking: str | None,
    *,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure(engine, mode, pure)
    if mode not in (MODE_SAFE, MODE_WORK, MODE_CALL):
        validate_mode(mode)
    argv = [str(section["binary"]), "-p", "--no-session", "--mode", "json"]
    if mode == MODE_SAFE or (mode == MODE_CALL and call_read_only):
        argv.extend(
            [
                "--tools",
                "read",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-approve",
            ]
        )
    if model:
        argv.extend(["--model", model])
    if thinking:
        argv.extend(["--thinking", thinking])
    return argv


def build_pi_argv(
    pi: JsonObject,
    mode: str,
    model: str | None,
    thinking: str | None,
    *,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    return _build_pi_family_argv(
        pi,
        "pi",
        mode,
        model,
        thinking,
        call_read_only=call_read_only,
        pure=pure,
    )


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
    reasoning_capability: reasoning.ReasoningCapability | None = None,
    fast: bool | None = None,
    prompt_transport: str = PROMPT_TRANSPORT_ARGV,
    output_schema: str | None = None,
    call_read_only: bool = False,
    pure: bool = False,
) -> list[str]:
    _reject_pure("codex", mode, pure)
    binary = str(codex["binary"])
    if pure:
        argv = [binary]
        if model:
            argv.extend(["--model", model])
        if reasoning_capability is not None:
            argv.extend(["-c", f'model_reasoning_effort="{reasoning_capability.effort}"'])
        if fast is not None:
            service_tier = "fast" if fast else "default"
            argv.extend(["-c", f'service_tier="{service_tier}"'])
            if fast:
                argv.extend(["-c", "features.fast_mode=true"])
        argv.extend(
            [
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
            ]
        )
        if output_schema is not None:
            argv.extend(["--output-schema", output_schema])
        if stream_capture:
            argv.extend(["--color", "never", "--json"])
            if codex.get("ephemeral", True) is True:
                argv.append("--ephemeral")
        argv.append("-")
        return argv
    argv = [binary]
    # Safe mode is read-only by contract: never emit the dangerous bypass flags,
    # even if a policy block somehow carries them. Config validation rejects such
    # configs up front, but enforce the invariant structurally here too.
    # Work-level call ("work minus a repo") gets the workSandbox but never the
    # bypass flags — those stay bound to real work mode.
    elevated = mode == MODE_WORK
    call_write = mode == MODE_CALL and not call_read_only
    write_sandbox = mode == MODE_WORK or call_write
    bypass_sandbox = elevated and policy.get("bypassApprovalsAndSandbox") is True
    bypass_hook_trust = elevated and policy.get("bypassHookTrust") is True
    if policy.get("webSearch") is True:
        argv.append("--search")
    if not bypass_sandbox:
        argv.extend(["--ask-for-approval", "never"])
    if codex.get("profile"):
        argv.extend(["--profile", str(codex["profile"])])
    if model:
        argv.extend(["--model", model])
    if reasoning_capability is not None:
        argv.extend(
            [
                "-c",
                f'model_reasoning_effort="{reasoning_capability.effort}"',
            ]
        )
    if fast is not None:
        service_tier = "fast" if fast else "default"
        argv.extend(["-c", f'service_tier="{service_tier}"'])
        if fast:
            # Codex drops a "fast" tier silently when features.fast_mode is off
            # in the ambient config; enable it so --fast cannot no-op.
            argv.extend(["-c", "features.fast_mode=true"])
    argv.append("exec")
    argv.extend(["--cd", workspace])
    if output_schema is not None:
        argv.extend(["--output-schema", output_schema])
    if codex.get("ignoreUserConfig") is True:
        argv.append("--ignore-user-config")
    if workspace_kind != "git":
        argv.append("--skip-git-repo-check")
    if bypass_sandbox:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = codex["workSandbox"] if write_sandbox else "read-only"
        argv.extend(["--sandbox", str(sandbox)])
        if write_sandbox and sandbox == "workspace-write" and policy.get("networkAccess") is True:
            argv.extend(["-c", "sandbox_workspace_write.network_access=true"])
    if bypass_hook_trust:
        argv.append("--dangerously-bypass-hook-trust")
    if stream_capture:
        argv.extend(["--color", "never", "--json"])
        if codex.get("ephemeral", True) is True:
            argv.append("--ephemeral")
    if prompt_transport == PROMPT_TRANSPORT_ARGV:
        argv.append(prompt)
    elif prompt_transport == PROMPT_TRANSPORT_STDIN:
        argv.append("-")
    else:
        raise DelegateError(
            "invalid_prompt_transport",
            f"Unsupported Codex prompt transport: {prompt_transport}",
        )
    return argv
