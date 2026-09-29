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
one repository is a bad key in every repository. Writes are atomic (a unique
temp file per write, published by rename); writes, clears, and removals of stale
markers take one store lock; a corrupt marker is ignored with a warning, never a
crash.

Which account a launch uses is part of the lane. Codex and Claude carry their own
account identity; for every engine the credential-bearing environment the child
will see (API keys and tokens, and broker, realm, account, endpoint selectors) is
folded into the key as a salted HMAC. The salt lives beside the store, so neither
the key nor the marker can be used to recover or test a secret. Over-splitting
(a rotated key reads as a new lane) only costs one failed launch to re-learn; the
opposite mistake would refuse a healthy account for a bad one's failure.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from delegate_agent import provider_errors, redaction
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

# Environment names that say which account or gateway a child talks to, beyond the
# secret-looking names redaction already knows (API keys, tokens, credentials).
_ACCOUNT_SELECTOR = re.compile(
    r"BROKER|REALM|ACCOUNT|TENANT|ENDPOINT|BASE[_-]?URL|API[_-]?URL|ORG(?:ANI[SZ]ATION)?[_-]?ID",
    re.IGNORECASE,
)
# Delegate's own per-run variables (mail tokens, run ids) are not an account.
_OWN_ENV_PREFIX = "DELEGATE_"
_CREDENTIAL_TAG_LENGTH = 8
# OpenCode takes provider keys inside OPENCODE_CONFIG_CONTENT. Delegate rewrites
# that variable per run (persona prompts, permissions), so the whole value is not
# an account; only its credential and gateway fields are.
_OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"
_CONFIG_AUTH_HEADER = re.compile(r"AUTHORI[SZ]ATION", re.IGNORECASE)
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01


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
    ``credentials`` is the salted digest of the credential-bearing environment
    (``credential_fingerprint``); it enters the key, and only its first few
    characters are shown so two lanes on one model can be told apart.
    """

    engine: str
    provider: str | None = None
    model: str | None = None
    account: str | None = None
    identity: str | None = None
    credentials: str | None = None

    @property
    def key(self) -> str:
        parts = [
            self.engine,
            self.provider or "",
            self.model or "",
            self.account or "",
            self.identity or "",
        ]
        if self.credentials:
            parts.append(self.credentials)
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]

    @property
    def credential_tag(self) -> str | None:
        return self.credentials[:_CREDENTIAL_TAG_LENGTH] if self.credentials else None

    @property
    def shown_account(self) -> str | None:
        """The account label with addresses masked (a profile may be named by email)."""
        return provider_errors.scrub(self.account) if self.account else None

    @property
    def label(self) -> str:
        target = self.model or "default model"
        text = f"{self.engine} {target}"
        if self.account:
            text += f" (account {self.shown_account})"
        if self.credential_tag:
            text += f" [credential {self.credential_tag}]"
        return text

    def public(self) -> JsonObject:
        return {
            "key": self.key,
            "engine": self.engine,
            "provider": self.provider,
            "model": self.model,
            "account": self.shown_account,
            "credential": self.credential_tag,
        }


def _account_variable(name: str) -> bool:
    """True for an environment name that identifies an account or a gateway."""
    if name.startswith(_OWN_ENV_PREFIX) or name in redaction.SENSITIVE_ENV_KEYS:
        return False
    return redaction.key_looks_secret(name) or _ACCOUNT_SELECTOR.search(name) is not None


def _opencode_config_credentials(raw: str) -> list[tuple[str, str]]:
    """The credential and gateway fields inside an OpenCode config, by JSON path.

    Unparseable content yields nothing: OpenCode cannot read it either, so it
    names no account.
    """
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, dict):
        return []
    found: list[tuple[str, str]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                child = f"{path}.{key}" if path else key
                if isinstance(value, (dict, list)):
                    walk(value, child)
                elif (
                    isinstance(value, str)
                    and value
                    and (
                        redaction.key_looks_secret(key)
                        or _ACCOUNT_SELECTOR.search(key) is not None
                        or _CONFIG_AUTH_HEADER.search(key) is not None
                    )
                ):
                    found.append((f"{_OPENCODE_CONFIG_ENV}:{child}", value))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(loaded, "")
    return found


def _salt_path() -> Path:
    return store_dir().parent / "lane-health.salt"


def _machine_salt() -> bytes:
    """A random per-machine secret that keeps credential digests untestable offline.

    Created once (published by hard link so concurrent launches agree) beside the
    store, mode 0600. When the home cannot hold one the salt is per-process: keys
    stay stable within a run and nothing durable can be written anyway.
    """
    path = _salt_path()
    with contextlib.suppress(OSError):
        existing = path.read_bytes().strip()
        if existing:
            return existing
    fresh = secrets.token_hex(32).encode()
    temp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp.")
        temp = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(fresh)
        with contextlib.suppress(FileExistsError):
            os.link(temp, path)
        return path.read_bytes().strip() or fresh
    except OSError:
        return fresh
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()


def credential_fingerprint(env: Mapping[str, str] | None = None) -> str | None:
    """A short, secret-free digest of the account the child will authenticate as.

    Reads the child's effective environment (this process's, then ``env`` on top,
    as the launch composes it) and digests every credential-bearing variable's
    name and value under the machine salt. None when the environment carries none.
    """
    effective = {**os.environ, **(env or {})}
    pairs = sorted(
        (name, value)
        for name, value in effective.items()
        if isinstance(value, str) and value and _account_variable(name)
    )
    config = effective.get(_OPENCODE_CONFIG_ENV)
    if isinstance(config, str) and config:
        pairs = sorted([*pairs, *_opencode_config_credentials(config)])
    if not pairs:
        return None
    digest = hmac.new(_machine_salt(), digestmod=hashlib.sha256)
    for name, value in pairs:
        digest.update(f"{name}\0{value}\0".encode())
    return digest.hexdigest()[:24]


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
    return Lane(engine, provider, model, account, identity, credential_fingerprint(env))


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


@contextlib.contextmanager
def _store_lock() -> Iterator[None]:
    """Serialize every mutation of the marker store (write, clear, stale removal).

    One advisory ``flock`` on a file beside the store directory (not inside it, so
    the directory holds only markers). ``flock`` excludes separate open file
    descriptions, so it orders threads in one process as well as separate launches.
    Holders keep it for a single small file operation; waiting longer than
    ``_LOCK_TIMEOUT_SECONDS`` raises ``TimeoutError`` (an ``OSError``), which every
    caller already treats as "the store is unavailable".
    """
    lock_path = store_dir().parent / "lane-health.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {lock_path}") from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _load(path: Path) -> Marker | None:
    """The marker file at ``path`` parsed, or None when it is absent or malformed."""
    try:
        return _parse_marker(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _discard(path: Path, now: float) -> None:
    """Remove a corrupt or expired marker, unless a writer replaced it since it was read.

    The caller read the file without the lock, so a fresh marker may have been
    published in between; re-read under the lock and only remove what is still bad.
    """
    with contextlib.suppress(OSError), _store_lock():
        marker = _load(path)
        if marker is None or marker.expires_at <= now:
            path.unlink()


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
        _discard(path, now)
        return None
    if marker.expires_at <= now:
        _discard(path, now)
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
    if lane.get("credential"):
        where += f" [credential {lane['credential']}]"
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
    # A unique name per write (mkstemp opens it 0600 and exclusively): two writers
    # that shared one temp file would truncate each other's bytes and race to rename it.
    fd, name = tempfile.mkstemp(dir=directory, prefix=f"{path.name}.tmp.")
    temp = Path(name)
    try:
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
        # Records arrive scrubbed; the store re-scrubs so no caller can persist an address.
        message=provider_errors.scrub(_optional_str(record.get("message")) or ""),
        hint=_optional_str(record.get("hint")) or "",
        written_at=moment,
        expires_at=moment + seconds,
        run_id=run_id,
        alias=alias,
    )
    try:
        with _store_lock():
            _write_atomic(_marker_path(lane), marker.to_json())
    except OSError:
        return None
    return marker


def clear(lane: Lane | None) -> bool:
    """Drop the marker for ``lane``; True when one existed.

    Takes the store lock so a clear waits for a write in flight instead of
    running before it and being undone by it.
    """
    if lane is None or not store_dir().is_dir():
        return False
    path = _marker_path(lane)
    try:
        with _store_lock():
            path.unlink()
    except FileNotFoundError:
        return False
    except TimeoutError:
        # A stuck lock must not leave a lane that just succeeded refused on its next
        # launch. The unlink is atomic; the only cost of skipping the lock is that a
        # failure being recorded at this very moment could be removed with it.
        try:
            path.unlink()
        except OSError:
            return False
    except OSError:  # an unavailable store
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
