"""Codex auth profile selection, preflight, and quota-fallback helpers."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from delegate_agent import config as delegate_config
from delegate_agent import redaction
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS
from delegate_agent.git_utils import run_git as _run_git
from delegate_agent.harness_events import StreamAccumulator
from delegate_agent.json_types import JsonObject

DEFAULT_AUTH_PROFILE_NAMES = frozenset({"personal", "work"})

DEFAULT_RUNTIME_CODEX_HOMES: dict[str, str] = {
    "personal": "~/.ai-profiles/runtime/codex/personal",
    "work": "~/.ai-profiles/runtime/codex/work",
}

STDERR_TAIL_LIMIT = 8_000

_RATE_LIMIT_PATTERN = re.compile(r"rate limit", re.IGNORECASE)

_USAGE_LIMIT_PATTERNS = (
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"insufficient_quota", re.IGNORECASE),
    re.compile(r"exceeded your current quota", re.IGNORECASE),
    _RATE_LIMIT_PATTERN,
)

_ACCOUNT_QUOTA_CONTEXT = re.compile(
    r"(quota|usage|billing|subscription|account|credit|limit)",
    re.IGNORECASE,
)


def codex_section(config: JsonObject) -> JsonObject | None:
    section = config.get("codex")
    return section if isinstance(section, dict) else None


def auth_profile_name(codex: JsonObject, key: str) -> str | None:
    value = codex.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DelegateError(
            "invalid_codex_config",
            f"codex.{key} must be a non-empty string or null.",
        )
    return value.strip()


def auth_profiles_map(codex: JsonObject) -> dict[str, JsonObject]:
    profiles = codex.get("authProfiles")
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            "codex.authProfiles must be an object.",
        )
    normalized: dict[str, JsonObject] = {}
    for name, entry in profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise delegate_config.ConfigError(
                "invalid_codex_config",
                "codex.authProfiles keys must be non-empty strings.",
            )
        if not isinstance(entry, dict):
            raise delegate_config.ConfigError(
                "invalid_codex_config",
                f"codex.authProfiles.{name} must be an object.",
            )
        normalized[name.strip()] = entry
    return normalized


def validate_codex_home_path(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            f"{path} must be a non-empty absolute path or start with ~/.",
        )
    expanded = Path(value.strip()).expanduser()
    if not expanded.is_absolute():
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            f"{path} must be a non-empty absolute path or start with ~/.",
        )
    return str(expanded)


def profile_codex_home(entry: JsonObject, *, profile_name: str) -> str:
    raw = entry.get("codexHome")
    return validate_codex_home_path(raw, path=f"codex.authProfiles.{profile_name}.codexHome")


def resolve_profile_codex_home(codex: JsonObject, profile_name: str) -> str:
    profiles = auth_profiles_map(codex)
    entry = profiles.get(profile_name)
    if entry is None:
        raise DelegateError(
            "unknown_auth_profile",
            f"Unknown Codex auth profile: {profile_name}.",
        )
    return profile_codex_home(entry, profile_name=profile_name)


def _config_auth_profile_name(codex: JsonObject, key: str) -> str | None:
    value = codex.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            f"codex.{key} must be a non-empty string or null.",
        )
    return value.strip()


def validate_codex_auth_config(codex: JsonObject) -> None:
    auth_profile = (
        _config_auth_profile_name(codex, "authProfile") if "authProfile" in codex else None
    )
    fallback = (
        _config_auth_profile_name(codex, "fallbackAuthProfile")
        if "fallbackAuthProfile" in codex
        else None
    )
    profiles = auth_profiles_map(codex)
    for name, entry in profiles.items():
        profile_codex_home(entry, profile_name=name)
        unknown = set(entry) - {"codexHome"}
        if unknown:
            raise delegate_config.ConfigError(
                "invalid_codex_config",
                f"codex.authProfiles.{name} has unknown keys: {', '.join(sorted(unknown))}.",
            )
    if auth_profile is not None and auth_profile not in profiles:
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            f"codex.authProfile {auth_profile!r} is not defined in codex.authProfiles.",
        )
    if fallback is not None and fallback not in profiles:
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            f"codex.fallbackAuthProfile {fallback!r} is not defined in codex.authProfiles.",
        )
    if auth_profile is not None and fallback is not None and auth_profile == fallback:
        raise delegate_config.ConfigError(
            "invalid_codex_config",
            "codex.authProfile and codex.fallbackAuthProfile must differ when both are set.",
        )


def _auth_file_readable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def preflight_codex_home(codex_home: str, *, codex: JsonObject) -> None:
    home = Path(codex_home)
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


def preflight_codex_auth(config: JsonObject) -> None:
    codex = codex_section(config)
    if codex is None:
        return
    auth_profile = auth_profile_name(codex, "authProfile") if "authProfile" in codex else None
    if auth_profile is None:
        return
    codex_home = resolve_profile_codex_home(codex, auth_profile)
    preflight_codex_home(codex_home, codex=codex)
    fallback = (
        auth_profile_name(codex, "fallbackAuthProfile") if "fallbackAuthProfile" in codex else None
    )
    if fallback is not None:
        fallback_home = resolve_profile_codex_home(codex, fallback)
        preflight_codex_home(fallback_home, codex=codex)


def resolve_codex_auth_for_request(
    config: JsonObject,
) -> tuple[dict[str, str], str | None, str | None]:
    codex = codex_section(config)
    if codex is None:
        return {}, None, None
    auth_profile = auth_profile_name(codex, "authProfile") if "authProfile" in codex else None
    fallback = (
        auth_profile_name(codex, "fallbackAuthProfile") if "fallbackAuthProfile" in codex else None
    )
    if auth_profile is None:
        return {}, None, fallback
    codex_home = resolve_profile_codex_home(codex, auth_profile)
    return {"CODEX_HOME": codex_home}, auth_profile, fallback


def fallback_env_overrides(config: JsonObject) -> dict[str, str] | None:
    codex = codex_section(config)
    if codex is None:
        return None
    auth_profile = auth_profile_name(codex, "authProfile") if "authProfile" in codex else None
    fallback = (
        auth_profile_name(codex, "fallbackAuthProfile") if "fallbackAuthProfile" in codex else None
    )
    if auth_profile is None or fallback is None:
        return None
    return {"CODEX_HOME": resolve_profile_codex_home(codex, fallback)}


def codex_auth_write_target() -> Path:
    explicit = os.environ.get(delegate_config.CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return delegate_config.default_config_path()


def _git_root_for(path: Path) -> Path | None:
    try:
        result = _run_git(
            str(path.parent if path.is_file() else path),
            ["rev-parse", "--show-toplevel"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def refuse_tracked_config_write(target: Path) -> None:
    resolved = target.expanduser().resolve()
    git_root = _git_root_for(resolved)
    if git_root is None:
        return
    try:
        relative = resolved.relative_to(git_root)
    except ValueError:
        return
    try:
        result = _run_git(
            str(git_root),
            ["ls-files", "--error-unmatch", str(relative)],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return
    if result.returncode == 0:
        raise DelegateError(
            "unsafe_config_target",
            f"Refusing to write Codex auth settings to tracked repo file: {resolved}. "
            "Use ~/.delegate/config.json or an untracked DELEGATE_CONFIG path.",
        )


def bootstrap_default_auth_profiles() -> dict[str, JsonObject]:
    profiles: dict[str, JsonObject] = {}
    for name, home in DEFAULT_RUNTIME_CODEX_HOMES.items():
        expanded = Path(home).expanduser()
        auth_json = expanded / "auth.json"
        config_toml = expanded / "config.toml"
        if _auth_file_readable(auth_json) and _auth_file_readable(config_toml):
            profiles[name] = {"codexHome": str(expanded)}
    return profiles


def effective_auth_profiles_for_use(config: JsonObject) -> dict[str, JsonObject]:
    codex = codex_section(config) or {}
    profiles = auth_profiles_map(codex)
    if profiles:
        return profiles
    return bootstrap_default_auth_profiles()


def read_raw_config_object(path: Path) -> JsonObject:
    if path.exists():
        return delegate_config.read_config_file(path)
    return {}


def write_raw_config_object(path: Path, payload: JsonObject) -> None:
    from delegate_agent.private_io import write_json_atomic

    refuse_tracked_config_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, payload)


def show_payload(config: JsonObject, *, config_source: str) -> JsonObject:
    codex = codex_section(config) or {}
    auth_profile = codex.get("authProfile")
    fallback = codex.get("fallbackAuthProfile")
    codex_home: str | None = None
    if isinstance(auth_profile, str) and auth_profile.strip():
        try:
            codex_home = resolve_profile_codex_home(codex, auth_profile.strip())
        except (DelegateError, delegate_config.ConfigError):
            codex_home = None
    return {
        "ok": True,
        "configSource": config_source,
        "authProfile": auth_profile if isinstance(auth_profile, str) else None,
        "fallbackAuthProfile": fallback if isinstance(fallback, str) else None,
        "codexHome": codex_home,
    }


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
    data = stderr_log.read_bytes()
    if len(data) <= limit:
        text = data.decode("utf-8", errors="replace")
    else:
        text = data[-limit:].decode("utf-8", errors="replace")
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


def child_environment(
    base: dict[str, str] | None = None, overrides: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ)
    if base:
        env.update(base)
    if overrides:
        env.update(overrides)
    return env


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
        "fallbackAuthProfile": fallback_auth_profile,
        "primaryExitCode": primary_exit_code,
        "fallbackExitCode": fallback_exit_code,
        "primaryStderrTail": primary_stderr_tail,
    }
