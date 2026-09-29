"""Known-bad lane markers: remember that a lane failed persistently, for a while.

A lane is one engine on one resolved model/provider under one account. When a
run fails with a *persistent* provider error scoped to the lane (a rejected key,
an exhausted quota, a retired model), the lane is marked "known bad until T".
The next launch on the same lane refuses fast, before spawning anything, and says
why; ``--force-launch`` overrides. Transient failures never write a marker, and a
success clears it.

This generalizes the Codex-only usage-limit memory in ``failover_state``. That
store stays authoritative for the Codex profile-failover swap; this one is the
engine-agnostic refusal and never blocks a Codex run that has an unblocked
fallback profile to fail over to (the runner skips those writes).

State lives under the Delegate home (``~/.delegate/state/lane-health/``), not a
workspace registry, because an account or a model is machine-wide: a bad key in
one repository is a bad key in every repository. Writes are atomic and a corrupt
marker is ignored with a warning, never a crash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from delegate_agent import provider_errors
from delegate_agent.errors import EXIT_LANE_KNOWN_BAD, DelegateError
from delegate_agent.json_types import JsonObject

DEFAULT_KNOWN_BAD_MINUTES = 15
DEFAULT_STAGE_STOP_AFTER = 3
MARKER_VERSION = 1
LANE_KNOWN_BAD_ERROR = "lane_known_bad"
FORCE_LAUNCH_FLAG = "--force-launch"

# Engines whose model ids are "<provider>/<model>".
_PROVIDER_PREFIXED_ENGINES = frozenset({"omp", "pi", "opencode"})
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Policy:
    """Config-resolved behavior for one launch; frozen so a Request can carry it."""

    # Seconds a persistent lane failure keeps the lane refused; 0 disables markers.
    known_bad_seconds: float = DEFAULT_KNOWN_BAD_MINUTES * 60.0
    # One automatic resume after a transient stream drop; False opts out.
    auto_resume: bool = True
    # Workflow stage stops launching after this many same-signature persistent
    # results on one lane; 0 disables.
    stage_stop_after: int = DEFAULT_STAGE_STOP_AFTER
    # --force-launch: launch despite a live marker.
    force_launch: bool = False

    @property
    def markers_enabled(self) -> bool:
        return self.known_bad_seconds > 0


def policy_from_config(config: JsonObject, *, force_launch: bool = False) -> Policy:
    """Read ``providerErrors`` from a validated config; bad values fall back to defaults."""
    section = config.get("providerErrors")
    section = section if isinstance(section, dict) else {}
    minutes = section.get("knownBadLaneMinutes", DEFAULT_KNOWN_BAD_MINUTES)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        minutes = DEFAULT_KNOWN_BAD_MINUTES
    stop_after = section.get("stageStopAfter", DEFAULT_STAGE_STOP_AFTER)
    if isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 0:
        stop_after = DEFAULT_STAGE_STOP_AFTER
    auto_resume = section.get("autoResume", True)
    return Policy(
        known_bad_seconds=float(minutes) * 60.0,
        auto_resume=auto_resume if isinstance(auto_resume, bool) else True,
        stage_stop_after=stop_after,
        force_launch=force_launch,
    )


@dataclass(frozen=True)
class Lane:
    """One engine on one resolved model/provider under one account.

    ``identity`` is the strongest account discriminator known (a Codex auth file
    plus profile overlay, a Claude config dir). It enters the key only as a hash
    and is never stored; ``account`` is the plain label shown to people.
    """

    engine: str
    provider: str | None = None
    model: str | None = None
    account: str | None = None
    identity: str | None = None

    @property
    def key(self) -> str:
        material = "\0".join(
            (
                self.engine,
                self.provider or "",
                self.model or "",
                self.account or "",
                self.identity or "",
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()[:24]

    @property
    def label(self) -> str:
        target = self.model or "default model"
        text = f"{self.engine} {target}"
        if self.account:
            text += f" (account {self.account})"
        return text

    def public(self) -> JsonObject:
        return {
            "key": self.key,
            "engine": self.engine,
            "provider": self.provider,
            "model": self.model,
            "account": self.account,
        }


def derive_lane(
    *,
    engine: str,
    model: str | None,
    auth_profile: str | None = None,
    env: Mapping[str, str] | None = None,
    codex_identity: str | None = None,
) -> Lane:
    """Lane for a launch: engine + provider/model + the account the child will use."""
    env = env or {}
    provider: str | None = None
    if engine in _PROVIDER_PREFIXED_ENGINES and model and "/" in model:
        provider = model.split("/", 1)[0] or None
    identity: str | None = None
    account = auth_profile
    if engine == "codex":
        identity = codex_identity
    elif engine == "claude":
        identity = env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or None
        if account is None and identity:
            account = os.path.basename(identity.rstrip("/")) or None
    return Lane(engine, provider, model, account, identity)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def store_dir() -> Path:
    return Path.home() / ".delegate" / "state" / "lane-health"


def _marker_path(lane: Lane) -> Path:
    return store_dir() / f"{_SAFE_NAME.sub('-', lane.engine)}-{lane.key}.json"


@dataclass(frozen=True)
class Marker:
    lane: JsonObject
    signature: str
    klass: str
    status: int | None
    provider_code: str | None
    message: str
    hint: str
    written_at: float
    expires_at: float
    run_id: str | None = None
    alias: str | None = None

    def seconds_left(self, now: float | None = None) -> int:
        return max(int(self.expires_at - (time.time() if now is None else now)), 0)

    def public(self, now: float | None = None) -> JsonObject:
        return {
            "lane": self.lane,
            "signature": self.signature,
            "class": self.klass,
            "status": self.status,
            "providerCode": self.provider_code,
            "message": self.message,
            "hint": self.hint,
            "writtenAt": _iso(self.written_at),
            "expiresAt": _iso(self.expires_at),
            "secondsLeft": self.seconds_left(now),
            "runId": self.run_id,
            "alias": self.alias,
        }

    def to_json(self) -> JsonObject:
        return {
            "version": MARKER_VERSION,
            "lane": self.lane,
            "signature": self.signature,
            "class": self.klass,
            "status": self.status,
            "providerCode": self.provider_code,
            "message": self.message,
            "hint": self.hint,
            "writtenAt": self.written_at,
            "expiresAt": self.expires_at,
            "runId": self.run_id,
            "alias": self.alias,
        }


def _iso(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return str(int(epoch))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_marker(raw: object) -> Marker | None:
    if not isinstance(raw, dict) or raw.get("version") != MARKER_VERSION:
        return None
    lane = raw.get("lane")
    signature = raw.get("signature")
    expires_at = _number(raw.get("expiresAt"))
    written_at = _number(raw.get("writtenAt"))
    if (
        not isinstance(lane, dict)
        or not isinstance(signature, str)
        or not signature
        or expires_at is None
        or written_at is None
    ):
        return None
    status = raw.get("status")
    return Marker(
        lane=lane,
        signature=signature,
        klass=_optional_str(raw.get("class")) or provider_errors.CLASS_PERSISTENT,
        status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        provider_code=_optional_str(raw.get("providerCode")),
        message=_optional_str(raw.get("message")) or "",
        hint=_optional_str(raw.get("hint")) or "",
        written_at=written_at,
        expires_at=expires_at,
        run_id=_optional_str(raw.get("runId")),
        alias=_optional_str(raw.get("alias")),
    )


def _read_path(path: Path, now: float, warnings: list[str]) -> Marker | None:
    """A live marker at ``path``; expired markers are removed, corrupt ones warned about."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"lane-health: could not read {path.name}: {exc.strerror or exc}")
        return None
    try:
        marker = _parse_marker(json.loads(text))
    except ValueError:
        marker = None
    if marker is None:
        warnings.append(
            f"lane-health: ignoring corrupt marker {path.name} (removed; the lane is treated as healthy)"
        )
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    if marker.expires_at <= now:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return marker


