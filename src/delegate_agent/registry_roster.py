"""Roster of the workspace registries this user has launched runs in.

Runs and workflows are recorded per workspace under ``<workspace>/.delegate``.
Run ids and workflow ids are globally unique, but a handle only resolves inside
its own registry, so an agent in the wrong directory hits a dead end. The
roster is a small bounded list under the delegate state home (``~/.delegate``)
that lets a lookup which missed in the current registry find the workspace that
holds it.

The roster is advisory. Appending is best effort and never raises, reads
tolerate a missing, corrupt, or stale file, and entries whose registry
directory is gone are skipped on read and dropped on the next write. Nothing
here mutates a registry; lookups only read another registry's ``index.json``.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from delegate_agent import private_io
from delegate_agent.json_types import JsonObject

ROSTER_FILE_NAME = "registries.json"
ROSTER_LOCK_NAME = "registries.lock"
ROSTER_SCHEMA = "delegate.registry-roster.v1"
# Upper bound on remembered workspaces; the least recently used are dropped.
ROSTER_LIMIT = 256
# A workspace already on the roster is only re-stamped once it is this old, so
# a launch does not pay a fsynced write every time.
ROSTER_RESTAMP_SECONDS = 24 * 60 * 60
ROSTER_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.02
_DELEGATE_DIR_NAME = ".delegate"
_INDEX_FILE_NAME = "index.json"
_WORKFLOWS_DIR_NAME = "workflows"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class RosterMatch:
    """One other registry that holds the looked-up id or alias."""

    workspace: Path
    run_id: str | None = None
    alias: str | None = None
    workflow_id: str | None = None

    @property
    def registry_root(self) -> Path:
        return self.workspace / _DELEGATE_DIR_NAME


def roster_path() -> Path:
    return Path.home() / _DELEGATE_DIR_NAME / ROSTER_FILE_NAME


def _lock_path() -> Path:
    return Path.home() / _DELEGATE_DIR_NAME / ROSTER_LOCK_NAME


def _normalize(workspace: Path | str) -> Path:
    return Path(os.path.realpath(os.fspath(workspace)))


def _has_registry(workspace: Path) -> bool:
    return (workspace / _DELEGATE_DIR_NAME).is_dir()


def _read_entries() -> list[JsonObject]:
    payload = private_io.read_json_object_or_none(roster_path())
    if payload is None:
        return []
    entries = payload.get("workspaces")
    if not isinstance(entries, list):
        return []
    valid: list[JsonObject] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]:
            valid.append(entry)
    return valid


def _parse_stamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


class _RosterBusy(Exception):
    pass


def _locked_write(mutate) -> None:
    """Run ``mutate`` on the entry list under the roster lock and write it back."""
    fd = private_io.open_private_file(_lock_path(), os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + ROSTER_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _RosterBusy from None
                time.sleep(_LOCK_POLL_SECONDS)
        entries = mutate(_read_entries())
        if entries is None:
            return
        private_io.write_json_atomic(
            roster_path(), {"schema": ROSTER_SCHEMA, "workspaces": entries}
        )
    finally:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def note_workspace(workspace: Path | str, *, now: float | None = None) -> None:
    """Remember a workspace whose registry was created or used for a launch.

    Best effort: a read-only home, a busy lock, or a corrupt roster leaves the
    roster as it was and never fails the caller.
    """
    try:
        target = os.fspath(_normalize(workspace))
        moment = time.time() if now is None else now
        stamp = datetime.fromtimestamp(moment, UTC).strftime(_TIME_FORMAT)

        def already_fresh() -> bool:
            for entry in _read_entries():
                if entry["path"] == target:
                    seen = _parse_stamp(entry.get("lastUsedAt"))
                    return seen is not None and moment - seen < ROSTER_RESTAMP_SECONDS
            return False

        if already_fresh():
            return

        def mutate(entries: list[JsonObject]) -> list[JsonObject]:
            kept = [
                entry
                for entry in entries
                if entry["path"] != target and _has_registry(Path(entry["path"]))
            ]
            kept.insert(0, {"path": target, "lastUsedAt": stamp})
            return kept[:ROSTER_LIMIT]

        _locked_write(mutate)
    except (OSError, ValueError, _RosterBusy):
        return


def known_workspaces() -> list[Path]:
    """Workspaces on the roster whose registry still exists, most recent first."""
    try:
        entries = _read_entries()
    except (OSError, ValueError):
        return []
    workspaces: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        workspace = Path(entry["path"])
        if workspace in seen or not _has_registry(workspace):
            continue
        seen.add(workspace)
        workspaces.append(workspace)
        if len(workspaces) >= ROSTER_LIMIT:
            break
    return workspaces


def _other_workspaces(exclude: Path | str | None) -> list[Path]:
    skip = _normalize(exclude) if exclude is not None else None
    return [workspace for workspace in known_workspaces() if workspace != skip]


def _read_index(workspace: Path) -> JsonObject:
    payload = private_io.read_json_object_or_none(workspace / _DELEGATE_DIR_NAME / _INDEX_FILE_NAME)
    return payload if payload is not None else {}


def find_run(run_id: str, *, exclude: Path | str | None = None) -> list[RosterMatch]:
    """Other registries whose index records this run id."""
    matches: list[RosterMatch] = []
    for workspace in _other_workspaces(exclude):
        runs = _read_index(workspace).get("runs")
        entry = runs.get(run_id) if isinstance(runs, dict) else None
        if entry is None:
            continue
        alias = entry.get("alias") if isinstance(entry, dict) else None
        matches.append(
            RosterMatch(workspace, run_id=run_id, alias=alias if isinstance(alias, str) else None)
        )
    return matches


def find_alias(alias: str, *, exclude: Path | str | None = None) -> list[RosterMatch]:
    """Other registries that have a run under this alias (aliases are per registry)."""
    matches: list[RosterMatch] = []
    for workspace in _other_workspaces(exclude):
        aliases = _read_index(workspace).get("aliases")
        run_id = aliases.get(alias) if isinstance(aliases, dict) else None
        if isinstance(run_id, str):
            matches.append(RosterMatch(workspace, run_id=run_id, alias=alias))
    return matches


def find_workflow(wf_id: str, *, exclude: Path | str | None = None) -> list[RosterMatch]:
    """Other workspaces whose ``.delegate/workflows`` holds this workflow id."""
    matches: list[RosterMatch] = []
    for workspace in _other_workspaces(exclude):
        if (workspace / _DELEGATE_DIR_NAME / _WORKFLOWS_DIR_NAME / wf_id).is_dir():
            matches.append(RosterMatch(workspace, workflow_id=wf_id))
    return matches


_LISTED_MATCH_LIMIT = 5


def cwd_command(workspace: Path, command: str | None, handle: str) -> str:
    """The exact command that runs ``command handle`` inside ``workspace``."""
    return shlex.join(["delegate", "--cwd", str(workspace), *(command or "").split(), handle])


def describe_matches(
    handle: str,
    matches: list[RosterMatch],
    *,
    command: str | None = None,
) -> tuple[str, list[str]]:
    """Error text and next actions for a handle found in other Registries.

    A run id or workflow id names one thing, so the text names its workspace
    and the exact ``--cwd`` command. An alias is per Registry, so several
    workspaces can honestly hold it: they are listed and none is chosen.
    """
    if not matches:
        return "", []
    shown = matches[:_LISTED_MATCH_LIMIT]
    more = len(matches) - len(shown)
    next_actions = [cwd_command(match.workspace, command, handle) for match in shown]
    noun = "workflow" if matches[0].workflow_id is not None else "run"
    if matches[0].workflow_id is not None or handle == matches[0].run_id:
        if len(matches) == 1:
            workspace = matches[0].workspace
            hint = (
                f"Run: {next_actions[0]}"
                if command
                else f"Rerun the same command with --cwd {workspace}"
            )
            return f" This {noun} is recorded in workspace {workspace}. {hint}.", next_actions
        listed = ", ".join(str(match.workspace) for match in shown)
        suffix = f", and {more} more" if more else ""
        return (
            f" This {noun} id is recorded in {len(matches)} workspaces: {listed}{suffix}. "
            "Rerun the same command with --cwd for the one you mean.",
            next_actions,
        )
    listed = "; ".join(
        f"{match.workspace} (run {match.run_id})" if match.run_id else str(match.workspace)
        for match in shown
    )
    suffix = f"; and {more} more" if more else ""
    return (
        f" The alias {handle} is not unique across workspaces and is never resolved for you. "
        f"Workspaces that have it: {listed}{suffix}. "
        "Rerun with --cwd for the one you mean, or use the run id.",
        next_actions,
    )
