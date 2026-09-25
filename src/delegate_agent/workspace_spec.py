"""Workspace spec: the base, environment, and setup step a work lane runs in.

A spec is declared at launch (``--base``/``--env``/``--env-file``/``--setup``,
or the ``base``/``env``/``setup`` input-JSON keys a workflow ``agent()``
sends) and recorded with the run so resume and followup replay it:

- ``base`` names the Git ref a persistent worktree is cut from (default: the
  source checkout's HEAD). Creation-only.
- ``env`` is applied to the child beneath Delegate's own variables. Values are
  stored only in the run directory's private ``workspace-env.json``; manifests,
  envelopes, and help show the keys.
- ``setup`` is a shell command run in the fresh worktree, under the launcher,
  before the child starts. A nonzero exit is a typed ``workspace_setup_failed``
  failure and no child is launched. Creation-only.

Delegate never infers a setup command: callers supply it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess  # nosec B404 - setup runs the caller's own declared command.
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from delegate_agent import private_io, redaction, run_registry
from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject

WORKSPACE_ENV_FILE = "workspace-env.json"
WORKSPACE_ENV_SCHEMA = "delegate.workspace-env.v1"
SETUP_LOG_FILE = "setup.log"
SETUP_FAILED = "workspace_setup_failed"
SETUP_TAIL_CHARS = 2000
# How long the setup process group gets to exit on SIGTERM before escalation to
# SIGKILL, and how often the group is probed in between.
SETUP_KILL_GRACE_SECONDS = 5.0
SETUP_KILL_POLL_SECONDS = 0.05
ENV_FILE_MAX_BYTES = 256 * 1024

ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Names Delegate itself sets for every tracked worktree child. A caller value
# would either be silently overwritten or would break run isolation, so the
# spec refuses them instead of ranking them.
RESERVED_ENV_PREFIXES = ("DELEGATE_",)
# The engine home variables (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
# `KIMI_CODE_HOME`) move a harness's config, credentials, and session store.
RESERVED_ENV_NAMES = frozenset(
    {
        "WORKSPACE_ROOT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "KIMI_CODE_HOME",
    }
)
RESERVED_ENV_LIST = (
    "DELEGATE_*, WORKSPACE_ROOT, TMPDIR/TMP/TEMP, CODEX_HOME, CLAUDE_CONFIG_DIR, KIMI_CODE_HOME"
)


def _env_error(message: str) -> DelegateError:
    return DelegateError("invalid_workspace_env", message)


def validate_env_name(name: object, *, origin: str) -> str:
    if not isinstance(name, str) or not ENV_NAME_RE.match(name):
        # The rejected text is never echoed: an env file line or a --env token
        # that fails this check may be key material (a multi-line quoted value
        # split across lines is exactly that shape), and only names are allowed
        # in human-facing output.
        raise _env_error(
            f"{origin}: the name before '=' is not a valid environment variable name "
            "(letters, digits, and underscores, not starting with a digit)."
        )
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        raise _env_error(
            f"{origin}: {name} is reserved; Delegate sets it for every tracked child "
            f"({RESERVED_ENV_LIST})."
        )
    return name


def validate_env_value(name: str, value: object, *, origin: str) -> str:
    if not isinstance(value, str):
        raise _env_error(f"{origin}: the value of {name} must be a string.")
    if "\x00" in value:
        raise _env_error(f"{origin}: the value of {name} contains a NUL byte.")
    return value


def validate_env(mapping: object, *, origin: str) -> dict[str, str]:
    """Validate a name->value mapping; returns a plain dict copy."""
    if not isinstance(mapping, Mapping):
        raise _env_error(f"{origin} must be an object mapping names to string values.")
    result: dict[str, str] = {}
    for name, value in mapping.items():
        validate_env_name(name, origin=origin)
        result[name] = validate_env_value(name, value, origin=origin)
    return result


def parse_env_assignment(token: str, *, origin: str = "--env") -> tuple[str, str]:
    """Parse ``NAME=VALUE`` (the value may be empty and may contain ``=``)."""
    name, sep, value = token.partition("=")
    if not sep:
        # Never echo the token: it may hold a value fragment (a multi-line
        # quoted value split across an env file's lines) rather than a name.
        raise _env_error(f"{origin} expects NAME=VALUE; this entry has no '='.")
    validate_env_name(name, origin=origin)
    return name, validate_env_value(name, value, origin=origin)


def reject_unclosed_quote(value: str, *, origin: str) -> None:
    """Refuse a value that opens a quote with no matching close on its line.

    Only :func:`read_env_file` applies this. Its reader splits on newlines, so
    it cannot honor a quoted value that runs onto the next line, and keeping
    the stray quote as part of the value instead handed the child a truncated
    secret-shaped string. A ``--env`` token is literal: the shell already
    resolved its quoting, so a quote character inside the value is data.
    """
    stripped = value.strip()
    quote = stripped[:1]
    if quote in {"'", '"'} and stripped.count(quote) < 2:
        raise _env_error(
            f"{origin}: the value opens a quote that is not closed on the same line; "
            "multi-line quoted values are not supported."
        )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_file(path_text: str) -> dict[str, str]:
    """Read a dotenv-style file: ``NAME=VALUE`` lines, ``#`` comments, blanks.

    An optional leading ``export`` and one pair of matching surrounding quotes
    are accepted. No variable interpolation or escape processing is done.
    """
    path = Path(path_text).expanduser()
    origin = f"--env-file {path}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _env_error(f"{origin}: cannot read the file ({exc.strerror or exc}).") from exc
    if len(raw) > ENV_FILE_MAX_BYTES:
        raise _env_error(f"{origin}: file exceeds {ENV_FILE_MAX_BYTES} bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _env_error(f"{origin}: file is not UTF-8.") from exc
    result: dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name, value = parse_env_assignment(stripped, origin=f"{origin} line {number}")
        reject_unclosed_quote(value, origin=f"{origin} line {number}")
        result[name] = _unquote(value.strip())
    return result


def resolve_env(
    assignments: Mapping[str, str] | None, env_files: tuple[str, ...] | list[str] = ()
) -> dict[str, str]:
    """Merge env files in order, then explicit assignments (which win)."""
    merged: dict[str, str] = {}
    for env_file in env_files:
        merged.update(read_env_file(env_file))
    if assignments:
        merged.update(validate_env(assignments, origin="--env"))
    return merged


def mask_recorded_env_values(text: str, env: Mapping[str, str] | None) -> str:
    """Replace every recorded env value in ``text`` with ``***``.

    Setup output is the child-shaped environment's own output and it prints
    expanded values (`set -x`, package-manager traces), so a message that
    carries its tail into ``state.json`` or an error envelope has to mask them
    first. Values shorter than four characters are left alone: masking them
    would shred ordinary output without hiding anything a reader could not
    guess.
    """
    if not text or not env:
        return text
    values = sorted(
        (value for value in env.values() if isinstance(value, str) and len(value) >= 4),
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "***")
    return text


def validate_setup(value: object, *, origin: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DelegateError("invalid_workspace_setup", f"{origin} must be a non-empty command.")
    if "\x00" in value:
        raise DelegateError("invalid_workspace_setup", f"{origin} contains a NUL byte.")
    return value


def validate_base(value: object, *, origin: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("-")
        or "\x00" in value
        or any(ch.isspace() for ch in value)
    ):
        raise DelegateError(
            "invalid_workspace_base",
            f"{origin} must be a Git ref or commit (no whitespace, no leading '-').",
        )
    return value


# Run-directory record of env values. Private (0600) like prompt.txt.


def write_run_env(run_path: Path, env: Mapping[str, str]) -> None:
    payload = {"schema": WORKSPACE_ENV_SCHEMA, "env": dict(sorted(env.items()))}
    run_registry.write_private_text(run_path / WORKSPACE_ENV_FILE, json.dumps(payload) + "\n")


def read_run_env(run_path: Path) -> dict[str, str] | None:
    """Return the env recorded in a run (or workflow) directory, None if none."""
    try:
        text = private_io.read_private_text_bounded(
            run_path / WORKSPACE_ENV_FILE,
            max_bytes=private_io.PRIVATE_RECORD_READ_MAX_BYTES,
        )
    except private_io.BoundedReadError as exc:
        if exc.reason == "not_found":
            return None
        raise DelegateError(
            "invalid_run_record", f"The recorded {WORKSPACE_ENV_FILE} is unreadable."
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DelegateError(
            "invalid_run_record", f"The recorded {WORKSPACE_ENV_FILE} is not JSON."
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != WORKSPACE_ENV_SCHEMA:
        raise DelegateError(
            "invalid_run_record", f"The recorded {WORKSPACE_ENV_FILE} has an unknown schema."
        )
    return validate_env(payload.get("env"), origin=WORKSPACE_ENV_FILE)


def spec_record(
    *,
    base: str | None = None,
    base_oid: str | None = None,
    env: Mapping[str, str] | None = None,
    setup: str | None = None,
) -> JsonObject | None:
    """Manifest-facing record: never includes env values."""
    record: JsonObject = {}
    if base is not None:
        record["base"] = base
        if base_oid is not None:
            record["baseOid"] = base_oid
    if env:
        record["envKeys"] = sorted(env)
    if setup is not None:
        record["setup"] = redaction.redact_string(setup)
    return record or None


@dataclass(frozen=True)
class SetupResult:
    exit_code: int
    duration_ms: int
    timed_out: bool
    log_path: Path
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_json(self) -> JsonObject:
        return {
            "exitCode": self.exit_code,
            "durationMs": self.duration_ms,
            "timedOut": self.timed_out,
            "logPath": str(self.log_path),
        }


def _group_liveness(pgid: int) -> bool | None:
    """Whether a setup group still has members; None when it is not ours.

    ``PermissionError`` means the group exists but is not Delegate's to signal:
    the caller stops instead of escalating, so a group whose id has moved on is
    never SIGKILLed.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return True


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """TERM the setup group, then SIGKILL whatever of it outlives the grace.

    Waiting on the group *leader* is not enough: ``--setup 'npm ci && npm run
    build'`` leads with ``/bin/sh``, which dies at once on TERM while npm keeps
    running and keeps writing into the worktree the launcher is leaving behind.
    Setup owns the whole group, so the grace is measured against the group --
    the same escalation ``delegate cancel`` does.
    """
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + SETUP_KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        # Reap the leader first: an unreaped leader is still a member of the
        # group, so the probe below would wait out the whole grace around a
        # setup that is already gone.
        process.poll()
        if _group_liveness(pgid) is not True:
            break
        time.sleep(SETUP_KILL_POLL_SECONDS)
    if _group_liveness(pgid) is True:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    process.wait()


