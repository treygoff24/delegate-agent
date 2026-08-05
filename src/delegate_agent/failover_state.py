"""Persistent usage-limit state shared by Delegate and profile launchers."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import re
import time
from collections.abc import Generator
from pathlib import Path

_VALID_TOOLS = frozenset({"codex"})
_LEGACY_PROFILES = frozenset({"work", "personal"})
_DEFAULT_COOLDOWN_SEC = 1800
_LOCK_WAIT_SEC = 5.0
_LOCK_STALE_SEC = 30.0


def _root() -> Path:
    return Path.home() / ".ai-profiles" / "runtime" / "failover"


def _state_slug(tool: str, identity: str) -> str:
    digest = hashlib.sha256(f"{tool}\0{identity}".encode()).hexdigest()
    return digest[:32]


def _state_file(tool: str, identity: str) -> Path:
    return _root() / f"{tool}-{_state_slug(tool, identity)}.blocked-until"


@contextlib.contextmanager
def _lock_path(lock: Path) -> Generator[bool, None, None]:
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


def _lock(tool: str, identity: str) -> Generator[bool, None, None]:
    return _lock_path(_root() / f"{tool}-{_state_slug(tool, identity)}.lock")


def _valid(tool: str, identity: str | None) -> bool:
    return tool in _VALID_TOOLS and isinstance(identity, str) and bool(identity)


def _legacy_identity(profile_alias: str) -> str:
    homes = {
        "work": Path.home() / ".codex",
        "personal": Path.home() / ".ai-profiles" / "runtime" / "codex" / "personal",
    }
    return f"auth={(homes[profile_alias] / 'auth.json').resolve(strict=False)}\0profile="


def _legacy_profile(tool: str, identity: str, profile_alias: str | None) -> str | None:
    if tool != "codex" or profile_alias not in _LEGACY_PROFILES:
        return None
    if identity != _legacy_identity(profile_alias):
        return None
    return profile_alias


def _legacy_state_file(tool: str, profile_alias: str) -> Path:
    return _root() / f"{tool}-{profile_alias}.blocked-until"


def _legacy_lock(tool: str, profile_alias: str) -> Generator[bool, None, None]:
    return _lock_path(_root() / f"{tool}-{profile_alias}.lock")


def _read_block(state: Path, lock: Generator[bool, None, None]) -> int | None:
    with lock as acquired:
        if not acquired or not state.exists():
            return None
        raw = state.read_text().strip()
        if not raw.isdigit() or int(raw) <= int(time.time()):
            state.unlink(missing_ok=True)
            return None
        return int(raw)


def _write_state(state: Path, lock: Generator[bool, None, None], proposed: int) -> None:
    with lock as acquired:
        if not acquired:
            return
        current = state.read_text().strip() if state.exists() else ""
        expiry = max(proposed, int(current) if current.isdigit() else 0)
        temp = state.with_name(f"{state.name}.tmp.{os.getpid()}")
        temp.write_text(f"{expiry}\n")
        os.chmod(temp, 0o600)
        os.replace(temp, state)


def check_blocked(
    tool: str, identity: str | None, *, profile_alias: str | None = None
) -> tuple[bool, int | None]:
    if not _valid(tool, identity):
        return False, None
    assert identity is not None
    try:
        _root().mkdir(parents=True, exist_ok=True, mode=0o700)
        expiries = [_read_block(_state_file(tool, identity), _lock(tool, identity))]
        legacy = _legacy_profile(tool, identity, profile_alias)
        if legacy is not None:
            expiries.append(
                _read_block(_legacy_state_file(tool, legacy), _legacy_lock(tool, legacy))
            )
        active = [expiry for expiry in expiries if expiry is not None]
        if not active:
            return False, None
        return True, max(active)
    except (OSError, ValueError):
        return False, None


def write_block(
    tool: str,
    identity: str | None,
    reset_epoch: int | None = None,
    *,
    profile_alias: str | None = None,
) -> None:
    if not _valid(tool, identity):
        return
    assert identity is not None
    try:
        root = _root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        proposed = reset_epoch or int(time.time()) + _DEFAULT_COOLDOWN_SEC
        _write_state(_state_file(tool, identity), _lock(tool, identity), proposed)
        legacy = _legacy_profile(tool, identity, profile_alias)
        if legacy is not None:
            _write_state(_legacy_state_file(tool, legacy), _legacy_lock(tool, legacy), proposed)
    except (OSError, ValueError):
        pass


def clear_block(tool: str, identity: str | None, *, profile_alias: str | None = None) -> None:
    if not _valid(tool, identity):
        return
    assert identity is not None
    try:
        with _lock(tool, identity) as acquired:
            if acquired:
                _state_file(tool, identity).unlink(missing_ok=True)
        legacy = _legacy_profile(tool, identity, profile_alias)
        if legacy is not None:
            with _legacy_lock(tool, legacy) as acquired:
                if acquired:
                    _legacy_state_file(tool, legacy).unlink(missing_ok=True)
    except OSError:
        pass


def parse_reset_epoch(text: str) -> int | None:
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
