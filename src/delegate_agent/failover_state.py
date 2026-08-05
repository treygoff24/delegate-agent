"""Persistent usage-limit state shared by Delegate and profile launchers."""

from __future__ import annotations

import contextlib
import datetime
import os
import re
import time
from collections.abc import Generator
from pathlib import Path

_VALID_TOOLS = frozenset({"codex", "claude"})
_VALID_PROFILES = frozenset({"work", "personal"})
_DEFAULT_COOLDOWN_SEC = 1800
_LOCK_WAIT_SEC = 5.0
_LOCK_STALE_SEC = 30.0
_CODEX_USAGE_LIMIT_RE = re.compile(r"hit your usage limit", re.IGNORECASE)
_CLAUDE_USAGE_LIMIT_RES = (
    re.compile(r"hit your session limit", re.IGNORECASE),
    re.compile(r"usage limit reached", re.IGNORECASE),
)
_TRANSIENT_GUARD_RE = re.compile(r"not your usage limit", re.IGNORECASE)


def _root() -> Path:
    return Path.home() / ".ai-profiles" / "runtime" / "failover"


def _state_file(tool: str, profile: str) -> Path:
    return _root() / f"{tool}-{profile}.blocked-until"


@contextlib.contextmanager
def _lock(tool: str, profile: str) -> Generator[bool, None, None]:
    lock = _root() / f"{tool}-{profile}.lock"
    deadline = time.monotonic() + _LOCK_WAIT_SEC
    acquired = False
    while not acquired:
        try:
            lock.mkdir()
            acquired = True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_SEC:
                    lock.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                lock.rmdir()


def failover_enabled() -> bool:
    return os.environ.get("AI_FAILOVER", "1") != "0"


def _valid(tool: str, profile: str) -> bool:
    return tool in _VALID_TOOLS and profile in _VALID_PROFILES


def check_blocked(tool: str, profile: str) -> tuple[bool, int | None]:
    if not _valid(tool, profile):
        return False, None
    try:
        _root().mkdir(parents=True, exist_ok=True, mode=0o700)
        state = _state_file(tool, profile)
        with _lock(tool, profile) as acquired:
            if not acquired or not state.exists():
                return False, None
            raw = state.read_text().strip()
            if not raw.isdigit() or int(raw) <= int(time.time()):
                state.unlink(missing_ok=True)
                return False, None
            return True, int(raw)
    except (OSError, ValueError):
        return False, None


def write_block(tool: str, profile: str, reset_epoch: int | None = None) -> None:
    if not _valid(tool, profile):
        return
    try:
        root = _root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        cooldown = os.environ.get("AI_FAILOVER_COOLDOWN", str(_DEFAULT_COOLDOWN_SEC))
        seconds = int(cooldown) if cooldown.isdigit() else _DEFAULT_COOLDOWN_SEC
        proposed = reset_epoch or int(time.time()) + seconds
        state = _state_file(tool, profile)
        with _lock(tool, profile) as acquired:
            if not acquired:
                return
            current = state.read_text().strip() if state.exists() else ""
            expiry = max(proposed, int(current) if current.isdigit() else 0)
            temp = state.with_name(f"{state.name}.tmp.{os.getpid()}")
            temp.write_text(f"{expiry}\n")
            os.chmod(temp, 0o600)
            os.replace(temp, state)
    except (OSError, ValueError):
        pass


def clear_block(tool: str, profile: str) -> None:
    if not _valid(tool, profile):
        return
    try:
        with _lock(tool, profile) as acquired:
            if acquired:
                _state_file(tool, profile).unlink(missing_ok=True)
    except OSError:
        pass


def parse_reset_epoch(text: str) -> int | None:
    import re

    match = re.search(r"try again at (\d{1,2}):(\d{2})\s*(AM|PM)", text, re.IGNORECASE)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    if match.group(3).upper() == "PM" and hour != 12:
        hour += 12
    elif match.group(3).upper() == "AM" and hour == 12:
        hour = 0
    now = datetime.datetime.now()
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += datetime.timedelta(days=1)
    return int(reset.timestamp())


def epoch_to_human(epoch: int) -> str:
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%H:%M")
    except (OSError, OverflowError, ValueError):
        return "?:??"


def classify_codex_usage_limit_narrow(text: str) -> bool:
    return any(
        _CODEX_USAGE_LIMIT_RE.search(line) and not _TRANSIENT_GUARD_RE.search(line)
        for line in text.splitlines()
    )


def classify_claude_usage_limit(text: str) -> bool:
    return any(
        not _TRANSIENT_GUARD_RE.search(line)
        and any(pattern.search(line) for pattern in _CLAUDE_USAGE_LIMIT_RES)
        for line in text.splitlines()
    )