class SetupInterrupted(Exception):
    """Raised inside :func:`run_setup` when the launcher is terminated mid-setup.

    Converting SIGTERM to an exception for the setup window only is what lets
    the launcher kill the setup process group on its way out; the default
    disposition would end the launcher under the child and orphan it.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(f"workspace setup interrupted by signal {signum}")
        self.signum = signum


@contextlib.contextmanager
def terminate_as_exception() -> Iterator[None]:
    """Turn SIGTERM into :class:`SetupInterrupted` for the enclosed window.

    Only the main thread may install a handler; anywhere else the previous
    behavior (default disposition) is kept rather than failing the launch.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, _frame: object) -> None:
        raise SetupInterrupted(signum)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGTERM, previous)


class _PendingInterrupt:
    """The SIGTERM a deferred window recorded, replayed when the window ends."""

    def __init__(self) -> None:
        self.signum: int | None = None

    def replay(self) -> None:
        signum = self.signum
        if signum is not None:
            self.signum = None
            raise SetupInterrupted(signum)


@contextlib.contextmanager
def _deferred_sigterm(pending: _PendingInterrupt) -> Iterator[None]:
    """Record SIGTERM instead of raising it, for a window that cannot take it.

    :func:`terminate_as_exception` raises from whatever bytecode the signal
    lands on, which is wrong in two windows: the fork inside ``Popen``, before
    ``process`` is bound, where nothing could take the new group down; and the
    ``setupPgid`` clear, where cleanup would swallow the exception and the
    launcher would go on to launch the child. Here the handler only records the
    signum, and :meth:`_PendingInterrupt.replay` raises it as the window ends --
    with the process in hand -- so the interruption still takes setup down.

    SIGTERM is deliberately *not* blocked across these windows:
    ``signal.pthread_sigmask`` is inherited across ``fork`` *and* ``execve``, so
    a setup child started with SIGTERM blocked ignores the TERM that is meant to
    stop it, and every kill of it (cancel's included) would wait out the grace
    before escalating.
    """
    if threading.current_thread() is not threading.main_thread():
        # The handler is only installed on the main thread; anywhere else the
        # previous behavior is kept.
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, _frame: object) -> None:
        pending.signum = signum

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGTERM, previous)
        pending.replay()