def check(lane: Lane | None, *, now: float | None = None) -> tuple[Marker | None, list[str]]:
    """The live marker for ``lane`` and any warnings about unreadable state."""
    warnings: list[str] = []
    if lane is None:
        return None, warnings
    moment = time.time() if now is None else now
    return _read_path(_marker_path(lane), moment, warnings), warnings


def list_live(*, now: float | None = None) -> tuple[list[Marker], list[str]]:
    """Every live marker on this machine, soonest expiry first."""
    warnings: list[str] = []
    moment = time.time() if now is None else now
    directory = store_dir()
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return [], warnings
    markers = [
        marker for path in paths if (marker := _read_path(path, moment, warnings)) is not None
    ]
    markers.sort(key=lambda marker: marker.expires_at)
    return markers, warnings


def live_public(*, now: float | None = None) -> tuple[list[JsonObject], list[str]]:
    """Live markers as plain JSON for `delegate doctor`, plus store warnings."""
    moment = time.time() if now is None else now
    markers, warnings = list_live(now=moment)
    return [marker.public(moment) for marker in markers], warnings


def describe_public(marker: Mapping[str, object]) -> str:
    """One human line for a public marker view."""
    lane = marker.get("lane")
    lane = lane if isinstance(lane, dict) else {}
    where = f"{lane.get('engine')} {lane.get('model') or 'default model'}"
    if lane.get("account"):
        where += f" (account {lane['account']})"
    status = f" HTTP {marker['status']}" if marker.get("status") is not None else ""
    line = (
        f"{where}: {marker.get('signature')}{status} until {marker.get('expiresAt')} "
        f"({marker.get('secondsLeft')}s left)"
    )
    hint = marker.get("hint")
    return f"{line} - {hint}" if isinstance(hint, str) and hint else line


