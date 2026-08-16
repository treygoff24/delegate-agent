from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile

from delegate_agent import profiles
from delegate_agent.json_types import JsonObject

CURSOR_STATUS_TIMEOUT_SECONDS = 15


def cursor_status_command(engine: str, config: JsonObject) -> tuple[str, ...] | None:
    if engine != "cursor":
        return None
    cursor = config.get("cursor")
    prefix = cursor.get("argvPrefix") if isinstance(cursor, dict) else None
    if (
        not isinstance(prefix, list)
        or not prefix
        or not all(isinstance(part, str) for part in prefix)
    ):
        return None
    return (*prefix, "status", "--format", "json")


def cursor_account_fingerprint(
    command: tuple[str, ...] | None,
    *,
    env_overrides: dict[str, str] | None = None,
) -> str | None:
    if command is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="delegate-cursor-status-") as directory:
            result = subprocess.run(
                command,
                cwd=directory,
                env=profiles.child_environment(overrides=env_overrides),
                text=True,
                capture_output=True,
                check=False,
                timeout=CURSOR_STATUS_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(result.stdout.encode()) > 64 * 1024:
        return None
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(status, dict) or status.get("status") != "authenticated":
        return None
    if status.get("isAuthenticated") is not True:
        return None
    if status.get("hasAccessToken") is not True or status.get("hasRefreshToken") is not True:
        return None
    user = status.get("userInfo")
    if not isinstance(user, dict):
        return None
    user_id = user.get("userId")
    email = user.get("email")
    if not isinstance(user_id, (int, str)) or isinstance(user_id, bool):
        return None
    if not isinstance(email, str) or not email.strip():
        return None
    identity = json.dumps(
        {"email": email.strip().casefold(), "userId": str(user_id)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"cursor-account-v1\0{identity}".encode()).hexdigest()


def add_account_fingerprint(
    payload: JsonObject,
    *,
    engine: str,
    command: tuple[str, ...] | None,
    env_overrides: dict[str, str] | None,
) -> None:
    if engine != "cursor":
        return
    fingerprint = cursor_account_fingerprint(command, env_overrides=env_overrides)
    if fingerprint is not None:
        payload["accountFingerprint"] = fingerprint
