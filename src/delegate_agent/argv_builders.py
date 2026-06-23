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
from delegate_agent.constants import MODE_SAFE, MODE_WORK, validate_mode
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_instructions import SKILL_REVIEW_PREFIX
from delegate_agent.prompt_transport import (
    CURSOR_PROMPT_REDACTION,
    DROID_PROMPT_FILE_ARG_PLACEHOLDER,
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
    "If a write is blocked, treat that as confirmation of the read-only boundary "
    "and continue with a text-only report.\n\n"
)
_SAFE_REVIEW_LABEL_BY_ENGINE = {
    "cursor": "Delegate review mode",
    "codex": "Delegate Codex safe mode",
    "claude": "Delegate Claude safe mode",
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


def _claude_harness_bypass_enabled(config: JsonObject, mode: str) -> bool:
    """Return true only for explicit Claude-scoped bypass policy.

    Delegate's historical policy profiles were Codex-oriented. Requiring the
    Claude bypass to live under policy.harness.claude.work prevents a global
    external-sandbox profile from silently broadening Claude Code permissions.
    """
    if mode != MODE_WORK:
        return False
    policy = config.get("policy")
    if not isinstance(policy, dict):
        return False
    harnesses = policy.get("harness")
    if not isinstance(harnesses, dict):
        return False
    claude_policy = harnesses.get("claude")
    if not isinstance(claude_policy, dict):
        return False
    work_policy = claude_policy.get(MODE_WORK)
    return isinstance(work_policy, dict) and work_policy.get("bypassApprovalsAndSandbox") is True


def build_cursor_argv(
    prefix: list[str],
    mode: str,
    workspace: str,
    model: str,
    prompt: str,
    *,
    stream_capture: bool = True,
) -> list[str]:
    argv = [*prefix, "--workspace", workspace, "-p", "--trust"]
    if mode == MODE_WORK:
        argv.extend(["--approve-mcps", "--force"])
    elif mode == MODE_SAFE:
        prompt = prefix_cursor_safe_prompt(prompt)
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
) -> list[str]:
    argv = [binary, "exec", "--cwd", workspace]
    if mode == MODE_WORK:
        argv.append("--skip-permissions-unsafe")
    elif mode == MODE_SAFE:
        prompt = prefix_droid_safe_prompt(prompt)
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
) -> list[str]:
    argv = [str(kimi["binary"])]
    if mode == MODE_SAFE:
        prompt = prefix_kimi_safe_prompt(prompt)
    elif mode != MODE_WORK:
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
) -> list[str]:
    argv = [
        str(claude["binary"]),
        "-p",
        "--output-format",
        "stream-json" if stream_capture else "text",
        "--input-format",
        "text",
    ]
    if mode == MODE_SAFE:
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
    else:
        validate_mode(mode)
    if claude.get("noSessionPersistence", True) is True:
        argv.append("--no-session-persistence")
    if claude.get("bare", False) is True:
        argv.append("--bare")
    if model:
        argv.extend(["--model", model])
    if reasoning_effort is not None:
        argv.extend(["--effort", reasoning_effort])
    return argv


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
    prompt_transport: str = PROMPT_TRANSPORT_ARGV,
) -> list[str]:
    binary = str(codex["binary"])
    argv = [binary]
    # Safe mode is read-only by contract: never emit the dangerous bypass flags,
    # even if a policy block somehow carries them. Config validation rejects such
    # configs up front, but enforce the invariant structurally here too.
    elevated = mode == MODE_WORK
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
    argv.append("exec")
    argv.extend(["--cd", workspace])
    if codex.get("ignoreUserConfig") is True:
        argv.append("--ignore-user-config")
    if workspace_kind != "git":
        argv.append("--skip-git-repo-check")
    if bypass_sandbox:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = codex["workSandbox"] if mode == MODE_WORK else "read-only"
        argv.extend(["--sandbox", str(sandbox)])
        if (
            mode == MODE_WORK
            and sandbox == "workspace-write"
            and policy.get("networkAccess") is True
        ):
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