def _write_atomic(path: Path, payload: JsonObject) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def write(
    lane: Lane,
    record: Mapping[str, object],
    *,
    seconds: float,
    run_id: str | None = None,
    alias: str | None = None,
    now: float | None = None,
) -> Marker | None:
    """Mark ``lane`` known-bad for ``seconds``; returns the marker, or None if unwritable."""
    moment = time.time() if now is None else now
    status = record.get("status")
    marker = Marker(
        lane=lane.public(),
        signature=str(record.get("signature") or provider_errors.UNCLASSIFIED),
        klass=str(record.get("class") or provider_errors.CLASS_PERSISTENT),
        status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        provider_code=_optional_str(record.get("providerCode")),
        message=_optional_str(record.get("message")) or "",
        hint=_optional_str(record.get("hint")) or "",
        written_at=moment,
        expires_at=moment + seconds,
        run_id=run_id,
        alias=alias,
    )
    try:
        _write_atomic(_marker_path(lane), marker.to_json())
    except OSError:
        return None
    return marker


def clear(lane: Lane | None) -> bool:
    """Drop the marker for ``lane``; True when one existed."""
    if lane is None:
        return False
    try:
        _marker_path(lane).unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def earns_marker(record: Mapping[str, object] | None) -> bool:
    """A persistent failure that is the lane's fault, not just this request's."""
    return (
        record is not None
        and record.get("class") == provider_errors.CLASS_PERSISTENT
        and record.get("scope") == provider_errors.SCOPE_LANE
    )


def observe(
    lane: Lane | None,
    policy: Policy,
    *,
    succeeded: bool,
    record: Mapping[str, object] | None,
    run_id: str | None = None,
    alias: str | None = None,
) -> str | None:
    """Update the lane's marker from a finished run.

    Returns ``"written"``, ``"cleared"`` or None. A success clears the marker; a
    persistent lane-scoped failure writes it; everything else (transient,
    request-scoped, unclassified) leaves the lane alone.
    """
    if lane is None:
        return None
    if succeeded:
        return "cleared" if clear(lane) else None
    if not policy.markers_enabled or not earns_marker(record):
        return None
    assert record is not None
    written = write(lane, record, seconds=policy.known_bad_seconds, run_id=run_id, alias=alias)
    return "written" if written is not None else None


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def refusal(marker: Marker, lane: Lane, *, now: float | None = None) -> DelegateError:
    """The fast refusal for a launch on a known-bad lane."""
    left = marker.seconds_left(now)
    minutes, seconds = divmod(left, 60)
    remaining = f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"
    status = f" HTTP {marker.status}" if marker.status is not None else ""
    parts = [
        f"Refusing to launch: lane {lane.label} is marked known-bad until "
        f"{_iso(marker.expires_at)} ({remaining} left) after a persistent "
        f"{marker.signature}{status} failure. No child process was started."
    ]
    if marker.hint:
        parts.append(marker.hint)
    if marker.message:
        parts.append(f'Last provider message: "{marker.message}".')
    parts.append(
        f"Fix the cause and retry, wait for the marker to expire, or override with "
        f"{FORCE_LAUNCH_FLAG} (a success clears the marker)."
    )
    diagnostics: JsonObject = {
        # The closed failureKind a caller branches on (outcome.FAILURE_LANE_KNOWN_BAD).
        "failureKind": LANE_KNOWN_BAD_ERROR,
        "laneKnownBad": True,
        "lane": lane.public(),
        "signature": marker.signature,
        "class": marker.klass,
        "status": marker.status,
        "hint": marker.hint,
        "expiresAt": _iso(marker.expires_at),
        "secondsLeft": left,
        "markedRunId": marker.run_id,
    }
    return DelegateError(
        LANE_KNOWN_BAD_ERROR,
        " ".join(parts),
        EXIT_LANE_KNOWN_BAD,
        diagnostics=diagnostics,
        next_actions=[
            *([marker.hint] if marker.hint else []),
            f"Re-run with {FORCE_LAUNCH_FLAG} to try the lane anyway.",
            "delegate doctor  # lists live known-bad lanes",
        ],
    )
