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
import time
from collections.abc import Mapping
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
ENV_FILE_MAX_BYTES = 256 * 1024

ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Names Delegate itself sets for every tracked worktree child. A caller value
# would either be silently overwritten or would break run isolation, so the
# spec refuses them instead of ranking them.
RESERVED_ENV_PREFIXES = ("DELEGATE_",)
RESERVED_ENV_NAMES = frozenset(
    {"WORKSPACE_ROOT", "TMPDIR", "TMP", "TEMP", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}
)


def _env_error(message: str) -> DelegateError:
    return DelegateError("invalid_workspace_env", message)


def validate_env_name(name: object, *, origin: str) -> str:
    if not isinstance(name, str) or not ENV_NAME_RE.match(name):
        raise _env_error(f"{origin}: {name!r} is not a valid environment variable name.")
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        raise _env_error(
            f"{origin}: {name} is reserved; Delegate sets it for every tracked child "
            "(DELEGATE_*, WORKSPACE_ROOT, TMPDIR/TMP/TEMP, CODEX_HOME, CLAUDE_CONFIG_DIR)."
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
        raise _env_error(f"{origin} expects NAME=VALUE, got {redaction.redact_string(token)!r}.")
    validate_env_name(name, origin=origin)
    return name, validate_env_value(name, value, origin=origin)


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


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_setup(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Path,
    timeout: float | None = None,
) -> SetupResult:
    """Run ``command`` via ``/bin/sh -c`` in ``cwd``; output goes to ``log_path``.

    The process runs in its own session so a timeout terminates everything it
    started. Output is captured to a private log, not echoed.
    """
    fd = run_registry.open_private_file(log_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
    started = time.monotonic()
    timed_out = False
    with os.fdopen(fd, "wb") as log:
        process = subprocess.Popen(  # nosec B603 - fixed /bin/sh argv; command is the caller's.
            ["/bin/sh", "-c", command],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            exit_code = process.returncode if process.returncode is not None else -1
    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        data = log_path.read_bytes()
    except OSError:
        data = b""
    tail = data[-SETUP_TAIL_CHARS * 4 :].decode("utf-8", errors="replace")[-SETUP_TAIL_CHARS:]
    return SetupResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        log_path=log_path,
        output_tail=redaction.redact_string(tail),
    )
