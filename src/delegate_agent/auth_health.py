"""Per-engine auth health, recorded by `delegate capabilities refresh`.

Some lanes already have a cheap, read-only health command outside Delegate
(`estate-cursor status` says whether Cursor is signed in; `estate-omp usage`
lists each OMP provider's quota windows). Refresh runs the ones configured under
``providerErrors.authProbes`` and records what they say, so `delegate doctor` can
show it before a launch fails.

A probe that is absent, times out, exits non-zero, or prints something this
module does not recognise records ``unknown``. Unknown is never a failure and
never refuses a launch; only a probe that clearly reports a signed-out account or
an exhausted quota window says otherwise. The raw probe output is never stored
(it can carry an account name).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from delegate_agent.json_types import JsonObject

SCHEMA = "delegate.auth-health.v1"
PROBE_TIMEOUT_SECONDS = 15

STATUS_OK = "ok"
STATUS_LOGGED_OUT = "logged_out"
STATUS_LIMIT_REACHED = "limit_reached"
STATUS_UNKNOWN = "unknown"

# Engines whose probe output this module knows how to read.
KNOWN_PROBE_ENGINES = ("cursor", "omp")
DEFAULT_PROBES: dict[str, list[str]] = {
    "cursor": ["estate-cursor", "status"],
    "omp": ["estate-omp", "usage"],
}

_PERCENT_USED = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*used")
_SIGNED_OUT = ("not logged in", "not authenticated", "login required", "logged out")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


DISABLE_ENV = "DELEGATE_AUTH_PROBES"


def probes_disabled() -> bool:
    """``DELEGATE_AUTH_PROBES=off`` (or 0/false/no) skips every probe, for CI and sandboxes."""
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "off", "false", "no")


def state_path() -> Path:
    return Path.home() / ".delegate" / "state" / "auth-health.json"


def probes_from_config(config: Mapping[str, object]) -> dict[str, list[str]]:
    """The configured probe argv per engine; absent configuration means no probes."""
    section = config.get("providerErrors")
    probes = section.get("authProbes") if isinstance(section, dict) else None
    if not isinstance(probes, dict):
        return {}
    result: dict[str, list[str]] = {}
    for engine, argv in probes.items():
        if (
            engine in KNOWN_PROBE_ENGINES
            and isinstance(argv, list)
            and argv
            and all(isinstance(part, str) and part for part in argv)
        ):
            result[engine] = list(argv)
    return result


def classify(engine: str, exit_code: int, text: str) -> str:
    """What a probe's output says about the engine's auth, or ``unknown``."""
    lowered = text.lower()
    if engine == "cursor":
        if any(phrase in lowered for phrase in _SIGNED_OUT):
            return STATUS_LOGGED_OUT
        if exit_code == 0 and "logged in" in lowered:
            return STATUS_OK
        return STATUS_UNKNOWN
    if engine == "omp":
        if exit_code != 0:
            return STATUS_UNKNOWN
        percents = [float(value) for value in _PERCENT_USED.findall(lowered)]
        if not percents:
            return STATUS_UNKNOWN
        return STATUS_LIMIT_REACHED if max(percents) >= 100.0 else STATUS_OK
    return STATUS_UNKNOWN


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def probe_engine(
    engine: str,
    argv: list[str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    now: float | None = None,
) -> JsonObject:
    """Run one probe. Never raises: any trouble records ``unknown`` with a reason."""
    moment = time.time() if now is None else now
    record: JsonObject = {
        "engine": engine,
        "probe": " ".join(argv),
        "checkedAt": _iso(moment),
        "status": STATUS_UNKNOWN,
    }
    executable = which(argv[0])
    if executable is None:
        record["reason"] = "probe_not_installed"
        return record
    try:
        completed = run(
            [executable, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        record["reason"] = "probe_timeout"
        return record
    except (OSError, ValueError):
        record["reason"] = "probe_not_runnable"
        return record
    output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    record["status"] = classify(engine, completed.returncode, output)
    if record["status"] == STATUS_UNKNOWN:
        record["reason"] = "probe_output_unrecognized"
    return record


def refresh(
    config: Mapping[str, object],
    engines: Iterable[str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
) -> dict[str, JsonObject]:
    """Probe the configured engines (all, or the requested subset) and persist the result."""
    if probes_disabled():
        return {}
    wanted = None if engines is None else set(engines)
    probes = {
        engine: argv
        for engine, argv in probes_from_config(config).items()
        if wanted is None or engine in wanted
    }
    if not probes:
        return {}
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {
            engine: pool.submit(probe_engine, engine, argv, which=which, run=run)
            for engine, argv in probes.items()
        }
        results = {engine: future.result() for engine, future in futures.items()}
    _persist(results)
    return results


def _persist(results: Mapping[str, JsonObject]) -> None:
    """Merge into the state file so engines this refresh skipped keep their last reading."""
    merged = load()
    merged.update(results)
    path = state_path()
    temp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA, "engines": merged}, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def load() -> dict[str, JsonObject]:
    """The last recorded reading per engine; empty when there is none or it is unreadable."""
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    engines = raw.get("engines") if isinstance(raw, dict) and raw.get("schema") == SCHEMA else None
    if not isinstance(engines, dict):
        return {}
    return {
        engine: record
        for engine, record in engines.items()
        if isinstance(engine, str) and isinstance(record, dict)
    }


def describe(record: Mapping[str, object]) -> str:
    """One line for text output: `cursor: ok (estate-cursor status, checked ...)`."""
    line = f"{record.get('engine')}: {record.get('status')}"
    detail = [str(part) for part in (record.get("probe"), record.get("checkedAt")) if part]
    if record.get("reason"):
        detail.append(str(record["reason"]))
    return f"{line} ({', '.join(detail)})" if detail else line
