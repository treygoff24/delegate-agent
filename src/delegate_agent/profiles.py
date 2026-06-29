"""Profile selection, environment injection, and Codex auth helpers."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from delegate_agent import redaction, wsl
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.harness_events import StreamAccumulator
from delegate_agent.json_types import JsonObject

STDERR_TAIL_LIMIT = 8_000

_RATE_LIMIT_PATTERN = re.compile(r"rate limit", re.IGNORECASE)

_USAGE_LIMIT_PATTERNS = (
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"insufficient_quota", re.IGNORECASE),
    re.compile(r"exceeded your current quota", re.IGNORECASE),
    _RATE_LIMIT_PATTERN,
)

_ACCOUNT_QUOTA_CONTEXT = re.compile(
    r"(quota|usage|billing|subscription|account|credit)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProfileResolution:
    name: str | None
    source: str | None
    warnings: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    codex_home: str | None = None
    codex_fallback_home: str | None = None


def empty_profile_resolution() -> ProfileResolution:
    return ProfileResolution(name=None, source=None)


def profiles_section(config: JsonObject) -> JsonObject:
    section = config.get("profiles")
    return section if isinstance(section, dict) else {}


def profile_definitions(config: JsonObject) -> dict[str, JsonObject]:
    definitions = profiles_section(config).get("definitions")
    if not isinstance(definitions, dict):
        return {}
    return {
        name.strip(): entry
        for name, entry in definitions.items()
        if isinstance(name, str) and name.strip() and isinstance(entry, dict)
    }


def _expanded_env_value(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value.strip()))


def _unknown_profile_error(name: str, definitions: Mapping[str, JsonObject]) -> DelegateError:
    defined = sorted(definitions)
    if defined:
        hint = f"Defined profiles: {', '.join(defined)}."
    else:
        hint = "No profiles are defined; add a profiles.definitions entry to config."
    return DelegateError(
        "unknown_profile",
        f"Unknown auth profile: {name}. {hint} Run delegate profiles to inspect the active profile.",
    )


def profile_env(config: JsonObject, name: str) -> dict[str, str]:
    definitions = profile_definitions(config)
    profile = definitions.get(name)
    if profile is None:
        raise _unknown_profile_error(name, definitions)
    env = profile.get("env")
    if not isinstance(env, dict):
        return {}
    return {
        key: _expanded_env_value(value)
        for key, value in env.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def codex_section(config: JsonObject) -> JsonObject:
    section = config.get("codex")
    return section if isinstance(section, dict) else {}


def codex_fallback_profile(config: JsonObject) -> str | None:
    fallback = codex_section(config).get("fallbackProfile")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def _warn_once(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _resolve_default_profile(
    config: JsonObject, definitions: Mapping[str, JsonObject]
) -> str | None:
    default = profiles_section(config).get("default")
    if isinstance(default, str) and default in definitions:
        return default
    return None


def resolve_active_profile(
    config: JsonObject,
    env: Mapping[str, str | None],
    cli_override: str | None = None,
) -> ProfileResolution:
    definitions = profile_definitions(config)
    warnings: list[str] = []
    selected: str | None = None
    source: str | None = None

    if cli_override is not None:
        selected = cli_override.strip()
        if not selected or selected not in definitions:
            raise _unknown_profile_error(cli_override, definitions)
        source = "flag"
    elif not definitions:
        return empty_profile_resolution()
    else:
        detect_from = profiles_section(config).get("detectFrom")
        for var_name in detect_from if isinstance(detect_from, list) else []:
            if not isinstance(var_name, str):
                continue
            raw = env.get(var_name)
            value = raw.strip() if isinstance(raw, str) else ""
            if not value:
                continue
            if value not in definitions:
                safe_value = redaction.redact_mapping_value(var_name, value)
                _warn_once(
                    warnings,
                    f"profile detect var {var_name}={safe_value} is not defined; ignoring it.",
                )
                continue
            if selected is None:
                selected = value
                source = var_name
                continue
            if value != selected:
                _warn_once(
                    warnings,
                    f"profile mismatch ({source}={selected} vs {var_name}={value}); using {selected}.",
                )
        if selected is None:
            selected = _resolve_default_profile(config, definitions)
            if selected is not None:
                source = "default"

    active_env = profile_env(config, selected) if selected is not None else {}
    fallback_profile = codex_fallback_profile(config)
    fallback_home = None
    if fallback_profile is not None:
        fallback_home = profile_env(config, fallback_profile).get("CODEX_HOME")
    return ProfileResolution(
        name=selected,
        source=source,
        warnings=tuple(warnings),
        env=active_env,
        codex_home=active_env.get("CODEX_HOME"),
        codex_fallback_home=fallback_home,
    )


def child_environment(
    base: dict[str, str] | None = None, overrides: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    if base:
        env.update(base)
    if overrides:
        env.update(overrides)
    return env


def _auth_file_readable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def preflight_codex_home(codex_home: str, *, codex: JsonObject) -> None:
    if wsl.should_reject_windows_path(codex_home):
        raise DelegateError("windows_path", wsl.windows_path_message("CODEX_HOME", codex_home))
    home = Path(_expanded_env_value(codex_home))
    if not home.is_absolute():
        raise DelegateError(
            "codex_home_not_absolute",
            f"Codex auth preflight failed: CODEX_HOME must be absolute after expansion, "
            f"got {codex_home!r} -> {home}. A relative path resolves against different cwds "
            "for preflight vs the spawned child and can bill the wrong account.",
        )
    auth_json = home / "auth.json"
    if not _auth_file_readable(auth_json):
        raise DelegateError(
            "codex_auth_unavailable",
            f"Codex auth preflight failed: unreadable auth.json at {auth_json}.",
        )
    profile_overlay = codex.get("profile")
    if isinstance(profile_overlay, str) and profile_overlay.strip():
        config_toml = home / "config.toml"
        if not _auth_file_readable(config_toml):
            raise DelegateError(
                "codex_auth_unavailable",
                f"Codex auth preflight failed: unreadable config.toml at {config_toml}.",
            )


def _request_resolution(request: object) -> ProfileResolution:
    resolution = getattr(request, "profile_resolution", None)
    return resolution if resolution is not None else empty_profile_resolution()


def preflight_codex_request(request: object, codex_config: JsonObject) -> None:
    resolution = _request_resolution(request)
    if resolution.name is not None and resolution.codex_home is None:
        raise DelegateError(
            "profile_missing_codex_home",
            f"Profile {resolution.name!r} is active for a Codex Run but does not define CODEX_HOME. "
            f"Add it under profiles.definitions.{resolution.name}.env.CODEX_HOME.",
        )
    if resolution.codex_home is None:
        return
    preflight_codex_home(resolution.codex_home, codex=codex_config)
    fallback_home = resolution.codex_fallback_home
    if fallback_home is not None and not codex_homes_same_account(
        resolution.codex_home, fallback_home
    ):
        preflight_codex_home(fallback_home, codex=codex_config)


def _codex_auth_file(home: str) -> Path:
    return Path(_expanded_env_value(home), "auth.json")


def codex_homes_same_account(primary_home: str, fallback_home: str) -> bool:
    primary_auth = _codex_auth_file(primary_home)
    fallback_auth = _codex_auth_file(fallback_home)
    try:
        return primary_auth.resolve() == fallback_auth.resolve()
    except (OSError, RuntimeError):
        # Symlink loop or unresolvable path: compare without re-resolving (which would
        # raise again) — fall back to a normalized non-resolving path comparison.
        return os.path.normpath(primary_auth) == os.path.normpath(fallback_auth)


def codex_fallback_env_overrides(resolution: ProfileResolution) -> dict[str, str] | None:
    if resolution.codex_home is None or resolution.codex_fallback_home is None:
        return None
    if codex_homes_same_account(resolution.codex_home, resolution.codex_fallback_home):
        return None
    return {**resolution.env, "CODEX_HOME": resolution.codex_fallback_home}


def classify_codex_usage_limit(stderr_text: str) -> bool:
    if not stderr_text.strip():
        return False
    for pattern in _USAGE_LIMIT_PATTERNS:
        if not pattern.search(stderr_text):
            continue
        if pattern is _RATE_LIMIT_PATTERN and not _ACCOUNT_QUOTA_CONTEXT.search(stderr_text):
            continue
        return True
    return False


def read_bounded_stderr_tail(stderr_log: Path, *, limit: int = STDERR_TAIL_LIMIT) -> str:
    if not stderr_log.exists():
        return ""
    if limit <= 0:
        return ""
    with stderr_log.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(handle.tell() - limit, 0), os.SEEK_SET)
        text = handle.read(limit).decode("utf-8", errors="replace")
    return redaction.redact_string(text)


def accumulator_had_tool_events(accumulator: StreamAccumulator) -> bool:
    return any(event.kind in {"tool.started", "tool.completed"} for event in accumulator.events)


def capture_workspace_porcelain(cwd: str) -> str | None:
    try:
        result = _run_git(
            cwd,
            ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def capture_head_oid(cwd: str) -> str | None:
    try:
        result = _run_git(
            cwd,
            ["rev-parse", "--verify", "HEAD"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


@dataclass(frozen=True)
class WorkspaceBaseline:
    porcelain: str
    head: str


def capture_workspace_baseline(cwd: str) -> WorkspaceBaseline | None:
    porcelain = capture_workspace_porcelain(cwd)
    if porcelain is None:
        return None
    head = capture_head_oid(cwd)
    if head is None:
        return None
    return WorkspaceBaseline(porcelain=porcelain, head=head)


def workspace_is_clean(porcelain: str | None) -> bool:
    if porcelain is None:
        return False
    return porcelain.strip() == ""


def workspace_baseline_unchanged(cwd: str, baseline: WorkspaceBaseline | None) -> bool:
    if baseline is None:
        return False
    current = capture_workspace_baseline(cwd)
    if current is None:
        return False
    return current.porcelain == baseline.porcelain and current.head == baseline.head


def work_mode_safe_for_codex_fallback(cwd: str, baseline: WorkspaceBaseline | None) -> bool:
    if baseline is None:
        return False
    if not workspace_is_clean(baseline.porcelain):
        return False
    return workspace_baseline_unchanged(cwd, baseline)


def codex_auth_fallback_metadata(
    *,
    reason: str,
    primary_auth_profile: str | None,
    fallback_auth_profile: str | None,
    primary_exit_code: int,
    fallback_exit_code: int,
    primary_stderr_tail: str,
) -> JsonObject:
    return {
        "triggered": True,
        "reason": reason,
        "primaryAuthProfile": primary_auth_profile,
        "fallbackProfile": fallback_auth_profile,
        "primaryExitCode": primary_exit_code,
        "fallbackExitCode": fallback_exit_code,
        "primaryStderrTail": primary_stderr_tail,
    }