def run_setup(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Path,
    timeout: float | None = None,
    publish_pgid: Callable[[int | None], None] | None = None,
    mask_values: Mapping[str, str] | None = None,
) -> SetupResult:
    """Run ``command`` via ``/bin/sh -c`` in ``cwd``; output goes to ``log_path``.

    The process runs in its own session so a timeout terminates everything it
    started. Output is captured to a private log, not echoed.

    ``publish_pgid`` is told the setup process group (its session-leader pid)
    while setup runs and ``None`` when it ends, so ``delegate cancel`` can
    stop a setup that would otherwise run unbounded. That identity must never
    be published as the run's ``pid``/``pgid``: a published pid means "the
    child launched" to the unlaunched-seal logic.

    ``mask_values`` is the run's recorded ``--env`` set: setup output shows
    expanded values, so ``output_tail`` is masked before it is cut to
    :data:`SETUP_TAIL_CHARS` and never carries one.
    """
    fd = run_registry.open_private_file(log_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
    started = time.monotonic()
    timed_out = False
    with os.fdopen(fd, "wb") as log, terminate_as_exception():
        pending = _PendingInterrupt()
        process: subprocess.Popen[bytes] | None = None
        try:
            # The interruption is recorded rather than raised across the fork
            # inside Popen: `process` does not exist yet there, so nothing could
            # take the new group down. It is replayed as the window ends, with
            # the process in hand.
            with _deferred_sigterm(pending):
                process = subprocess.Popen(  # nosec B603 - fixed /bin/sh argv; command is the caller's.
                    ["/bin/sh", "-c", command],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            if publish_pgid is not None:
                # start_new_session makes this pid the setup group's leader.
                publish_pgid(process.pid)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                exit_code = process.returncode if process.returncode is not None else -1
        except BaseException:
            # A terminated or interrupted launcher takes its setup down with
            # it: whatever it started must not outlive the process that owns
            # the worktree lease. Nothing to take down when the fork never
            # happened (Popen's own OSError).
            if process is not None:
                _kill_group(process)
            raise
        finally:
            # The clear waits on the registry lock, so it is the launcher's
            # longest window: the interruption is recorded there too, instead of
            # raising into cleanup that would swallow it and let the child
            # launch, and replayed with the group unrecorded.
            try:
                with _deferred_sigterm(pending):
                    if publish_pgid is not None:
                        publish_pgid(None)
            except SetupInterrupted:
                if process is not None:
                    _kill_group(process)
                raise
            except Exception:
                # Annotating the record must not fail the setup itself.
                pass
    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        data = log_path.read_bytes()
    except OSError:
        data = b""
    # Mask before cutting. A value that straddles the tail boundary would
    # otherwise reach `state.json` and the error envelope as its suffix: the
    # suffix does not match the whole value, so masking the cut tail cannot
    # catch it. Read back far enough for any crossing value to be complete, and
    # 4 bytes per character covers the worst-case UTF-8 encoding of the window.
    overlap = max(
        (len(value) for value in (mask_values or {}).values() if isinstance(value, str)),
        default=0,
    )
    text = data[-(SETUP_TAIL_CHARS + overlap) * 4 :].decode("utf-8", errors="replace")
    tail = mask_recorded_env_values(text, mask_values)[-SETUP_TAIL_CHARS:]
    return SetupResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        log_path=log_path,
        output_tail=redaction.redact_string(tail),
    )
