from __future__ import annotations

import contextlib
import fcntl
import hashlib
import inspect
import io
import json
import math
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from delegate_agent import (
    notify,
    personas,
    profiles,
    reasoning,
    run_registry,
    structured_output,
    wait_cancel_commands,
)
from delegate_agent.constants import (
    KNOWN_ENGINES,
    MODE_CALL,
    MODE_SAFE,
    PROMPT_ENFORCED_SAFE_ENGINES,
    PROMPT_INSTRUCTION_MODE_SLASH,
    PROMPT_INSTRUCTION_MODE_WRAPPED,
)
from delegate_agent.json_types import JsonObject, JsonValue
from delegate_agent.prompt_transport import (
    ARGV_PROMPT_GUARD_BYTES,
    ARGV_PROMPT_TRANSPORT_ENGINES,
)
from delegate_agent.workflows import registry
from delegate_agent.workflows import schema as workflow_schema
from delegate_agent.workflows import script as workflow_script

PROMPT_ARGV_GUARD_BYTES = ARGV_PROMPT_GUARD_BYTES
DEFAULT_ENGINE = "codex"
DEFAULT_MODE = "safe"
DEFAULT_STRUCTURED_RETRIES = 2
DEFAULT_ITEM_THREADS = 64
ENGINE_ARGV_TRANSPORT = ARGV_PROMPT_TRANSPORT_ENGINES
STRUCTURED_RESUME_ENGINES = frozenset({"codex", "claude", "cursor", "omp"})
PERSONA_RESOLUTION_ERRORS = frozenset(
    {
        "persona_not_found",
        "invalid_persona",
        "invalid_persona_encoding",
        "invalid_persona_control",
        "persona_too_large",
        "workspace_persona_refused",
    }
)
WORKFLOW_LOCK_FD_ENV = "DELEGATE_WORKFLOW_LOCK_FD"
WORKFLOW_WATCHDOG_INTERVAL_SECONDS = 0.25
CHILD_WAIT_POLL_SECONDS = 0.25
KILL_SUPERVISOR_WAIT_SECONDS = 5.0
KILL_SUPERVISOR_FORCE_WAIT_SECONDS = 2.0
WORKFLOW_EFFORT_VALUES = tuple(dict.fromkeys(reasoning.PI_THINKING_LEVELS))


class _MissingType:
    """Sentinel type: an adopted-run lookup found nothing definitive."""

    __slots__ = ()


_MISSING = _MissingType()


class PersonaDigestMismatch(RuntimeError):
    """A workflow child resolved different persona bytes than its parent pinned."""


CHILD_FAILURE_REASONS = frozenset({"timeout", "output_cap", "stall", "nonzero_exit", "structured"})


@dataclass(frozen=True)
class ChildAttemptOutcome:
    """Typed, retry-safe identity for one failed child attempt."""

    run_id: str | None
    failure_reason: str
    branch: str | None = None
    worktree: str | None = None
    cleanup_ownership: JsonObject | None = None
    execution_cwd: str | None = None
    session_id: str | None = None
    session_metadata: JsonObject | None = None

    def as_json(self) -> JsonObject:
        payload: JsonObject = {
            "runId": self.run_id,
            "failureReason": self.failure_reason,
            "branch": self.branch,
            "worktree": self.worktree,
            "executionCwd": self.execution_cwd,
            "cleanupOwnership": self.cleanup_ownership,
            "sessionId": self.session_id,
            "sessionMetadata": self.session_metadata,
        }
        return payload


@dataclass(frozen=True)
class StructuredAttemptOutcome:
    """The final structured-output parse/validation result for one agent call."""

    last_parsed_candidate: JsonValue | None
    validation_error: str
    candidate_present: bool = False

    def as_json(self) -> JsonObject:
        return {
            "lastParsedCandidate": (self.last_parsed_candidate if self.candidate_present else None),
            "validationError": self.validation_error,
        }


@dataclass(frozen=True)
class _DelegateChildResult:
    text: str | None
    run_id: str | None
    execution_cwd: str | None
    session_id: str | None
    workspace_cleanup: JsonObject | None = None
    isolation_backend: str | None = None
    outcome: ChildAttemptOutcome | None = None
    completion_report_source: str | None = None
    completion_report_path: str | None = None


def _delegate_child_result(value: object) -> _DelegateChildResult:
    if isinstance(value, _DelegateChildResult):
        return value
    return _DelegateChildResult(
        text=value if isinstance(value, str) else None,
        run_id=None,
        execution_cwd=None,
        session_id=None,
        workspace_cleanup=None,
        isolation_backend=None,
    )


def _normalize_child_failure_reason(value: object, *, default: str) -> str:
    if isinstance(value, str):
        raw = value.strip().lower()
        aliases = {
            "agent_timeout": "timeout",
            "call_timeout": "timeout",
            "timeout": "timeout",
            "output_limit_exceeded": "output_cap",
            "output_cap": "output_cap",
            "stalled": "stall",
            "stall": "stall",
            "harness_cancelled": "stall",
            "provider_cancelled": "stall",
            "provider_refusal": "nonzero_exit",
            "provider_max_turns": "nonzero_exit",
            "nonzero_exit": "nonzero_exit",
            "structured": "structured",
        }
        normalized = aliases.get(raw)
        if normalized is not None:
            return normalized
    return default


def _child_attempt_outcome(
    payload: JsonObject | None,
    *,
    default_reason: str,
    text: str | None = None,
) -> ChildAttemptOutcome:
    data = payload or {}
    run_id = data.get("runId") if isinstance(data.get("runId"), str) else None
    cleanup = (
        data.get("temporaryWorkspaceCleanup")
        if isinstance(data.get("temporaryWorkspaceCleanup"), dict)
        else None
    )
    session_metadata = (
        data.get("sessionMetadata") if isinstance(data.get("sessionMetadata"), dict) else None
    )
    return ChildAttemptOutcome(
        run_id=run_id,
        failure_reason=_normalize_child_failure_reason(
            data.get("failureReason") or data.get("error"), default=default_reason
        ),
        branch=(data.get("branch") if isinstance(data.get("branch"), str) else None),
        worktree=(
            data.get("worktree")
            if isinstance(data.get("worktree"), str)
            else data.get("executionCwd")
            if isinstance(data.get("executionCwd"), str)
            else None
        ),
        cleanup_ownership=cleanup,
        execution_cwd=(
            data.get("executionCwd") if isinstance(data.get("executionCwd"), str) else None
        ),
        session_id=(data.get("sessionId") if isinstance(data.get("sessionId"), str) else None),
        session_metadata=session_metadata,
    )


def _child_result_from_payload(result: JsonObject, *, text: str | None) -> _DelegateChildResult:
    outcome = None
    if result.get("ok") is not True:
        outcome = _child_attempt_outcome(result, default_reason="nonzero_exit", text=text)
    return _DelegateChildResult(
        text=text,
        run_id=result.get("runId") if isinstance(result.get("runId"), str) else None,
        execution_cwd=(
            result.get("executionCwd") if isinstance(result.get("executionCwd"), str) else None
        ),
        session_id=(result.get("sessionId") if isinstance(result.get("sessionId"), str) else None),
        workspace_cleanup=(
            result.get("temporaryWorkspaceCleanup")
            if isinstance(result.get("temporaryWorkspaceCleanup"), dict)
            else None
        ),
        isolation_backend=(
            result.get("isolationBackend")
            if isinstance(result.get("isolationBackend"), str)
            else None
        ),
        outcome=outcome,
        completion_report_source=(
            result.get("completionReportSource")
            if isinstance(result.get("completionReportSource"), str)
            else None
        ),
        completion_report_path=(
            result.get("completionReportPath")
            if isinstance(result.get("completionReportPath"), str)
            else None
        ),
    )


def _failed_child_result(
    recovered: _DelegateChildResult | None,
    *,
    reason: str,
    session_id: str | None = None,
) -> _DelegateChildResult:
    prior = recovered or _DelegateChildResult(None, None, None, None)
    outcome = prior.outcome or ChildAttemptOutcome(
        run_id=prior.run_id,
        failure_reason=reason,
        worktree=prior.execution_cwd,
        cleanup_ownership=prior.workspace_cleanup,
        execution_cwd=prior.execution_cwd,
        session_id=prior.session_id,
    )
    outcome = ChildAttemptOutcome(
        run_id=outcome.run_id,
        failure_reason=reason,
        branch=outcome.branch,
        worktree=outcome.worktree,
        cleanup_ownership=outcome.cleanup_ownership,
        execution_cwd=outcome.execution_cwd,
        session_id=session_id or outcome.session_id,
        session_metadata=outcome.session_metadata,
    )
    return _DelegateChildResult(
        text=None,
        run_id=prior.run_id,
        execution_cwd=prior.execution_cwd,
        session_id=session_id or prior.session_id,
        workspace_cleanup=prior.workspace_cleanup or outcome.cleanup_ownership,
        isolation_backend=prior.isolation_backend,
        outcome=outcome,
        completion_report_source=prior.completion_report_source,
        completion_report_path=prior.completion_report_path,
    )


def _session_id_from_child_output(output: object) -> str | None:
    if isinstance(output, bytes):
        text = output.decode("utf-8", errors="replace")
    elif isinstance(output, str):
        text = output
    else:
        return None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        ):
            return event["thread_id"]
    return None


def _cleanup_structured_retry_workspace(record: JsonObject | None) -> None:
    if record is None:
        return
    git_root = record.get("gitRoot")
    isolated_workspace = record.get("isolatedWorkspace")
    temp_base = record.get("tempBase")
    source_root = record.get("sourceRoot")
    if (
        (git_root is not None and not isinstance(git_root, str))
        or not isinstance(isolated_workspace, str)
        or not isinstance(temp_base, str)
        or not isinstance(source_root, str)
    ):
        raise RuntimeError("structured retry workspace cleanup metadata is invalid")
    from delegate_agent import safe_workspace

    safe_workspace.cleanup_safe_isolated_workspace(
        git_root=git_root,
        isolated_workspace=isolated_workspace,
        temp_base=temp_base,
        source_root=source_root,
    )


def _workflow_agent_run_result_metadata(
    workspace: Path,
    wf_id: str,
    workflow_agent_key: str,
) -> _DelegateChildResult | None:
    """Recover child identity and cleanup ownership from the latest run snapshot.

    The supervisor may be handling a timeout, a killed child, or a malformed
    child envelope. In each case stdout is not a reliable ownership channel;
    the runner's first progress persist is the durable source of truth.
    """
    run_id = _find_workflow_agent_run(workspace, wf_id, workflow_agent_key)
    if run_id is None:
        return None
    root = _run_registry_root(workspace)
    index = run_registry.load_index(root)
    entry = index.get("runs", {}).get(run_id)
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    manifest = run_registry.load_run_manifest_or_none(root, run_id)
    records = tuple(record for record in (snapshot, manifest, entry) if isinstance(record, dict))
    if not records:
        return None
    execution_cwd = next(
        (
            value
            for record in records
            for value in (record.get("executionCwd"),)
            if isinstance(value, str) and value
        ),
        None,
    )
    branch = next(
        (
            value
            for record in records
            for value in (record.get("branch"), record.get("plannedBranch"))
            if isinstance(value, str) and value
        ),
        None,
    )
    session_id = snapshot.get("sessionId") if isinstance(snapshot, dict) else None
    cleanup = next(
        (
            value
            for record in records
            for value in (record.get("temporaryWorkspaceCleanup"),)
            if isinstance(value, dict)
        ),
        None,
    )
    backend = next(
        (
            value
            for record in records
            for value in (record.get("isolationBackend"),)
            if isinstance(value, str)
        ),
        None,
    )
    completion_report_source = next(
        (
            value
            for record in records
            for value in (record.get("completionReportSource"),)
            if isinstance(value, str)
        ),
        None,
    )
    completion_report_path = next(
        (
            value
            for record in records
            for value in (
                record.get("completionReportPath"),
                (
                    record.get("completionReport", {}).get("path")
                    if isinstance(record.get("completionReport"), dict)
                    else None
                ),
            )
            if isinstance(value, str)
        ),
        None,
    )
    return _DelegateChildResult(
        text=None,
        run_id=run_id,
        execution_cwd=execution_cwd,
        session_id=session_id if isinstance(session_id, str) else None,
        workspace_cleanup=cleanup if isinstance(cleanup, dict) else None,
        isolation_backend=backend if isinstance(backend, str) else None,
        outcome=ChildAttemptOutcome(
            run_id=run_id,
            failure_reason="nonzero_exit",
            branch=branch,
            worktree=execution_cwd,
            cleanup_ownership=cleanup if isinstance(cleanup, dict) else None,
            execution_cwd=execution_cwd,
            session_id=session_id if isinstance(session_id, str) else None,
        ),
        completion_report_source=completion_report_source,
        completion_report_path=completion_report_path,
    )


def _cleanup_workflow_agent_run_workspace(workspace: Path, run_id: str) -> None:
    if not run_registry.RUN_ID_RE.fullmatch(run_id):
        return
    root = _run_registry_root(workspace)
    index = run_registry.load_index(root)
    entry = index.get("runs", {}).get(run_id)
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    manifest = run_registry.load_run_manifest_or_none(root, run_id)
    for record in (snapshot, manifest, entry):
        if isinstance(record, dict):
            cleanup = record.get("temporaryWorkspaceCleanup")
            if isinstance(cleanup, dict):
                _cleanup_structured_retry_workspace(cleanup)
                return


def _release_structured_retry_worktree_for_state(state: WorkflowState, run_id: str) -> None:
    if not run_registry.RUN_ID_RE.fullmatch(run_id):
        return
    root = _run_registry_root(state.workspace)
    if not root.exists():
        return
    from delegate_agent import config as delegate_config
    from delegate_agent import worktree_mgmt

    auto_prune, auto_prune_days = delegate_config.worktree_auto_prune_settings(state.config)
    worktree_mgmt.retire_completed_worktree(
        root,
        run_id,
        retire_worktree=delegate_config.retire_worktree_on_completion(state.config),
        retirement_ignore_globs=delegate_config.retirement_ignore_globs(state.config),
        auto_prune=auto_prune,
        auto_prune_days=auto_prune_days,
    )


def cleanup_workflow_agent_workspaces(
    workspace: Path,
    wf_id: str,
    workflow_agent_key: str | None = None,
) -> None:
    """Reap all structured temporary workspaces belonging to a workflow."""
    root = _run_registry_root(workspace)
    if not root.exists():
        return
    index = run_registry.load_index(root)
    for run_id, entry in run_registry.index_run_entries(index):
        if entry.get("group") != wf_id:
            continue
        if workflow_agent_key is not None and entry.get("workflowAgentKey") != workflow_agent_key:
            continue
        _cleanup_workflow_agent_run_workspace(workspace, run_id)


class BudgetExceeded(RuntimeError):
    pass


class GateExit(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        gate_key: str | None = None,
        child: str | None = None,
        result: JsonValue = None,
        result_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.gate_key = gate_key
        self.child = child
        self.result = result
        self.result_hash = result_hash


class SoftParkExit(RuntimeError):
    """The runnable set drained with one or more named items parked."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        joined = ", ".join(names)
        super().__init__(f"workflow soft-parked items: {joined}")


class _SoftParkRequest(RuntimeError):
    """Internal request raised by an item callback to release its slot."""

    def __init__(self, name: str, result: JsonValue = None) -> None:
        self.name = name
        self.result = result
        super().__init__(f"soft-park item {name!r}")


class SoftPark(_SoftParkRequest):
    """Public callback signal for a named item that must yield its slot."""


class SupervisorWatchdogExit(RuntimeError):
    """Internal cooperative cancellation raised by the supervisor watchdog."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"workflow supervisor watchdog: {reason}")
        self.reason = reason


@dataclass
class Budget:
    total: int | None
    _spent: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            if self.total is None:
                return float("inf")
            return max(self.total - self._spent, 0)

    def claim(self) -> int:
        with self._lock:
            if self.total is not None and self._spent >= self.total:
                raise BudgetExceeded("workflow budget exceeded")
            self._spent += 1
            return self._spent

    def reconcile_spent(self, minimum: int) -> None:
        with self._lock:
            if minimum > self._spent:
                self._spent = minimum


@dataclass(frozen=True)
class CompletedChild:
    run_id: str
    engine: str
    resumable: bool


@dataclass
class WorkflowState:
    wf_id: str
    workspace: Path
    root: Path
    script_path: Path
    config: JsonObject
    cli_argv: list[str]
    args: JsonValue
    budget: Budget
    dry_run: bool = False
    replay_journal: bool = True
    workflow_key_version: int = 1
    # Read once at supervisor start and re-emitted on every status write.
    # status.json is REBUILT from scratch by _write_status_locked rather than
    # merged, so a key written only at create time is erased by the supervisor's
    # first write -- which is exactly what happened to the first version of this.
    notify_target: str | None = None
    depth: int = 0
    namespace: str = "root"
    replay: dict[str, JsonValue] = field(default_factory=dict)
    replay_keys: set[str] = field(default_factory=set)
    failed_replay_keys: set[str] = field(default_factory=set)
    tombstoned_keys: set[str] = field(default_factory=set)
    # Starts recorded after a tombstone are fresh work.  Preserve the
    # tombstone for v2 attempt-key derivation, but allow a later supervisor to
    # adopt that fresh child rather than launching a third copy of it.
    started_after_tombstone: set[str] = field(default_factory=set)
    label_keys: dict[str, str] = field(default_factory=dict)
    started_scopes: dict[str, str] = field(default_factory=dict)
    started_without_result: set[str] = field(default_factory=set)
    exhausted_keys: set[str] = field(default_factory=set)
    claimed_keys: set[str] = field(default_factory=set)
    sequence: int = 0
    journal_lock: threading.Lock = field(default_factory=threading.Lock)
    scope_lock: threading.Lock = field(default_factory=threading.Lock)
    lifetime_lock: threading.Lock = field(default_factory=threading.Lock)
    lifetime_counter: list[int] = field(default_factory=lambda: [0])
    gate_state: dict[str, bool | int] = field(
        default_factory=lambda: {"stop_admitting": False, "in_flight_agents": 0}
    )
    gate_condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )
    dry_runs: list[JsonObject] = field(default_factory=list)
    dry_run_budget_spent: int = 0
    label_lock: threading.Lock = field(default_factory=threading.Lock)
    completed_labels: dict[str, list[CompletedChild]] = field(default_factory=dict)
    supervisor_token: str = field(default_factory=lambda: os.urandom(8).hex())
    replay_attempt: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    retry_worktree_runs: set[str] = field(default_factory=set)
    pending_gate: list[tuple[str, str | None, JsonValue, str]] = field(default_factory=list)
    soft_parked_items: dict[str, JsonObject] = field(default_factory=dict)
    soft_park_scopes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.journal_path = self.root / registry.JOURNAL_FILE
        self.status_path = self.root / registry.STATUS_FILE
        self.result_path = self.root / registry.RESULT_FILE
        self.thread_local = threading.local()
        self.agent_semaphore = threading.Semaphore(_global_agent_cap())
        self.engine_semaphores = _engine_semaphores(self.config)
        self.item_semaphore = threading.Semaphore(_item_thread_cap(self.config))
        self._load_replay(include_simulated=self.replay_journal)

    def _load_replay(self, *, include_simulated: bool) -> None:
        status = registry.read_json(self.status_path) or {}
        last_seq = status.get("lastSeq")
        if isinstance(last_seq, int):
            self.sequence = max(self.sequence, last_seq)
        simulated_keys: set[str] = set()
        child_info: dict[str, tuple[str, str, bool, str | None]] = {}
        for event in registry.iter_journal(self.journal_path):
            seq = event.get("seq")
            if isinstance(seq, int):
                self.sequence = max(self.sequence, seq)
            if event.get("simulated") is True:
                continue
            etype = event.get("type")
            if etype == "agent_child":
                ckey = event.get("workflowAgentKey") or event.get("key")
                crun_id = event.get("runId")
                cengine = event.get("engine")
                clabel = event.get("label")
                cresumable = event.get("resumable") is True
                if isinstance(ckey, str) and isinstance(crun_id, str) and isinstance(cengine, str):
                    child_info[ckey] = (
                        crun_id,
                        cengine,
                        cresumable,
                        clabel if isinstance(clabel, str) else None,
                    )
            if etype == "item_parked":
                name = event.get("name")
                scope = event.get("scope")
                if isinstance(name, str) and name and isinstance(scope, str) and scope:
                    self.soft_park_scopes[name] = scope
                    self.soft_parked_items[name] = dict(event)
                continue
            if etype == "item_unparked":
                name = event.get("name")
                if isinstance(name, str) and name:
                    self.soft_parked_items.pop(name, None)
                continue
            # agent_adopted events carry no engine/resumable metadata; the
            # agent_child + agent_finished pair (always present for adopted
            # runs via _emit_adopted_child_identity) registers the label with
            # correct metadata, so a separate agent_adopted handler here would
            # double-register the label and break followup() after a resume.
            key = event.get("key")
            if not isinstance(key, str):
                continue
            label = event.get("label")
            if (
                event.get("type") in {"agent_started", "agent_finished"}
                and isinstance(label, str)
                and label
            ):
                self.label_keys[label] = key
            scope = event.get("scope")
            if event.get("type") == "agent_started" and isinstance(scope, str):
                self.started_scopes[key] = scope
            is_simulated = event.get("simulated") is True or event.get("dryRun") is True
            if is_simulated:
                simulated_keys.add(key)
            if not include_simulated and is_simulated:
                continue
            if not include_simulated and key in simulated_keys:
                if event.get("type") in {"budget", "agent_started"}:
                    simulated_keys.discard(key)
                else:
                    continue
            if event.get("type") == "agent_rejected":
                # A tombstone only invalidates a result that exists before it.
                # Repeated tombstones and no-result tombstones are durable
                # no-ops, preserving any unfinished adoption state.  The
                # tombstone marker itself remains so v2 can select a retry key.
                self.tombstoned_keys.add(key)
                self.started_after_tombstone.discard(key)
                if key not in self.replay_keys and key not in self.replay:
                    continue
                self.replay_keys.discard(key)
                self.replay.pop(key, None)
                self.started_without_result.discard(key)
                self.exhausted_keys.discard(key)
            elif event.get("type") == "budget":
                # Idempotent resume: keys already charged must not re-claim.
                self.claimed_keys.add(key)
            elif event.get("type") == "agent_started":
                self.started_without_result.add(key)
                if key in self.tombstoned_keys:
                    self.started_after_tombstone.add(key)
            elif event.get("type") == "agent_finished":
                # An exhausted key replays its None without respawning, but a
                # child run that did finish gets one adoption attempt first —
                # a resume after a parser or schema fix should pick it up.
                if event.get("exhausted") is True:
                    self.exhausted_keys.add(key)
                else:
                    self.exhausted_keys.discard(key)
                result = event.get("result")
                # A result after a tombstone is a fresh settlement and is
                # replayable.  A tombstone that follows this row removes it on
                # the next pass through the journal.
                self.tombstoned_keys.discard(key)
                self.started_after_tombstone.discard(key)
                # Historical v1 journals replay every completed result,
                # including an exhausted ``None``.  CP2's explicit reject()
                # tombstone adds invalidation without reinterpreting these
                # existing rows.
                self.failed_replay_keys.discard(key)
                self.replay_keys.add(key)
                self.replay[key] = result
                self.started_without_result.discard(key)
                if result is not None and key in child_info:
                    crun_id, cengine, cresumable, clabel = child_info[key]
                    if clabel is not None:
                        self.record_completed_child(clabel, crun_id, cengine, cresumable)
        # Budget events are fsynced but status.json is not, so after a hard
        # crash the seeded spent can lag the durable claim set. One claim per
        # key, so spent is at least len(claimed_keys); status can only lag
        # the journal, never lead it.
        self.budget.reconcile_spent(len(self.claimed_keys))

    def append_journal_only(self, event_type: str, **payload: JsonValue) -> None:
        """Record an event without touching status.

        `append_event` writes status="running" alongside every journal line,
        which is right for work events and catastrophic for anything recorded
        AFTER a terminal write: notifying on "succeeded" and then journalling the
        result reset the workflow to running, and two tests caught it. Telemetry
        about a finished workflow must not un-finish it.
        """
        with self.journal_lock:
            self.sequence += 1
            registry.append_jsonl(
                self.journal_path,
                {
                    "seq": self.sequence,
                    "type": event_type,
                    "at": run_registry.utc_now_iso(),
                    **payload,
                },
            )

    def append_durable_event(self, event_type: str, **payload: JsonValue) -> JsonObject:
        """Append and fsync a journal event without relying on its registry list."""
        with self.journal_lock:
            status = registry.read_json(self.status_path)
            last_seq = status.get("lastSeq") if isinstance(status, dict) else None
            if isinstance(last_seq, int):
                self.sequence = max(self.sequence, last_seq)
            self.sequence += 1
            event: JsonObject = {
                "seq": self.sequence,
                "type": event_type,
                "at": run_registry.utc_now_iso(),
                **payload,
            }
            fd = run_registry.open_private_file(
                self.journal_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY
            )
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            gate_key = status.get("gateKey") if isinstance(status, dict) else None
            if (
                isinstance(status, dict)
                and status.get("status") == "paused"
                and isinstance(gate_key, str)
            ):
                extra: JsonObject = {
                    "gateKey": gate_key,
                    "gateResult": status.get("gateResult"),
                }
                gate_result_hash = status.get("gateResultHash")
                if isinstance(gate_result_hash, str):
                    extra["gateResultHash"] = gate_result_hash
                self._write_status_locked(
                    status="paused",
                    last_event=event,
                    extra=extra,
                )
            else:
                self._write_status_locked(status="running", last_event=event)
            return event

    def append_event(self, event_type: str, **payload: JsonValue) -> JsonObject:
        with self.journal_lock:
            status = registry.read_json(self.status_path)
            last_seq = status.get("lastSeq") if isinstance(status, dict) else None
            if isinstance(last_seq, int):
                self.sequence = max(self.sequence, last_seq)
            self.sequence += 1
            event = {
                "seq": self.sequence,
                "type": event_type,
                "at": run_registry.utc_now_iso(),
                **payload,
            }
            event_key = event.get("key")
            event_label = event.get("label")
            if (
                event_type in {"agent_started", "agent_finished"}
                and isinstance(event_key, str)
                and isinstance(event_label, str)
                and event_label
            ):
                self.label_keys[event_label] = event_key
            event_scope = event.get("scope")
            if (
                event_type == "agent_started"
                and isinstance(event_key, str)
                and isinstance(event_scope, str)
            ):
                self.started_scopes[event_key] = event_scope
            registry.append_jsonl(self.journal_path, event)
            # A gate writes status="paused" with its gateKey and then raises GateExit; the
            # script's own unwind logging must not clobber that back to "running", or
            # `workflow approve` sees nothing gated and the parked supervisor reads as dead.
            gate_key = status.get("gateKey") if isinstance(status, dict) else None
            if (
                isinstance(status, dict)
                and status.get("status") == "paused"
                and isinstance(gate_key, str)
            ):
                extra: JsonObject = {
                    "gateKey": gate_key,
                    "gateResult": status.get("gateResult"),
                }
                gate_result_hash = status.get("gateResultHash")
                if isinstance(gate_result_hash, str):
                    extra["gateResultHash"] = gate_result_hash
                self._write_status_locked(
                    status="paused",
                    last_event=event,
                    extra=extra,
                )
            else:
                self._write_status_locked(status="running", last_event=event)
            return event

    def _latest_gate_event_locked(
        self, gate_key: str, result_hash: str | None = None
    ) -> JsonObject | None:
        latest: JsonObject | None = None
        for event in registry.iter_journal(self.journal_path):
            event_hash = event.get("gateResultHash")
            if (
                event.get("type") == "gate"
                and event.get("key") == gate_key
                and (event_hash if isinstance(event_hash, str) else None) == result_hash
            ):
                latest = event
        return latest

    def latest_gate_event(self) -> JsonObject | None:
        with self.journal_lock:
            latest: JsonObject | None = None
            for event in registry.iter_journal(self.journal_path):
                if event.get("type") == "gate" and isinstance(event.get("key"), str):
                    latest = event
            return latest

    def _write_gate_projection_locked(self, event: JsonObject) -> None:
        gate_key = event.get("key")
        if not isinstance(gate_key, str):
            return
        extra: JsonObject = {"gateKey": gate_key, "gateResult": event.get("result")}
        result_hash = event.get("gateResultHash")
        if isinstance(result_hash, str):
            extra["gateResultHash"] = result_hash
        self._write_status_locked(status="paused", last_event=event, extra=extra)

    def park_gate(self, gate_key: str, *, child: str | None, result: JsonValue) -> GateExit:
        """Durably record a gate before closing admission and draining agents.

        The journal is the authority.  ``status.json`` is only a recoverable
        projection, so a supervisor death while draining cannot lose the gate
        identity. Re-parking the same key and result reuses its journal event
        and never appends a duplicate.
        """
        result_hash = _gate_result_hash(result)
        with self.journal_lock:
            status = registry.read_json(self.status_path) or {}
            last_seq = status.get("lastSeq") if isinstance(status, dict) else None
            if isinstance(last_seq, int):
                self.sequence = max(self.sequence, last_seq)
            for prior in registry.iter_journal(self.journal_path):
                seq = prior.get("seq")
                if isinstance(seq, int):
                    self.sequence = max(self.sequence, seq)
            event = self._latest_gate_event_locked(gate_key, result_hash)
            if event is None:
                self.sequence += 1
                event = {
                    "seq": self.sequence,
                    "type": "gate",
                    "at": run_registry.utc_now_iso(),
                    "key": gate_key,
                    "child": child,
                    "result": result,
                    "gateResultHash": result_hash,
                }
                # ``gate`` is in DURABLE_EVENT_TYPES, so append_jsonl flushes
                # and fsyncs before anything below can close admission.
                registry.append_jsonl(self.journal_path, event)

        # Keep the metadata available to agents that race with the admission
        # close.  This is intentionally after the durable append above.
        self.pending_gate.append((gate_key, child, result, result_hash))
        try:
            self.close_gate_and_wait()
        finally:
            # A watchdog may interrupt the drain.  The journal event still
            # exists; make the projection recoverable before propagating the
            # interruption so approve/resume never depends on status.json.
            with self.journal_lock:
                self._write_gate_projection_locked(event)
        return GateExit(
            "workflow gate checkpoint reached",
            gate_key=gate_key,
            child=child,
            result=result,
            result_hash=result_hash,
        )

    def persist_gate(self, gate_key: str, *, child: str | None, result: JsonValue) -> GateExit:
        """Backward-compatible alias for the journal-authoritative gate park."""
        return self.park_gate(gate_key, child=child, result=result)

    def ensure_gate_durable(self, exc: GateExit) -> None:
        if exc.gate_key is None:
            return
        with self.journal_lock:
            event = self._latest_gate_event_locked(exc.gate_key, exc.result_hash)
            status = registry.read_json(self.status_path) or {}
            if event is not None:
                # Rebuild the projection from the journal, even if a stale
                # status write raced with the gate drain.
                self._write_gate_projection_locked(event)
                return
            status_hash = status.get("gateResultHash")
            if (
                status.get("status") == "paused"
                and status.get("gateKey") == exc.gate_key
                and (status_hash if isinstance(status_hash, str) else None) == exc.result_hash
            ):
                return
        self.park_gate(exc.gate_key, child=exc.child, result=exc.result)

    def closed_gate_exit(self) -> GateExit:
        if self.pending_gate:
            gate_key, child, result, result_hash = self.pending_gate[-1]
            return GateExit(
                "workflow gate is closed to new agent calls",
                gate_key=gate_key,
                child=child,
                result=result,
                result_hash=result_hash,
            )
        latest = self.latest_gate_event()
        if latest is not None:
            gate_key = latest.get("key")
            if isinstance(gate_key, str):
                child = latest.get("child")
                return GateExit(
                    "workflow gate is closed to new agent calls",
                    gate_key=gate_key,
                    child=child if isinstance(child, str) else None,
                    result=latest.get("result"),
                    result_hash=(
                        latest.get("gateResultHash")
                        if isinstance(latest.get("gateResultHash"), str)
                        else None
                    ),
                )
        return GateExit("workflow gate is closed to new agent calls")

    def _known_agent_keys(self) -> set[str]:
        """Return structural agent keys known to this live/replayed workflow."""
        with self.journal_lock:
            known = {
                *self.replay,
                *self.replay_keys,
                *self.failed_replay_keys,
                *self.tombstoned_keys,
                *self.started_without_result,
                *self.exhausted_keys,
                *self.claimed_keys,
                *self.started_scopes,
                *self.label_keys.values(),
            }
            for event in registry.iter_journal(self.journal_path):
                event_type = event.get("type")
                if not (
                    isinstance(event_type, str)
                    and (event_type.startswith("agent_") or event_type == "budget")
                ):
                    continue
                key = event.get("key")
                if isinstance(key, str):
                    known.add(key)
            return known

    def resolve_agent_key(self, key_or_label: object) -> tuple[str, str | None]:
        """Resolve an exact structural key or the most recent exact label."""
        if not isinstance(key_or_label, str) or not key_or_label.strip():
            raise ValueError("reject() expects a non-empty agent key or label")
        raw = key_or_label
        known = self._known_agent_keys()
        if raw in known:
            return raw, None
        key = self.label_keys.get(raw)
        if isinstance(key, str) and key in known:
            return key, raw
        raise ValueError(f"reject() could not resolve agent key or label: {raw!r}")

    def _has_cached_result(self, key: str) -> bool:
        """Check the latest cached-result state, including newly appended rows."""
        cached = False
        saw_settlement = False
        with self.journal_lock:
            for event in registry.iter_journal(self.journal_path):
                if event.get("key") != key:
                    continue
                if not self.replay_journal and (
                    event.get("simulated") is True or event.get("dryRun") is True
                ):
                    continue
                if event.get("type") == "agent_rejected":
                    cached = False
                    saw_settlement = True
                elif event.get("type") == "agent_finished":
                    cached = True
                    saw_settlement = True
        if saw_settlement:
            return cached
        return key in self.replay_keys or key in self.replay

    def reject_agent(self, key_or_label: object, reason: object) -> str:
        """Durably tombstone an agent key so a later call executes fresh."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reject() expects a non-empty reason string")
        key, label = self.resolve_agent_key(key_or_label)
        has_cached_result = self._has_cached_result(key)
        event: JsonObject = {"key": key, "reason": reason}
        if label is not None:
            event["label"] = label
        # The durable row is written before mutating in-memory replay state.
        self.append_event("agent_rejected", **event)
        # agent() snapshots every cache-decision input under journal_lock, so
        # invalidate this matching set atomically with respect to that reader.
        with self.journal_lock:
            self.tombstoned_keys.add(key)
            self.started_after_tombstone.discard(key)
            # Tombstones with no cached result do not clear an unfinished adoption
            # state; the marker remains for v2's fresh attempt-key derivation.
            if not has_cached_result:
                return key
            self.replay_keys.discard(key)
            self.replay.pop(key, None)
            self.started_without_result.discard(key)
            self.exhausted_keys.discard(key)
        return key

    def cancel_stale_scope_children(self, scope: str, current_key: str) -> None:
        for old_key, old_scope in tuple(self.started_scopes.items()):
            if (
                old_scope != scope
                or old_key == current_key
                or old_key not in self.started_without_result
            ):
                continue
            cancel_workflow_agent_child(self.workspace, self.wf_id, old_key)
            old_run = _find_workflow_agent_run(self.workspace, self.wf_id, old_key)
            if old_run is not None:
                _cleanup_workflow_agent_run_workspace(self.workspace, old_run)
            self.started_without_result.discard(old_key)

    def _write_status_locked(
        self,
        *,
        status: str,
        last_event: JsonObject | None = None,
        extra: JsonObject | None = None,
    ) -> None:
        effective_status = "dry_run" if self.dry_run else status
        payload: JsonObject = {
            "ok": effective_status not in {"failed", "killed"},
            "wfId": self.wf_id,
            "status": effective_status,
            "workspace": str(self.workspace),
            "scriptPath": str(self.script_path),
            "journalPath": str(self.journal_path),
            "resultPath": str(self.result_path),
            "lastSeq": self.sequence,
            "budget": {
                "total": self.budget.total,
                "spent": self.budget.spent(),
                "remaining": _budget_remaining_json(self.budget),
            },
            "supervisorPid": os.getpid(),
            "supervisorPgid": os.getpgrp(),
            "supervisorToken": self.supervisor_token,
            "notify": self.notify_target,
            "replayAttempt": self.replay_attempt,
            "updatedAt": run_registry.utc_now_iso(),
        }
        if self.workflow_key_version == 2:
            payload["workflowKeyVersion"] = 2
        if not self.replay_journal:
            payload["replayJournal"] = False
        if last_event is not None:
            payload["lastEvent"] = last_event
        if extra:
            payload.update(extra)
        registry.write_status(self.root, payload)

    def write_status(self, status: str, **extra: JsonValue) -> None:
        with self.journal_lock:
            self._write_status_locked(status=status, extra=extra)

    def _record_watchdog_fire(self, reason: str) -> bool:
        """Persist the watchdog marker and fire event before requesting cancel."""
        with self.journal_lock:
            status_present = self.status_path.exists()
            status = registry.read_json(self.status_path)
            if isinstance(status, dict) and status.get("status") in {
                "succeeded",
                "failed",
                "killed",
            }:
                return False
            last_seq = status.get("lastSeq") if isinstance(status, dict) else None
            if isinstance(last_seq, int):
                self.sequence = max(self.sequence, last_seq)
            self.sequence += 1
            fired_at = run_registry.utc_now_iso()
            event: JsonObject = {
                "seq": self.sequence,
                "type": "workflow_watchdog_fired",
                "at": fired_at,
                "reason": reason,
            }
            with contextlib.suppress(OSError):
                registry.append_jsonl(self.journal_path, event)
            # A deliberately removed status file is itself the watchdog signal.
            # Do not resurrect it while recording the state-missing fire.
            if not status_present or not self.status_path.exists():
                return True

            status_value = status.get("status") if isinstance(status, dict) else None
            status_name = status_value if isinstance(status_value, str) else "running"
            extra: JsonObject = {
                "watchdogFiredAt": fired_at,
                "watchdogReason": reason,
                "watchdogCancelRequested": True,
            }
            if status_name == "paused":
                gate_key = status.get("gateKey") if isinstance(status, dict) else None
                parked_items = status.get("parkedItems") if isinstance(status, dict) else None
                if isinstance(gate_key, str):
                    extra["gateKey"] = gate_key
                    if isinstance(status, dict):
                        extra["gateResult"] = status.get("gateResult")
                        gate_result_hash = status.get("gateResultHash")
                        if isinstance(gate_result_hash, str):
                            extra["gateResultHash"] = gate_result_hash
                elif isinstance(parked_items, list):
                    extra["parkedItems"] = parked_items
                    if isinstance(status, dict):
                        extra["softPark"] = status.get("softPark")
            try:
                self._write_status_locked(status=status_name, last_event=event, extra=extra)
            except OSError:
                # A damaged status path still needs cooperative cancellation;
                # the fire event remains the durable audit when projection is
                # unavailable.
                return True
            return True

    def notify_event(self, event: str, *, detail: str = "") -> None:
        """Send one metadata line to the workflow's --notify target, if any.

        A workflow supervisor is detached: it parks at a gate, dies, or finishes
        with nobody watching, and until now had no way to ring anyone. The
        consumer running six lanes tonight was babysitting it with a cron and a
        scheduled wake-up.

        Target comes from status.json rather than argv because the supervisor
        re-execs itself detached. Failures degrade exactly as launch notify does
        -- a notification that cannot be delivered must never change a workflow's
        outcome.
        """
        target_spec = self.notify_target
        if not target_spec:
            return
        suffix = f" — {detail}" if detail else ""
        message = f"delegate workflow {self.wf_id} {event}{suffix}"
        try:
            target = notify.parse_notify_target(target_spec)
            outcome = notify.send_notification(
                target, message, cwd=str(self.workspace), env=profiles.child_environment()
            )
        except Exception as exc:  # telemetry never fails the workflow
            self.append_journal_only(
                "notify_degraded", event=event, reason="hook_failed", detail=str(exc)[:200]
            )
            return
        # A silent degradation is indistinguishable from a delivered
        # notification, which is the same failure this feature exists to prevent
        # one level up. The launch path records notify.ok=false plus a reason;
        # this recorded nothing at all, so a workflow could believe it had rung
        # someone for hours.
        if not outcome.ok:
            self.append_journal_only(
                "notify_degraded",
                event=event,
                reason=outcome.reason or "unknown",
                detail=(outcome.detail or "")[:200],
            )

    def current_scope(self) -> str:
        return getattr(self.thread_local, "scope", self.namespace)

    def next_child_scope(self, kind: str) -> str:
        with self.scope_lock:
            counters = getattr(self.thread_local, "counters", None)
            if counters is None:
                counters = {}
                self.thread_local.counters = counters
            scope = self.current_scope()
            key = f"{scope}:{kind}"
            value = counters.get(key, 0)
            counters[key] = value + 1
            return f"{scope}/{kind}@{value}"

    @contextlib.contextmanager
    def scope(self, value: str) -> Iterator[None]:
        previous = self.current_scope()
        previous_counters = getattr(self.thread_local, "counters", None)
        self.thread_local.scope = value
        self.thread_local.counters = {}
        try:
            yield
        finally:
            self.thread_local.scope = previous
            self.thread_local.counters = previous_counters or {}

    def next_agent_path(self) -> str:
        with self.scope_lock:
            counters = getattr(self.thread_local, "counters", None)
            if counters is None:
                counters = {}
                self.thread_local.counters = counters
            scope = self.current_scope()
            key = f"{scope}:seq"
            value = counters.get(key, 0)
            counters[key] = value + 1
            return f"{scope}/seq#{value}"

    def claim_agent_lifetime(self) -> int:
        with self.lifetime_lock:
            self.lifetime_counter[0] += 1
            if self.lifetime_counter[0] > workflow_script.LIFETIME_AGENT_LIMIT:
                raise RuntimeError("workflow exceeded 1000 lifetime agent() calls")
            return self.lifetime_counter[0]

    @contextlib.contextmanager
    def active_agent(self) -> Iterator[None]:
        with self.gate_condition:
            if self.cancel_event.is_set():
                raise SupervisorWatchdogExit("cancellation requested before agent admission")
            if self.gate_state["stop_admitting"]:
                raise self.closed_gate_exit()
            self.gate_state["in_flight_agents"] += 1
        try:
            yield
        finally:
            with self.gate_condition:
                self.gate_state["in_flight_agents"] -= 1
                self.gate_condition.notify_all()

    def close_gate_and_wait(self) -> None:
        with self.gate_condition:
            self.gate_state["stop_admitting"] = True
            while self.gate_state["in_flight_agents"]:
                if self.cancel_event.is_set():
                    raise SupervisorWatchdogExit("cancellation requested while closing gate")
                self.gate_condition.wait(timeout=0.2)

    def inside_item_thread(self) -> bool:
        return bool(getattr(self.thread_local, "item_depth", 0))

    @contextlib.contextmanager
    def item_slot(self, *, bypass: bool = False, pre_acquired: bool = False) -> Iterator[None]:
        manager = contextlib.nullcontext() if bypass or pre_acquired else self.item_semaphore
        with manager:
            previous = getattr(self.thread_local, "item_depth", 0)
            self.thread_local.item_depth = previous + 1
            try:
                yield
            finally:
                self.thread_local.item_depth = previous
                if pre_acquired:
                    self.item_semaphore.release()

    def soft_park_scope(self, name: str) -> str:
        """Return the durable, explicit scope for one named soft-park item."""
        _validate_soft_park_name(name)
        existing = self.soft_park_scopes.get(name)
        if existing is not None:
            return existing
        scope = f"{self.namespace}/soft-park/{name}"
        self.soft_park_scopes[name] = scope
        return scope

    def park_item(self, name: object, result: JsonValue = None) -> None:
        """Request that the enclosing ``soft_park`` item unwind its slot."""
        if not isinstance(name, str):
            raise ValueError("park_item() expects a non-empty stable name")
        _validate_soft_park_name(name)
        raise SoftPark(name, result)

    def soft_park_request(self, name: object, result: JsonValue = None) -> _SoftParkRequest:
        """Build a park marker that an item callback may return."""
        if not isinstance(name, str):
            raise ValueError("soft_park_request() expects a non-empty stable name")
        _validate_soft_park_name(name)
        return SoftPark(name, result)

    def is_soft_parked(self, name: object) -> bool:
        return isinstance(name, str) and name in self.soft_parked_items

    def record_item_park(
        self, name: str, *, scope: str, result: JsonValue, owner_scope: str
    ) -> None:
        """Persist one item park before its worker unwinds and releases its slot."""
        with self.journal_lock:
            if name in self.soft_parked_items:
                return
        event = self.append_durable_event(
            "item_parked",
            name=name,
            scope=scope,
            ownerScope=owner_scope,
            result=result,
        )
        with self.journal_lock:
            self.soft_park_scopes[name] = scope
            self.soft_parked_items[name] = event

    def record_item_unpark(self, name: str, *, scope: str) -> None:
        """Persist successful replay of a previously parked item."""
        self.append_durable_event("item_unparked", name=name, scope=scope)
        with self.journal_lock:
            self.soft_parked_items.pop(name, None)

    def dry_run_budget_tick(self) -> int:
        with self.lifetime_lock:
            self.dry_run_budget_spent += 1
            return self.dry_run_budget_spent

    def record_completed_child(
        self, label: str | None, run_id: str, engine: str, resumable: bool
    ) -> None:
        if label is None:
            return
        with self.label_lock:
            self.completed_labels.setdefault(label, []).append(
                CompletedChild(run_id=run_id, engine=engine, resumable=resumable)
            )

    def get_prior_child(self, prior_label: str) -> CompletedChild:
        with self.label_lock:
            entries = self.completed_labels.get(prior_label)
            if not entries:
                known = sorted(self.completed_labels.keys())
                known_str = ", ".join(repr(k) for k in known) if known else "none"
                raise ValueError(
                    f"unknown or incomplete prior child label {prior_label!r}; "
                    f"known completed labels: {known_str}"
                )
            if len(entries) > 1:
                raise ValueError(
                    f"duplicate completed children with label {prior_label!r}; "
                    "labels must be unique to be referenced by followup()"
                )
            child = entries[0]
        if not child.resumable:
            raise ValueError(
                f"prior child with label {prior_label!r} was not launched with resumable=True; "
                "add resumable=True to the prior agent() call to enable followup()"
            )
        return child


def _global_agent_cap() -> int:
    cpus = os.cpu_count() or 2
    return min(16, max(2, cpus - 2))


def _workflow_config(config: JsonObject) -> JsonObject:
    value = config.get("workflows")
    return value if isinstance(value, dict) else {}


def _budget_remaining_json(budget: Budget) -> int | None:
    remaining = budget.remaining()
    if remaining == float("inf"):
        return None
    return int(remaining)


def _engine_semaphores(config: JsonObject) -> dict[str, threading.Semaphore]:
    caps = _workflow_config(config).get("engineCaps")
    if not isinstance(caps, dict):
        return {}
    result: dict[str, threading.Semaphore] = {}
    for engine, cap in caps.items():
        if engine in KNOWN_ENGINES and isinstance(cap, int) and cap > 0:
            result[engine] = threading.Semaphore(cap)
    return result


def _item_thread_cap(config: JsonObject) -> int:
    value = _workflow_config(config).get("itemThreads", DEFAULT_ITEM_THREADS)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_ITEM_THREADS


def _validate_soft_park_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("soft-park item names must be non-empty strings")
    if "/" in name:
        raise ValueError("soft-park item names must not contain '/'")


def _structured_retries(config: JsonObject) -> int:
    value = _workflow_config(config).get("structuredOutputRetries", DEFAULT_STRUCTURED_RETRIES)
    if isinstance(value, int) and value >= 0:
        return value
    return DEFAULT_STRUCTURED_RETRIES


def execute_workflow(state: WorkflowState) -> object:
    source = state.script_path.read_text(encoding="utf-8")
    code = workflow_script.compile_workflow(source, filename=str(state.script_path))
    meta = workflow_script.parse_meta(source, filename=str(state.script_path))
    dsl = WorkflowDsl(state, meta)
    globals_dict: dict[str, object] = {
        "agent": dsl.agent,
        "followup": dsl.followup,
        "pipeline": dsl.pipeline,
        "soft_park": dsl.soft_park,
        "park_item": dsl.park_item,
        "soft_park_item": dsl.park_item,
        "item_park": dsl.park_item,
        "park": dsl.park_item,
        "soft_park_request": dsl.soft_park_request,
        "parked": dsl.is_soft_parked,
        "SoftPark": SoftPark,
        "parallel": dsl.parallel,
        "phase": dsl.phase,
        "log": dsl.log,
        "workflow": dsl.workflow,
        "reject": dsl.reject,
        "structured_attempt": dsl.structured_attempt,
        "judges": dsl.judges,
        "args": state.args,
        "budget": state.budget,
        "dry_run": state.dry_run,
        "is_dry_run": state.dry_run,
    }
    exec(code, globals_dict)
    return globals_dict["__delegate_workflow__"]()


class WorkflowDsl:
    def __init__(self, state: WorkflowState, meta: workflow_script.WorkflowMeta) -> None:
        self.state = state
        self.meta = meta
        defaults = meta.get("defaults")
        self.defaults = defaults if isinstance(defaults, dict) else {}
        self.current_phase: str | None = None
        self._structured_attempts: dict[str, StructuredAttemptOutcome] = {}
        self._structured_attempt_lock = threading.Lock()
        self._soft_park_seen: set[str] = set()

    def _record_structured_attempt(
        self, key: str, outcome: StructuredAttemptOutcome | None
    ) -> None:
        lock = getattr(self, "_structured_attempt_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._structured_attempt_lock = lock
        attempts = getattr(self, "_structured_attempts", None)
        if attempts is None:
            attempts = {}
            self._structured_attempts = attempts
        with lock:
            if outcome is None:
                attempts.pop(key, None)
            else:
                attempts[key] = outcome
        self.state.thread_local.last_structured_attempt_key = key

    def structured_attempt(self, key_or_label: object | None = None) -> JsonObject | None:
        """Return the latest exhausted structured attempt for this workflow call."""
        if key_or_label is None:
            key_or_label = getattr(self.state.thread_local, "last_structured_attempt_key", None)
        if not isinstance(key_or_label, str) or not key_or_label.strip():
            return None
        key = key_or_label
        if key not in getattr(self, "_structured_attempts", {}):
            try:
                key, _label = self.state.resolve_agent_key(key_or_label)
            except ValueError:
                return None
        lock = getattr(self, "_structured_attempt_lock", None)
        if lock is None:
            return None
        with lock:
            outcome = getattr(self, "_structured_attempts", {}).get(key)
        if outcome is not None:
            return outcome.as_json()
        with self.state.journal_lock:
            for event in reversed(registry.iter_journal(self.state.journal_path)):
                if event.get("key") != key:
                    continue
                if event.get("type") == "agent_structured_exhausted":
                    return {
                        "lastParsedCandidate": event.get("lastParsedCandidate"),
                        "validationError": event.get("validationError", ""),
                    }
                if event.get("type") == "agent_finished":
                    if event.get("exhausted") is True and event.get("result") is None:
                        continue
                    return None
                if event.get("type") == "agent_rejected":
                    return None
        return None

    def phase(self, title: str) -> None:
        self.current_phase = str(title)
        self.state.append_event("phase", phase=self.current_phase)

    def log(self, message: object) -> None:
        self.state.append_event("log", message=str(message))

    def reject(self, key_or_label: object, reason: object) -> None:
        """Invalidate an agent result explicitly; emitters own this policy."""
        self.state.reject_agent(key_or_label, reason)

    def park_item(self, name: object, result: JsonValue = None) -> None:
        self.state.park_item(name, result)

    def soft_park_request(self, name: object, result: JsonValue = None) -> _SoftParkRequest:
        return self.state.soft_park_request(name, result)

    def is_soft_parked(self, name: object) -> bool:
        return self.state.is_soft_parked(name)

    def _soft_park_entries(
        self, items: object, worker: Callable[[object, str, int], object] | None
    ) -> list[tuple[str, object, Callable[[], object]]]:
        raw: list[tuple[str, object, Callable[[], object]]] = []

        def add(name: object, value: object, callback: Callable[[], object]) -> None:
            if not isinstance(name, str):
                raise ValueError("soft_park() item names must be non-empty strings")
            _validate_soft_park_name(name)
            raw.append((name, value, callback))

        if isinstance(items, str) and callable(worker):
            add(items, None, lambda: worker(None, items, 0))
        elif isinstance(items, dict):
            for name, value in items.items():
                if worker is None:
                    if not callable(value):
                        raise TypeError("soft_park() mapping values must be callables")
                    add(name, value, value)
                else:
                    add(name, value, lambda value=value, name=name: worker(value, name, 0))
        elif isinstance(items, (list, tuple)):
            if (
                isinstance(items, tuple)
                and len(items) == 2
                and isinstance(items[0], str)
                and callable(items[1])
            ):
                add(items[0], items[1], items[1])
            else:
                for index, item in enumerate(items):
                    if isinstance(item, str) and worker is not None:
                        add(
                            item,
                            item,
                            lambda item=item, index=index: worker(item, item, index),
                        )
                        continue
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        name, value = item
                        if not isinstance(name, str):
                            raise ValueError("soft_park() item names must be non-empty strings")
                        if worker is None:
                            if not callable(value):
                                raise TypeError("soft_park() tuple values must be callables")
                            add(name, value, value)
                        else:
                            add(
                                name,
                                value,
                                lambda value=value, name=name, index=index: worker(
                                    value, name, index
                                ),
                            )
                        continue
                    if isinstance(item, dict):
                        name = item.get("name")
                        if not isinstance(name, str):
                            raise ValueError("soft_park() item dictionaries require a name")
                        callback = next(
                            (
                                item.get(key)
                                for key in ("run", "thunk", "callback", "work", "fn")
                                if callable(item.get(key))
                            ),
                            None,
                        )
                        if worker is None and callback is None:
                            raise TypeError("soft_park() item dictionaries require a callable")
                        if worker is None:
                            add(name, item, callback)
                        else:
                            add(
                                name,
                                item,
                                lambda item=item, name=name, index=index: worker(item, name, index),
                            )
                        continue
                    if worker is None:
                        raise TypeError(
                            "soft_park() items must be (name, callable) pairs or named dictionaries"
                        )
                    raise ValueError("soft_park() items require an explicit stable name")
        else:
            raise TypeError("soft_park() expects a mapping or named item list")

        if not raw:
            return []
        if len(raw) > workflow_script.ITEM_LIMIT:
            raise ValueError(f"soft_park() item limit is {workflow_script.ITEM_LIMIT}")
        seen: set[str] = set()
        for name, _value, _callback in raw:
            if name in seen or name in self._soft_park_seen:
                raise ValueError(f"soft_park() item name must be unique: {name!r}")
            existing_scope = self.state.soft_park_scopes.get(name)
            candidate_scope = f"{self.state.namespace}/soft-park/{name}"
            if existing_scope is not None and existing_scope != candidate_scope:
                raise ValueError(f"soft_park() item name must be unique: {name!r}")
            seen.add(name)
        self._soft_park_seen.update(seen)
        return raw

    def soft_park(
        self,
        items: object,
        worker: Callable[[object, str, int], object] | None = None,
    ) -> list[object]:
        """Dynamically admit named items and unwind parked workers.

        ``items`` is a mapping of stable names to zero-argument callbacks, or
        a list of ``(name, callback)`` pairs.  A callback can call
        ``park_item(name, payload)`` (or return ``soft_park_request(...)``) to
        durably park itself.  The worker exits immediately, releasing its item
        slot; the supervisor parks only after all unrelated admitted work drains.
        On resume the script is replayed and the same named scope is reused.
        """
        entries = self._soft_park_entries(items, worker)
        if not entries:
            return []
        results: list[object] = [None] * len(entries)
        finished: queue.Queue[tuple[int, object, _SoftParkRequest | None, GateExit | None]] = (
            queue.Queue()
        )
        active: dict[int, threading.Thread] = {}
        gate_errors: list[GateExit] = []
        watchdog_errors: list[SupervisorWatchdogExit] = []
        validation_errors: list[ValueError] = []
        bypass_item_cap = self.state.inside_item_thread()
        cap = _item_thread_cap(self.state.config)
        owner_scope = self.state.current_scope()

        def run_item(
            index: int,
            name: str,
            value: object,
            callback: Callable[[], object],
            scope: str,
        ) -> None:
            request: _SoftParkRequest | None = None
            gate_error: GateExit | None = None
            with self.state.item_slot(bypass=bypass_item_cap), self.state.scope(scope):
                try:
                    result = callback()
                    if isinstance(result, _SoftParkRequest):
                        request = result
                        result = None
                        if request.name == name:
                            self.state.record_item_park(
                                name,
                                scope=scope,
                                owner_scope=owner_scope,
                                result=request.result,
                            )
                    results[index] = result
                except _SoftParkRequest as exc:
                    request = exc
                    if request.name == name:
                        self.state.record_item_park(
                            name,
                            scope=scope,
                            owner_scope=owner_scope,
                            result=request.result,
                        )
                except GateExit as exc:
                    gate_error = exc
                except SupervisorWatchdogExit as exc:
                    watchdog_errors.append(exc)
                except Exception as exc:
                    self.state.append_event(
                        "soft_park_item_failed",
                        scope=self.state.current_scope(),
                        name=name,
                        error=str(exc),
                    )
            finished.put((index, value, request, gate_error))

        next_index = 0
        while next_index < len(entries) or active:
            while (
                next_index < len(entries)
                and len(active) < cap
                and not gate_errors
                and not watchdog_errors
            ):
                index = next_index
                name, value, callback = entries[index]
                scope = self.state.soft_park_scope(name)
                thread = threading.Thread(
                    target=run_item,
                    args=(index, name, value, callback, scope),
                    daemon=self.state.dry_run,
                )
                thread.start()
                active[index] = thread
                next_index += 1
            if not active:
                break
            index, _value, request, gate_error = finished.get()
            active.pop(index, None)
            name = entries[index][0]
            scope = self.state.soft_park_scope(name)
            if gate_error is not None:
                gate_errors.append(gate_error)
                continue
            if request is not None:
                if request.name != name:
                    validation_errors.append(
                        ValueError(
                            f"soft_park() callback requested {request.name!r}, expected {name!r}"
                        )
                    )
                else:
                    self.state.record_item_park(
                        name,
                        scope=scope,
                        owner_scope=owner_scope,
                        result=request.result,
                    )
            elif name in self.state.soft_parked_items:
                self.state.record_item_unpark(name, scope=scope)

        for thread in tuple(active.values()):
            thread.join()
        if watchdog_errors:
            raise watchdog_errors[0]
        if validation_errors:
            raise validation_errors[0]
        if gate_errors:
            self.state.ensure_gate_durable(gate_errors[0])
            raise gate_errors[0]
        outstanding = tuple(
            sorted(
                name for name, _value, _callback in entries if name in self.state.soft_parked_items
            )
        )
        if outstanding:
            raise SoftParkExit(outstanding)
        return results

    def pipeline(
        self, items: list[object], *stages: Callable[[object, object, int], object]
    ) -> list[object]:
        if not isinstance(items, list):
            raise TypeError("pipeline() expects an array")
        if len(items) > workflow_script.ITEM_LIMIT:
            raise ValueError("pipeline() item limit is 4096")
        if any(not callable(stage) for stage in stages):
            raise TypeError("pipeline() stages must be functions")
        base_scope = self.state.next_child_scope("pipeline")
        results: list[object] = [None] * len(items)
        threads: list[threading.Thread] = []
        bypass_item_cap = self.state.inside_item_thread()
        gate_errors: list[GateExit] = []
        soft_park_errors: list[SoftParkExit | _SoftParkRequest] = []
        start_barrier = (
            threading.Barrier(len(items) + 1)
            if not bypass_item_cap and len(items) <= _item_thread_cap(self.state.config)
            else None
        )

        def run_item(index: int, item: object, pre_acquired: bool) -> None:
            # Nested primitives bypass the item-thread cap so an outer callback
            # cannot hold every slot while waiting for its child item threads.
            with self.state.item_slot(bypass=bypass_item_cap, pre_acquired=pre_acquired):
                if start_barrier is not None:
                    start_barrier.wait()
                previous = item
                with self.state.scope(f"{base_scope}/item#{index}"):
                    for stage_index, stage in enumerate(stages):
                        with self.state.scope(f"{base_scope}/item#{index}/stage#{stage_index}"):
                            try:
                                previous = stage(previous, item, index)
                            except BudgetExceeded:
                                previous = None
                                break
                            except SupervisorWatchdogExit:
                                raise
                            except (SoftParkExit, _SoftParkRequest) as exc:
                                soft_park_errors.append(exc)
                                previous = None
                                break
                            except GateExit as exc:
                                gate_errors.append(exc)
                                previous = None
                                break
                            except Exception as exc:
                                self.state.append_event(
                                    "stage_failed",
                                    scope=self.state.current_scope(),
                                    error=str(exc),
                                )
                                previous = None
                                break
                            if previous is None:
                                break
                results[index] = previous

        for index, item in enumerate(items):
            if self.state.gate_state["stop_admitting"] or gate_errors:
                # Gate already closed: short-circuit remaining items without
                # spawning threads that would only block then die on admission.
                break
            pre_acquired = False
            if not bypass_item_cap:
                self.state.item_semaphore.acquire()
                pre_acquired = True
                # Re-check after acquire: with a tight item-thread cap the gate
                # may have closed while we were blocked on the semaphore.
                if self.state.gate_state["stop_admitting"] or gate_errors:
                    self.state.item_semaphore.release()
                    break
            thread = threading.Thread(
                target=run_item, args=(index, item, pre_acquired), daemon=self.state.dry_run
            )
            try:
                thread.start()
            except BaseException:
                if pre_acquired:
                    self.state.item_semaphore.release()
                raise
            threads.append(thread)
        if start_barrier is not None:
            start_barrier.wait()
        for thread in threads:
            thread.join()
        if self.state.cancel_event.is_set():
            raise SupervisorWatchdogExit("cancellation requested during pipeline")
        if soft_park_errors:
            raise soft_park_errors[0]
        if gate_errors:
            self.state.ensure_gate_durable(gate_errors[0])
            raise gate_errors[0]
        if self.state.gate_state["stop_admitting"]:
            raise self.state.closed_gate_exit()
        return results

    def parallel(self, thunks: list[Callable[[], object]]) -> list[object]:
        if not isinstance(thunks, list):
            raise TypeError("parallel() expects an array")
        if len(thunks) > workflow_script.ITEM_LIMIT:
            raise ValueError("parallel() item limit is 4096")
        if any(not callable(thunk) for thunk in thunks):
            raise TypeError("parallel() items must be functions")
        if self.state.dry_run:
            # Dry-run output is a plan, not concurrent execution.  Preserve
            # source order so the reported call list is deterministic.
            results: list[object] = []
            for thunk in thunks:
                try:
                    results.append(thunk())
                except (SoftParkExit, _SoftParkRequest):
                    raise
                except (BudgetExceeded, GateExit):
                    results.append(None)
            return results
        base_scope = self.state.next_child_scope("parallel")
        results: list[object] = [None] * len(thunks)
        threads: list[threading.Thread] = []
        bypass_item_cap = self.state.inside_item_thread()
        gate_errors: list[GateExit] = []
        soft_park_errors: list[SoftParkExit | _SoftParkRequest] = []
        start_barrier = (
            threading.Barrier(len(thunks) + 1)
            if not bypass_item_cap and len(thunks) <= _item_thread_cap(self.state.config)
            else None
        )

        def run_thunk(index: int, thunk: Callable[[], object], pre_acquired: bool) -> None:
            with (
                self.state.item_slot(bypass=bypass_item_cap, pre_acquired=pre_acquired),
                self.state.scope(f"{base_scope}/thunk#{index}"),
            ):
                try:
                    if start_barrier is not None:
                        start_barrier.wait()
                    results[index] = thunk()
                except BudgetExceeded:
                    results[index] = None
                except SupervisorWatchdogExit:
                    raise
                except (SoftParkExit, _SoftParkRequest) as exc:
                    soft_park_errors.append(exc)
                    results[index] = None
                except GateExit as exc:
                    gate_errors.append(exc)
                    results[index] = None
                except Exception as exc:
                    self.state.append_event(
                        "thunk_failed",
                        scope=self.state.current_scope(),
                        error=str(exc),
                    )
                    results[index] = None

        for index, thunk in enumerate(thunks):
            if self.state.gate_state["stop_admitting"] or gate_errors:
                break
            pre_acquired = False
            if not bypass_item_cap:
                self.state.item_semaphore.acquire()
                pre_acquired = True
                if self.state.gate_state["stop_admitting"] or gate_errors:
                    self.state.item_semaphore.release()
                    break
            thread = threading.Thread(
                target=run_thunk, args=(index, thunk, pre_acquired), daemon=self.state.dry_run
            )
            try:
                thread.start()
            except BaseException:
                if pre_acquired:
                    self.state.item_semaphore.release()
                raise
            threads.append(thread)
        if start_barrier is not None:
            start_barrier.wait()
        for thread in threads:
            thread.join()
        if self.state.cancel_event.is_set():
            raise SupervisorWatchdogExit("cancellation requested during parallel")
        if soft_park_errors:
            raise soft_park_errors[0]
        if gate_errors:
            self.state.ensure_gate_durable(gate_errors[0])
            raise gate_errors[0]
        if self.state.gate_state["stop_admitting"]:
            raise self.state.closed_gate_exit()
        return results

    def judges(
        self,
        prompt: str,
        schema: JsonObject,
        engines: list[str | JsonObject] | None = None,
        *,
        effort: str | None = None,
    ) -> list[object]:
        effort = _validate_workflow_effort(effort)
        selected = engines or ["codex"]
        thunks = []
        for item in selected:
            engine, model, item_effort = _parse_engine_spec(item)
            judge_effort = item_effort if item_effort is not None else effort
            thunks.append(
                lambda engine=engine, model=model, judge_effort=judge_effort: self.agent(
                    prompt,
                    engine=engine,
                    model=model,
                    effort=judge_effort,
                    mode=MODE_CALL,
                    schema=schema,
                    label=f"judge:{engine if model is None else model}",
                )
            )
        return self.parallel(thunks)

    def workflow(
        self, name_or_path: str, args: JsonValue = None, gate: bool | str = False
    ) -> object:
        if self.state.depth >= 3:
            raise RuntimeError("workflow nesting depth exceeded 3")
        child_path = resolve_workflow_reference(name_or_path, self.state.script_path.parent)
        child_source = workflow_script.read_script(child_path)
        workflow_script.check_source(child_source, filename=str(child_path))
        name = Path(name_or_path).stem
        scope = self.state.next_child_scope(f"wf:{name}")
        if self.state.dry_run:
            self.state.dry_runs.append(
                {
                    "scope": scope,
                    "workflow": name,
                    "mode": "workflow",
                    "phase": self.current_phase,
                    "gate": gate,
                }
            )
            self.state.append_event("workflow_stubbed", scope=scope, child=name, gate=gate)
            return None
        child_state = WorkflowState(
            wf_id=self.state.wf_id,
            workspace=self.state.workspace,
            root=self.state.root,
            script_path=child_path,
            config=self.state.config,
            cli_argv=self.state.cli_argv,
            args=args,
            budget=self.state.budget,
            dry_run=self.state.dry_run,
            # Inherited, not defaulted. status.json is rebuilt rather than
            # merged, so any field a child state forgets is not merely absent
            # from the child -- it is ERASED from the file for everyone. A gated
            # sub-workflow was writing `paused` with notify null, silently losing
            # the target the parent was launched with, and replay_journal was
            # taking the same path back to its default and quietly re-enabling
            # journal replay after a dry run.
            replay_journal=self.state.replay_journal,
            notify_target=self.state.notify_target,
            depth=self.state.depth + 1,
            namespace=scope,
            replay=self.state.replay,
            replay_keys=self.state.replay_keys,
            started_without_result=self.state.started_without_result,
            claimed_keys=self.state.claimed_keys,
            failed_replay_keys=self.state.failed_replay_keys,
            tombstoned_keys=self.state.tombstoned_keys,
            started_after_tombstone=self.state.started_after_tombstone,
            label_keys=self.state.label_keys,
            started_scopes=self.state.started_scopes,
            lifetime_counter=self.state.lifetime_counter,
            gate_state=self.state.gate_state,
            replay_attempt=self.state.replay_attempt,
            workflow_key_version=self.state.workflow_key_version,
            cancel_event=self.state.cancel_event,
            retry_worktree_runs=self.state.retry_worktree_runs,
            pending_gate=self.state.pending_gate,
            soft_parked_items=self.state.soft_parked_items,
            soft_park_scopes=self.state.soft_park_scopes,
        )
        child_state.journal_lock = self.state.journal_lock
        child_state.scope_lock = self.state.scope_lock
        child_state.lifetime_lock = self.state.lifetime_lock
        child_state.gate_condition = self.state.gate_condition
        child_state.agent_semaphore = self.state.agent_semaphore
        child_state.engine_semaphores = self.state.engine_semaphores
        child_state.item_semaphore = self.state.item_semaphore
        child_state.thread_local.item_depth = getattr(self.state.thread_local, "item_depth", 0)
        result = execute_workflow(child_state)
        should_gate = gate is True or (gate == "on-failure" and _gate_failed(result))
        if should_gate:
            gate_key = _stable_hash(f"gate:{scope}:{_canonical_json(args)}")
            result_hash = _gate_result_hash(result)
            approved = registry.approval_allows(self.state.root, gate_key, result_hash)
            if not approved:
                # Let sibling pipeline/parallel callbacks that were admitted in
                # the same wave enter their child seam before stop_admitting
                # becomes visible.  This closes the race where a fast gate
                # callback otherwise parks before a concurrently-started item
                # has incremented active_agent.
                time.sleep(0.01)
                raise self.state.park_gate(gate_key, child=name, result=result)
        return result

    def agent(
        self,
        prompt: str,
        engine: str | list[str] | None = None,
        mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        schema: JsonObject | None = None,
        label: str | None = None,
        phase: str | None = None,
        isolation: str | None = None,
        passthrough: bool = False,
        timeout: int | float | None = None,
        retries: int | None = None,
        fast: bool | None = None,
        persona: str | None = None,
        allow_repo_persona: bool = False,
        resumable: bool = False,
    ) -> JsonValue:
        if not isinstance(prompt, str):
            prompt = str(prompt)
        engines = _engine_chain(engine or self.defaults.get("engine") or DEFAULT_ENGINE)
        resolved_mode = mode or self.defaults.get("mode") or DEFAULT_MODE
        if passthrough and resolved_mode == MODE_CALL:
            raise ValueError(
                "passthrough=True with mode='call' is invalid; slash pass-through "
                "needs a work or argv-enforced-safe lane"
            )
        if passthrough and schema is not None:
            raise ValueError("passthrough=True is mutually exclusive with schema=")
        if not isinstance(resumable, bool):
            raise ValueError("resumable must be a boolean")
        if resumable and resolved_mode == MODE_CALL:
            raise ValueError(
                "resumable=True with mode='call' is invalid; call mode runs execute in a "
                "throwaway workspace and cannot be followed up"
            )
        if resumable and resolved_mode == MODE_SAFE:
            raise ValueError(
                "resumable=True with mode='safe' is invalid; safe workspaces are temporary "
                "and a captured session would have no re-entry path"
            )
        if resumable and any(candidate not in {"codex", "claude"} for candidate in engines):
            raise ValueError(
                f"resumable=True is only supported by codex and claude; "
                f"{', '.join(e for e in engines if e not in {'codex', 'claude'})} does not support "
                "native session resumption"
            )
        resolved_model = model or self.defaults.get("model")
        resolved_effort = _validate_workflow_effort(
            effort if effort is not None else self.defaults.get("effort")
        )
        resolved_fast = fast if fast is not None else self.defaults.get("fast")
        if persona is not None and (not isinstance(persona, str) or not persona.strip()):
            raise ValueError("persona must be a non-empty string or None")
        persona_resolution = None
        if persona is not None:
            if resolved_mode == MODE_CALL:
                raise ValueError("personas are not supported for call mode")
            if passthrough:
                raise ValueError("personas are not supported with passthrough=True")
            persona_resolution = personas.resolve_persona(
                self.state.workspace,
                persona,
                mode=resolved_mode,
                allow_repo_persona=allow_repo_persona,
            )
        if resolved_fast is not None and not isinstance(resolved_fast, bool):
            raise ValueError("fast must be true, false, or None")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not timeout > 0
        ):
            raise ValueError("timeout must be a positive number of seconds")
        resolved_isolation = isolation or self.defaults.get("isolation")
        resolved_phase = phase or self.current_phase
        opts = {
            "engine": engines if len(engines) > 1 else engines[0],
            "mode": resolved_mode,
            "model": resolved_model,
            "effort": resolved_effort,
            "fast": resolved_fast,
            "schema": schema,
            "isolation": resolved_isolation,
            "personaDigest": persona_resolution.digest if persona_resolution is not None else None,
        }
        if resumable:
            opts["resumable"] = True
        if timeout is not None:
            opts["timeout"] = timeout
        path = self.state.next_agent_path()
        key_version = 2 if self.state.workflow_key_version == 2 else 1
        key_opts = dict(opts)
        if key_version == 1:
            # v1 cache keys intentionally omit timeout.  The timeout is
            # execution metadata, not structural identity; changing it must
            # not fork a paused production workflow.
            key_opts.pop("timeout", None)
        base_key = _agent_key(path, prompt, key_opts, version=key_version)
        key = base_key
        if key_version == 2 and base_key in self.state.tombstoned_keys:
            retry_opts = dict(key_opts)
            retry_opts["retryAttempt"] = max(self.state.replay_attempt, 1)
            key = _agent_key(path, prompt, retry_opts, version=key_version)
        if label is not None:
            self.state.label_keys[label] = key
        self.state.thread_local.last_structured_attempt_key = key
        if key_version == 2:
            # A v2 timeout change intentionally creates a fresh key.  Cancel
            # any unfinished child from the same structural scope before the
            # replacement launches, so its temporary worktree cannot leak.
            self.state.cancel_stale_scope_children(path, key)
        # reject() mutates the replay maps under journal_lock.  Take one
        # coherent decision so a concurrent cache hit cannot observe a key in
        # replay_keys after reject has popped its value.
        with self.state.journal_lock:
            cached = (
                key in self.state.replay_keys
                and key not in self.state.tombstoned_keys
                and key in self.state.replay
            )
            cached_result = self.state.replay.get(key)
            adoptable_started = key in self.state.started_without_result and (
                key not in self.state.tombstoned_keys or key in self.state.started_after_tombstone
            )
        if cached:
            result = cached_result
            if key in self.state.exhausted_keys:
                self.state.exhausted_keys.discard(key)
                adopted = self._adopt_existing_agent_run(
                    key,
                    scope=path,
                    label=label,
                    phase=resolved_phase,
                    schema=schema,
                    prefer_assistant=schema is not None,
                    timeout=timeout,
                )
                if adopted is not _MISSING and adopted is not None:
                    self.state.replay[key] = adopted
                    self.state.append_event(
                        "agent_finished",
                        key=key,
                        scope=path,
                        result=adopted,
                        adopted=True,
                    )
                    return adopted
            self.state.append_event(
                "agent_cache_hit",
                key=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                result=result,
            )
            return result
        if adoptable_started:
            adopted = self._adopt_existing_agent_run(
                key,
                scope=path,
                label=label,
                phase=resolved_phase,
                schema=schema,
                prefer_assistant=schema is not None,
                timeout=timeout,
            )
            if adopted is not _MISSING:
                self.state.replay_keys.add(key)
                self.state.replay[key] = adopted
                self.state.started_without_result.discard(key)
                self.state.append_event(
                    "agent_finished",
                    key=key,
                    scope=path,
                    result=adopted,
                    adopted=True,
                )
                return adopted
            # Failed/cancelled/unparseable/schema-invalid: keep the key in
            # started_without_result and fall through to a live respawn.
        already_claimed = key in self.state.claimed_keys
        if not already_claimed:
            self.state.claim_agent_lifetime()
        if self.state.dry_run:
            if already_claimed:
                spent = self.state.dry_run_budget_spent
            else:
                spent = self.state.dry_run_budget_tick()
                self.state.claimed_keys.add(key)
            remaining = (
                None if self.state.budget.total is None else max(self.state.budget.total - spent, 0)
            )
            self.state.append_event(
                "budget",
                key=key,
                spent=spent,
                total=self.state.budget.total,
                remaining=remaining,
                simulated=True,
            )
            placeholder = workflow_schema.placeholder(schema) if schema else ""
            persona_bytes = persona_resolution.size_bytes if persona_resolution is not None else 0
            prompt_bytes = len(prompt.encode("utf-8")) + persona_bytes
            dry_run_entry = {
                "scope": path,
                "engine": engines,
                "mode": resolved_mode,
                "model": resolved_model,
                "effort": resolved_effort,
                "fast": resolved_fast,
                "isolation": resolved_isolation,
                "promptBytes": prompt_bytes,
                "phase": resolved_phase,
                "label": label,
                "schema": bool(schema),
            }
            if resumable:
                dry_run_entry["resumable"] = True
            if persona_resolution is not None:
                dry_run_entry.update(
                    persona=persona_resolution.name,
                    personaSource=persona_resolution.source,
                    personaDigest=persona_resolution.digest,
                    personaBytes=persona_resolution.size_bytes,
                )
            constrained = [item for item in engines if item in ENGINE_ARGV_TRANSPORT]
            if constrained and prompt_bytes > PROMPT_ARGV_GUARD_BYTES:
                dry_run_entry["warnings"] = [
                    f"prompt is {prompt_bytes} UTF-8 bytes, exceeding the "
                    f"{PROMPT_ARGV_GUARD_BYTES}-byte argv transport limit for "
                    f"{'/'.join(constrained)}; route this stage to "
                    "codex/claude/droid/opencode"
                ]
            self.state.dry_runs.append(dry_run_entry)
            self.state.append_event(
                "agent_started",
                key=key,
                scope=path,
                dryRun=True,
                simulated=True,
                personaDigest=persona_resolution.digest if persona_resolution is not None else None,
                personaSource=persona_resolution.source if persona_resolution is not None else None,
            )
            self.state.append_event(
                "agent_finished", key=key, scope=path, result=placeholder, simulated=True
            )
            self.state.tombstoned_keys.discard(key)
            if label is not None:
                self.state.record_completed_child(
                    label=label,
                    run_id=f"dry_run_{key}",
                    engine=engines[0] if engines else "codex",
                    resumable=resumable,
                )
            return placeholder
        if already_claimed:
            spent = self.state.budget.spent()
        else:
            spent = self.state.budget.claim()
            self.state.claimed_keys.add(key)
        self.state.append_event(
            "budget",
            key=key,
            spent=spent,
            total=self.state.budget.total,
            remaining=_budget_remaining_json(self.state.budget),
        )
        with self.state.active_agent():
            if key in self.state.tombstoned_keys:
                self.state.started_after_tombstone.add(key)
            self.state.append_event(
                "agent_started",
                key=key,
                workflowAgentKey=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                engine=engines,
                mode=resolved_mode,
                personaDigest=persona_resolution.digest if persona_resolution is not None else None,
                personaSource=persona_resolution.source if persona_resolution is not None else None,
            )
            for candidate in engines:
                try:
                    result = self._run_agent_attempts(
                        candidate,
                        prompt,
                        key=key,
                        label=label,
                        mode=resolved_mode,
                        model=resolved_model,
                        effort=resolved_effort,
                        fast=resolved_fast,
                        schema=schema,
                        isolation=resolved_isolation,
                        passthrough=passthrough,
                        timeout=timeout,
                        retries=retries,
                        persona=persona_resolution,
                        allow_repo_persona=allow_repo_persona,
                        resumable=resumable,
                    )
                except SupervisorWatchdogExit:
                    raise
                except PersonaDigestMismatch:
                    raise
                except Exception as exc:
                    self.state.append_event(
                        "agent_failed",
                        key=key,
                        scope=path,
                        engine=candidate,
                        error=str(exc),
                    )
                    result = None
                if result is not None:
                    self.state.tombstoned_keys.discard(key)
                    self.state.replay_keys.add(key)
                    self.state.replay[key] = result
                    self.state.append_event(
                        "agent_finished",
                        key=key,
                        scope=path,
                        engine=candidate,
                        result=result,
                    )
                    run_id = getattr(self.state.thread_local, "last_run_id", None)
                    if label is not None and isinstance(run_id, str):
                        self.state.record_completed_child(label, run_id, candidate, resumable)
                    return result
            self.state.replay_keys.add(key)
            self.state.replay[key] = None
            self.state.append_event(
                "agent_finished", key=key, scope=path, result=None, exhausted=True
            )
            return None

    def _adopt_existing_agent_run(
        self,
        key: str,
        *,
        scope: str,
        label: str | None = None,
        phase: str | None,
        schema: JsonObject | None,
        prefer_assistant: bool,
        timeout: int | float | None,
    ) -> JsonValue | _MissingType:
        run_id = _find_workflow_agent_run(self.state.workspace, self.state.wf_id, key)
        if run_id is None:
            return _MISSING
        if not _workflow_run_terminal(self.state.workspace, run_id):
            waited = _wait_for_workflow_agent_run(self.state.workspace, run_id, timeout)
            if not waited and not _workflow_run_terminal(self.state.workspace, run_id):
                # Match live-path timeout: cancel the child; timeout is definitive.
                # The terminal re-check closes the race where the child finished
                # between the wait deadline and the cancel — adopt that instead.
                cancel_workflow_agent_child(self.state.workspace, self.state.wf_id, key)
                # The adoption path recorded key/scope/runId but not the label a
                # human reads, nor the bound that expired, and it never notified
                # at all -- so a lane adopted from a prior run could time out in
                # silence while the live path rang. The two sites now carry every
                # identifier each can honestly produce; `engine` belongs to the
                # adopted run rather than this call, so it stays off this row
                # instead of being guessed.
                self.state.append_event(
                    "agent_timeout",
                    key=key,
                    scope=scope,
                    runId=run_id,
                    label=label,
                    timeout=timeout,
                )
                self.state.notify_event(
                    "agent_timeout", detail=f"{label or key} (adopted run {run_id})"
                )
                _cleanup_workflow_agent_run_workspace(self.state.workspace, run_id)
                return None
        text = _workflow_agent_run_result(
            self.state.workspace,
            run_id,
            prefer_assistant=prefer_assistant,
        )
        if text is None:
            # Failed/cancelled/unparseable children are not definitive — respawn.
            _cleanup_workflow_agent_run_workspace(self.state.workspace, run_id)
            return _MISSING
        if schema is None:
            result: JsonValue = text
        else:
            try:
                value = workflow_schema.parse_json_tolerant(text, schema)
                workflow_schema.validate_value(value, schema)
                result = value
            # Child output is untrusted; parse/validation blowups must not kill the supervisor.
            except Exception as exc:
                self.state.append_event(
                    "agent_adopt_rejected",
                    key=key,
                    scope=scope,
                    runId=run_id,
                    error=str(exc),
                )
                _cleanup_workflow_agent_run_workspace(self.state.workspace, run_id)
                return _MISSING
        _cleanup_workflow_agent_run_workspace(self.state.workspace, run_id)
        resumable = _workflow_agent_run_resumable(self.state.workspace, run_id)
        self._emit_adopted_child_identity(run_id, key=key, label=label, resumable=resumable)
        if label is not None:
            engine = _workflow_agent_run_engine(self.state.workspace, run_id) or "codex"
            self.state.record_completed_child(label, run_id, engine, resumable)
        self.state.append_event(
            "agent_adopted",
            key=key,
            workflowAgentKey=key,
            scope=scope,
            label=label,
            phase=phase,
            runId=run_id,
            result=result,
        )
        return result

    def _emit_adopted_child_identity(
        self, run_id: str, *, key: str, label: str | None, resumable: bool = False
    ) -> None:
        if _workflow_agent_child_event_exists(self.state.journal_path, run_id, key):
            return
        engine = _workflow_agent_run_engine(self.state.workspace, run_id)
        if engine is None:
            return
        event: JsonObject = {
            "engine": engine,
            "key": key,
            "workflowAgentKey": key,
            "runId": run_id,
        }
        if label is not None:
            event["label"] = label
        if resumable:
            event["resumable"] = True
        self.state.append_event("agent_child", **event)

    def _run_agent_attempts(
        self,
        engine: str,
        prompt: str,
        *,
        key: str,
        label: str | None = None,
        mode: str,
        model: str | None,
        effort: str | None,
        fast: bool | None,
        schema: JsonObject | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        retries: int | None,
        persona: personas.PersonaResolution | None = None,
        allow_repo_persona: bool = False,
        resumable: bool = False,
    ) -> JsonValue:
        if engine not in KNOWN_ENGINES:
            raise ValueError(f"engine must be one of {', '.join(KNOWN_ENGINES)}")
        if mode == MODE_SAFE and passthrough and engine in PROMPT_ENFORCED_SAFE_ENGINES:
            raise ValueError("passthrough=True is not supported for prompt-enforced safe engines")
        if (
            engine in ENGINE_ARGV_TRANSPORT
            and len(prompt.encode("utf-8")) + (persona.size_bytes if persona is not None else 0)
            > PROMPT_ARGV_GUARD_BYTES
        ):
            raise ValueError(
                f"stage output too large for {'/'.join(ENGINE_ARGV_TRANSPORT)} argv transport; "
                "route this stage to codex/claude/droid/opencode"
            )
        engine_sem = self.state.engine_semaphores.get(engine)
        with self.state.agent_semaphore:
            if engine_sem is None:
                return self._run_structured_or_text(
                    engine,
                    prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    fast=fast,
                    schema=schema,
                    isolation=isolation,
                    passthrough=passthrough,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                    label=label,
                    persona=persona,
                    allow_repo_persona=allow_repo_persona,
                    resumable=resumable,
                )
            with engine_sem:
                return self._run_structured_or_text(
                    engine,
                    prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    fast=fast,
                    schema=schema,
                    isolation=isolation,
                    passthrough=passthrough,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                    label=label,
                    persona=persona,
                    allow_repo_persona=allow_repo_persona,
                    resumable=resumable,
                )

    def _run_structured_or_text(
        self,
        engine: str,
        prompt: str,
        *,
        mode: str,
        model: str | None,
        effort: str | None,
        fast: bool | None,
        schema: JsonObject | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        retries: int | None,
        key: str,
        label: str | None = None,
        persona: personas.PersonaResolution | None = None,
        allow_repo_persona: bool = False,
        resumable: bool = False,
    ) -> JsonValue:
        if schema is None:
            # Retry attachment is a child-run concern, not a structured-output
            # concern.  A work-lane timeout with no schema still has a dirty
            # worktree worth preserving for its retry.
            attempts = retries if retries is not None else 0
            if attempts <= 0:
                return self._run_delegate(
                    engine,
                    prompt,
                    mode=mode,
                    model=model,
                    effort=effort,
                    fast=fast,
                    isolation=isolation,
                    passthrough=passthrough,
                    timeout=timeout,
                    output_schema=None,
                    prefer_assistant=False,
                    workflow_agent_key=key,
                    label=label,
                    persona=persona,
                    allow_repo_persona=allow_repo_persona,
                    expected_persona_digest=persona.digest if persona is not None else None,
                    resumable=resumable,
                )
            retry_workspace_run_id: str | None = None
            retry_backend: str | None = None
            workspace_cleanup: JsonObject | None = None
            first_child_run_id: str | None = None
            for attempt in range(attempts + 1):
                try:
                    raw_child = self._run_delegate(
                        engine,
                        prompt,
                        mode=mode,
                        model=model,
                        effort=effort,
                        fast=fast,
                        isolation=isolation,
                        passthrough=passthrough,
                        timeout=timeout,
                        output_schema=None,
                        prefer_assistant=False,
                        workflow_agent_key=key,
                        label=label,
                        persona=persona,
                        allow_repo_persona=allow_repo_persona,
                        expected_persona_digest=persona.digest if persona is not None else None,
                        return_metadata=True,
                        structured_retry_run_id=retry_workspace_run_id,
                        preserve_retry_workspace=True,
                        structured_retry_backend=retry_backend,
                        resumable=resumable,
                    )
                except BaseException:
                    _cleanup_structured_retry_workspace(workspace_cleanup)
                    if first_child_run_id is not None:
                        self._release_structured_retry_worktree(first_child_run_id)
                    raise
                child = _delegate_child_result(raw_child)
                if first_child_run_id is None:
                    first_child_run_id = child.run_id
                if child.run_id is not None:
                    self.state.retry_worktree_runs.add(child.run_id)
                if child.workspace_cleanup is not None:
                    if (
                        workspace_cleanup is not None
                        and child.workspace_cleanup != workspace_cleanup
                    ):
                        _cleanup_structured_retry_workspace(child.workspace_cleanup)
                        _cleanup_structured_retry_workspace(workspace_cleanup)
                        if first_child_run_id is not None:
                            self._release_structured_retry_worktree(first_child_run_id)
                        raise RuntimeError("structured retry workspace cleanup metadata changed")
                    workspace_cleanup = child.workspace_cleanup
                if child.isolation_backend == "bwrap":
                    retry_backend = "bwrap"
                if child.text is not None:
                    _cleanup_structured_retry_workspace(workspace_cleanup)
                    if first_child_run_id is not None:
                        self._release_structured_retry_worktree(first_child_run_id)
                    return child.text
                outcome = child.outcome or ChildAttemptOutcome(
                    run_id=child.run_id,
                    failure_reason="nonzero_exit",
                    worktree=child.execution_cwd,
                    cleanup_ownership=child.workspace_cleanup,
                    execution_cwd=child.execution_cwd,
                )
                if attempt >= attempts or outcome.failure_reason not in CHILD_FAILURE_REASONS:
                    break
                self.state.append_event(
                    "agent_retry",
                    key=key,
                    label=label,
                    engine=engine,
                    attempt=attempt,
                    retryAttempt=attempt + 1,
                    childAttemptOutcome=outcome.as_json(),
                )
                if retry_workspace_run_id is None:
                    retry_workspace_run_id = child.run_id
            _cleanup_structured_retry_workspace(workspace_cleanup)
            if first_child_run_id is not None:
                self._release_structured_retry_worktree(first_child_run_id)
            return None
        workflow_schema.validate_schema_subset(schema)
        attempts = retries if retries is not None else _structured_retries(self.state.config)
        native_schema = _native_schema(engine, schema)
        prior_output = ""
        prior_error = ""
        prior_child: _DelegateChildResult | None = None
        last_parsed_candidate: JsonValue | None = None
        candidate_present = False
        retry_workspace_run_id: str | None = None
        structured_retry_backend: str | None = None
        workspace_cleanup: JsonObject | None = None
        first_child_run_id: str | None = None
        child: _DelegateChildResult | None = None
        for attempt in range(attempts + 1):
            resume_session_id = (
                prior_child.session_id
                if prior_child is not None and engine in STRUCTURED_RESUME_ENGINES
                else None
            )
            if attempt == 0:
                attempt_prompt = prompt
            elif resume_session_id is not None:
                attempt_prompt = _structured_resume_prompt(prior_error)
            else:
                attempt_prompt = _correction_prompt(prompt, prior_output, prior_error)
            attempt_persona = persona if resume_session_id is None else None
            if native_schema is not None:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", delete=False
                ) as schema_file:
                    json.dump(native_schema, schema_file)
                    schema_path = schema_file.name
                try:
                    try:
                        raw_child = self._run_delegate(
                            engine,
                            attempt_prompt,
                            mode=mode,
                            model=model,
                            effort=effort,
                            fast=fast,
                            isolation=isolation,
                            passthrough=False,
                            timeout=timeout,
                            output_schema=schema_path,
                            prefer_assistant=True,
                            workflow_agent_key=key,
                            label=label,
                            persona=attempt_persona,
                            allow_repo_persona=allow_repo_persona,
                            expected_persona_digest=(
                                attempt_persona.digest if attempt_persona is not None else None
                            ),
                            return_metadata=True,
                            structured_session=engine in STRUCTURED_RESUME_ENGINES,
                            structured_retry_run_id=retry_workspace_run_id,
                            resume_session_id=resume_session_id,
                            preserve_retry_workspace=True,
                            structured_retry_backend=structured_retry_backend,
                            resumable=resumable,
                        )
                    except BaseException:
                        _cleanup_structured_retry_workspace(workspace_cleanup)
                        if first_child_run_id is not None:
                            self._release_structured_retry_worktree(first_child_run_id)
                        raise
                finally:
                    Path(schema_path).unlink(missing_ok=True)
            else:
                if attempt == 0 or resume_session_id is None:
                    attempt_prompt = _structured_prompt(prompt, schema, prior_output, prior_error)
                try:
                    raw_child = self._run_delegate(
                        engine,
                        attempt_prompt,
                        mode=mode,
                        model=model,
                        effort=effort,
                        fast=fast,
                        isolation=isolation,
                        passthrough=False,
                        timeout=timeout,
                        output_schema=None,
                        prefer_assistant=True,
                        workflow_agent_key=key,
                        label=label,
                        persona=attempt_persona,
                        allow_repo_persona=allow_repo_persona,
                        expected_persona_digest=(
                            attempt_persona.digest if attempt_persona is not None else None
                        ),
                        return_metadata=True,
                        structured_session=engine in STRUCTURED_RESUME_ENGINES,
                        structured_retry_run_id=retry_workspace_run_id,
                        resume_session_id=resume_session_id,
                        preserve_retry_workspace=True,
                        structured_retry_backend=structured_retry_backend,
                        resumable=resumable,
                    )
                except BaseException:
                    _cleanup_structured_retry_workspace(workspace_cleanup)
                    if first_child_run_id is not None:
                        self._release_structured_retry_worktree(first_child_run_id)
                    raise
            child = _delegate_child_result(raw_child)
            if first_child_run_id is None:
                first_child_run_id = child.run_id
            if child.run_id is not None:
                self.state.retry_worktree_runs.add(child.run_id)
            if child.workspace_cleanup is not None:
                if workspace_cleanup is not None and child.workspace_cleanup != workspace_cleanup:
                    _cleanup_structured_retry_workspace(child.workspace_cleanup)
                    _cleanup_structured_retry_workspace(workspace_cleanup)
                    if first_child_run_id is not None:
                        self._release_structured_retry_worktree(first_child_run_id)
                    raise RuntimeError("structured retry workspace cleanup metadata changed")
                workspace_cleanup = child.workspace_cleanup
            if child.isolation_backend == "bwrap":
                structured_retry_backend = "bwrap"
            text = child.text
            try:
                value = workflow_schema.parse_json_tolerant(text or "", schema)
                last_parsed_candidate = value
                candidate_present = True
                workflow_schema.validate_value(value, schema)
                self._record_structured_attempt(key, None)
                _cleanup_structured_retry_workspace(workspace_cleanup)
                if first_child_run_id is not None:
                    self._release_structured_retry_worktree(first_child_run_id)
                return value
            except PersonaDigestMismatch:
                _cleanup_structured_retry_workspace(workspace_cleanup)
                if first_child_run_id is not None:
                    self._release_structured_retry_worktree(first_child_run_id)
                raise
            # Child output is untrusted; parse/validation blowups must not kill the supervisor.
            except Exception as exc:
                prior_output = text or ""
                outcome = child.outcome or ChildAttemptOutcome(
                    run_id=child.run_id,
                    failure_reason="structured",
                    worktree=child.execution_cwd,
                    cleanup_ownership=child.workspace_cleanup,
                    execution_cwd=child.execution_cwd,
                    session_id=child.session_id,
                )
                prior_error = (
                    f"child attempt {outcome.failure_reason}: {exc}"
                    if child.outcome is not None
                    else str(exc)
                )
                # Same defect the timeout rows had: engine and attempt without
                # key or label means learning which task burned its retries
                # still costs a cross-reference against agent_started by time.
                event: JsonObject = {
                    "engine": engine,
                    "attempt": attempt,
                    "error": prior_error,
                    "key": key,
                    "label": label,
                    "strategy": (
                        "resume"
                        if child.session_id is not None and engine in STRUCTURED_RESUME_ENGINES
                        else "relaunch"
                    ),
                    "retryAttempt": attempt + 1,
                    "childAttemptOutcome": outcome.as_json(),
                }
                if event["strategy"] == "resume":
                    event["sessionId"] = child.session_id
                self.state.append_event("agent_structured_retry", **event)
                if retry_workspace_run_id is None:
                    retry_workspace_run_id = child.run_id
                prior_child = child
        fallback: JsonValue | _MissingType = _MISSING
        if child is not None:
            fallback = _structured_completion_report_fallback(child, self.state.workspace, schema)
        if fallback is not _MISSING:
            self._record_structured_attempt(key, None)
            _cleanup_structured_retry_workspace(workspace_cleanup)
            if first_child_run_id is not None:
                self._release_structured_retry_worktree(first_child_run_id)
            return fallback
        outcome = StructuredAttemptOutcome(
            last_parsed_candidate=last_parsed_candidate,
            validation_error=prior_error,
            candidate_present=candidate_present,
        )
        self._record_structured_attempt(key, outcome)
        self.state.append_durable_event(
            "agent_structured_exhausted",
            key=key,
            scope=self.state.current_scope(),
            label=label,
            engine=engine,
            attempts=attempts + 1,
            **outcome.as_json(),
        )
        _cleanup_structured_retry_workspace(workspace_cleanup)
        if first_child_run_id is not None:
            self._release_structured_retry_worktree(first_child_run_id)
        return None

    def _release_structured_retry_worktree(self, run_id: str) -> None:
        """Release a completed structured retry's completion-time worktree hold."""
        _release_structured_retry_worktree_for_state(self.state, run_id)
        self.state.retry_worktree_runs.discard(run_id)

    def _run_delegate(
        self,
        engine: str,
        prompt: str,
        *,
        mode: str,
        model: str | None,
        effort: str | None,
        fast: bool | None,
        isolation: str | None,
        passthrough: bool,
        timeout: int | float | None,
        output_schema: str | None,
        prefer_assistant: bool,
        workflow_agent_key: str,
        label: str | None = None,
        persona: personas.PersonaResolution | None = None,
        allow_repo_persona: bool = False,
        expected_persona_digest: str | None = None,
        return_metadata: bool = False,
        structured_session: bool = False,
        structured_retry_run_id: str | None = None,
        resume_session_id: str | None = None,
        preserve_retry_workspace: bool = False,
        structured_retry_backend: str | None = None,
        resumable: bool = False,
    ) -> str | _DelegateChildResult | None:
        payload: JsonObject = {
            "engine": engine,
            "mode": mode,
            "prompt": prompt,
        }
        if mode != MODE_CALL:
            payload["cwd"] = str(self.state.workspace)
        if model is not None:
            payload["model"] = model
        if effort is not None:
            payload["reasoningEffort"] = effort
        if fast is not None and engine == "codex":
            payload["fast"] = fast
        if structured_retry_run_id is not None and structured_retry_backend != "bwrap":
            payload["isolation"] = "none"
        elif isolation is not None:
            payload["isolation"] = isolation
        if output_schema is not None:
            payload["outputSchema"] = output_schema
        if persona is not None:
            payload["persona"] = persona.name
            payload["allowRepoPersona"] = allow_repo_persona
        if expected_persona_digest is not None:
            payload["expectedPersonaDigest"] = expected_persona_digest
        if structured_session:
            payload["structuredSession"] = True
        if preserve_retry_workspace:
            payload["structuredRetryWorkspace"] = True
        if structured_retry_run_id is not None:
            payload["structuredRetryRunId"] = structured_retry_run_id
        if structured_retry_backend is not None:
            payload["structuredRetryBackend"] = structured_retry_backend
        if resume_session_id is not None:
            payload["structuredRetrySessionId"] = resume_session_id
        payload["workflowAgentKey"] = workflow_agent_key
        payload["promptInstructionMode"] = (
            PROMPT_INSTRUCTION_MODE_SLASH if passthrough else PROMPT_INSTRUCTION_MODE_WRAPPED
        )
        if mode == MODE_CALL:
            payload["readOnly"] = True
        if resumable:
            payload["resumable"] = True
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(payload, handle)
            input_path = handle.name
        try:
            argv = [
                *self.state.cli_argv,
                "--json",
                "--group",
                self.state.wf_id,
                "run",
                "--input-json",
                input_path,
            ]
            completed = _run_child_command_for_state(argv, state=self.state, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            cancel_workflow_agent_child(self.state.workspace, self.state.wf_id, workflow_agent_key)
            # key and label were both in scope here and neither was recorded, so
            # a timeout row said which engine died and not which task, and the
            # only way to find out was cross-referencing agent_started by time.
            # Two sites reporting a timeout differently is how that survived, so
            # both now carry every identifier each can honestly produce.
            self.state.append_event(
                "agent_timeout",
                engine=engine,
                timeout=timeout,
                key=workflow_agent_key,
                label=label,
                model=model,
                scope=self.state.current_scope(),
            )
            self.state.notify_event(
                "agent_timeout",
                detail=f"{label or workflow_agent_key} ({engine}) after {timeout}s",
            )
            recovered = _workflow_agent_run_result_metadata(
                self.state.workspace,
                self.state.wf_id,
                workflow_agent_key,
            )
            session_id = _session_id_from_child_output(exc.output)
            if session_id is not None:
                recovered = _DelegateChildResult(
                    text=None,
                    run_id=recovered.run_id if recovered is not None else None,
                    execution_cwd=recovered.execution_cwd if recovered is not None else None,
                    session_id=session_id,
                    workspace_cleanup=(
                        recovered.workspace_cleanup if recovered is not None else None
                    ),
                    isolation_backend=(
                        recovered.isolation_backend if recovered is not None else None
                    ),
                    outcome=(recovered.outcome if recovered is not None else None),
                    completion_report_source=(
                        recovered.completion_report_source if recovered is not None else None
                    ),
                    completion_report_path=(
                        recovered.completion_report_path if recovered is not None else None
                    ),
                )
            recovered = _failed_child_result(recovered, reason="timeout", session_id=session_id)
            if return_metadata:
                return recovered
            if recovered is not None:
                _cleanup_structured_retry_workspace(recovered.workspace_cleanup)
            return None
        finally:
            Path(input_path).unlink(missing_ok=True)
        text = completed.stdout.decode("utf-8", errors="replace")
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = None
        if isinstance(result, dict):
            run_id = result.get("runId")
            if isinstance(run_id, str):
                self.state.thread_local.last_run_id = run_id
                event: JsonObject = {
                    "engine": engine,
                    "key": workflow_agent_key,
                    "workflowAgentKey": workflow_agent_key,
                    "runId": run_id,
                }
                if label is not None:
                    event["label"] = label
                if resumable:
                    event["resumable"] = True
                self.state.append_event("agent_child", **event)
        if (
            expected_persona_digest is not None
            and isinstance(result, dict)
            and result.get("error") in PERSONA_RESOLUTION_ERRORS
        ):
            raise PersonaDigestMismatch(
                "workflow child could not resolve the parent-pinned persona"
            )
        if completed.returncode != 0:
            failure_reason = (
                result.get("failureReason") or result.get("error")
                if isinstance(result, dict)
                else None
            )
            normalized_reason = _normalize_child_failure_reason(
                failure_reason, default="nonzero_exit"
            )
            if return_metadata:
                # Preserve the failed run's workspace/branch for the structured
                # retry protocol.  Cleaning it here would discard checkpoint
                # commits before the retry can attach to the lane.
                child = (
                    _child_result_from_payload(result, text=None)
                    if isinstance(result, dict)
                    else _DelegateChildResult(None, None, None, None)
                )
                return _failed_child_result(child, reason=normalized_reason)
            cleanup = (
                result.get("temporaryWorkspaceCleanup")
                if isinstance(result, dict)
                and isinstance(result.get("temporaryWorkspaceCleanup"), dict)
                else None
            )
            if cleanup is None:
                recovered = _workflow_agent_run_result_metadata(
                    self.state.workspace,
                    self.state.wf_id,
                    workflow_agent_key,
                )
                cleanup = recovered.workspace_cleanup if recovered is not None else None
            _cleanup_structured_retry_workspace(cleanup)
            stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            if expected_persona_digest is not None and "workflow_persona_digest_mismatch" in text:
                raise PersonaDigestMismatch("workflow child rejected changed persona bytes")
            # The child's JSON error is the real diagnosis; stderr alone is
            # often just the persona preface and pool warnings.
            parts = [f"delegate child exited {completed.returncode}"]
            if isinstance(result, dict):
                for field in ("error", "message", "runId"):
                    if isinstance(result.get(field), str) and result[field]:
                        parts.append(f"{field}={result[field]}")
            elif text.strip():
                parts.append(text.strip()[:500])
            if stderr.strip():
                parts.append(stderr.strip())
            raise RuntimeError("; ".join(parts))
        if result is None:
            recovered = _workflow_agent_run_result_metadata(
                self.state.workspace,
                self.state.wf_id,
                workflow_agent_key,
            )
            if return_metadata:
                return _failed_child_result(recovered, reason="structured")
            if recovered is not None:
                _cleanup_structured_retry_workspace(recovered.workspace_cleanup)
            raise RuntimeError(f"delegate child returned invalid JSON: {text[:500]}")
        if not isinstance(result, dict) or not result.get("ok", False):
            child = (
                _child_result_from_payload(result, text=None) if isinstance(result, dict) else None
            )
            if return_metadata and child is not None:
                return child
            if child is not None:
                _cleanup_structured_retry_workspace(child.workspace_cleanup)
            else:
                recovered = _workflow_agent_run_result_metadata(
                    self.state.workspace,
                    self.state.wf_id,
                    workflow_agent_key,
                )
                if recovered is not None:
                    _cleanup_structured_retry_workspace(recovered.workspace_cleanup)
            return None
        if (
            expected_persona_digest is not None
            and result.get("personaDigest") != expected_persona_digest
        ):
            raise PersonaDigestMismatch(
                "workflow child did not report the parent-pinned persona digest"
            )
        structured_codex = engine == "codex" and output_schema is not None
        if structured_codex:
            answer = _live_child_completion_report(result, self.state.workspace)
        elif isinstance(result.get("text"), str):
            answer = result["text"]
        else:
            assistant = result.get("assistantText")
            if prefer_assistant and isinstance(assistant, str) and assistant.strip():
                answer = assistant
            else:
                report_path = result.get("completionReportPath")
                report = _read_completion_report(report_path, self.state.workspace)
                if report is not None:
                    answer = report
                elif isinstance(assistant, str):
                    answer = assistant
                else:
                    answer = ""
        if not return_metadata:
            return answer
        child = _child_result_from_payload(result, text=answer)
        return child

    def followup(
        self,
        prior_label: str,
        prompt: str,
        *,
        label: str | None = None,
        phase: str | None = None,
        schema: JsonObject | None = None,
        timeout: int | float | None = None,
        retries: int | None = None,
    ) -> JsonValue:
        if not isinstance(prior_label, str) or not prior_label.strip():
            raise ValueError("prior_label must be a non-empty string")
        if not isinstance(prompt, str):
            prompt = str(prompt)
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not timeout > 0
        ):
            raise ValueError("timeout must be a positive number of seconds")
        prior_child = self.state.get_prior_child(prior_label)
        resolved_phase = phase or self.current_phase
        opts: JsonObject = {}
        if schema is not None:
            opts["schema"] = schema
        if timeout is not None:
            opts["timeout"] = timeout
        if retries is not None:
            opts["retries"] = retries
        path = self.state.next_agent_path()
        key_version = 2 if self.state.workflow_key_version == 2 else 1
        base_key = _followup_key(path, prior_label, prompt, opts, version=key_version)
        key = base_key
        if key_version == 2 and base_key in self.state.tombstoned_keys:
            retry_opts = dict(opts)
            retry_opts["retryAttempt"] = max(self.state.replay_attempt, 1)
            key = _followup_key(path, prior_label, prompt, retry_opts, version=key_version)
        if label is not None:
            self.state.label_keys[label] = key
        if key in self.state.replay_keys and key not in self.state.tombstoned_keys:
            result = self.state.replay[key]
            self.state.append_event(
                "agent_cache_hit",
                key=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                result=result,
            )
            return result
        if key in self.state.started_without_result and key not in self.state.tombstoned_keys:
            adopted = self._adopt_existing_agent_run(
                key,
                scope=path,
                label=label,
                phase=resolved_phase,
                schema=schema,
                prefer_assistant=schema is not None,
                timeout=timeout,
            )
            if adopted is not _MISSING:
                self.state.replay_keys.add(key)
                self.state.replay[key] = adopted
                self.state.started_without_result.discard(key)
                self.state.append_event(
                    "agent_finished",
                    key=key,
                    scope=path,
                    result=adopted,
                    adopted=True,
                )
                return adopted
        already_claimed = key in self.state.claimed_keys
        if not already_claimed:
            self.state.claim_agent_lifetime()
        if self.state.dry_run:
            if already_claimed:
                spent = self.state.dry_run_budget_spent
            else:
                spent = self.state.dry_run_budget_tick()
                self.state.claimed_keys.add(key)
            remaining = (
                None if self.state.budget.total is None else max(self.state.budget.total - spent, 0)
            )
            self.state.append_event(
                "budget",
                key=key,
                spent=spent,
                total=self.state.budget.total,
                remaining=remaining,
                simulated=True,
            )
            placeholder = workflow_schema.placeholder(schema) if schema else ""
            prompt_bytes = len(prompt.encode("utf-8"))
            dry_run_entry = {
                "primitive": "followup",
                "scope": path,
                "priorLabel": prior_label,
                "engine": prior_child.engine,
                "mode": "work",
                "promptBytes": prompt_bytes,
                "phase": resolved_phase,
                "label": label,
                "schema": bool(schema),
            }
            self.state.dry_runs.append(dry_run_entry)
            self.state.append_event(
                "agent_started",
                key=key,
                scope=path,
                dryRun=True,
                simulated=True,
            )
            self.state.append_event(
                "agent_finished", key=key, scope=path, result=placeholder, simulated=True
            )
            if label is not None:
                self.state.record_completed_child(
                    label=label,
                    run_id=f"dry_run_{key}",
                    engine=prior_child.engine,
                    resumable=True,
                )
            return placeholder
        if already_claimed:
            spent = self.state.budget.spent()
        else:
            spent = self.state.budget.claim()
            self.state.claimed_keys.add(key)
        self.state.append_event(
            "budget",
            key=key,
            spent=spent,
            total=self.state.budget.total,
            remaining=_budget_remaining_json(self.state.budget),
        )
        with self.state.active_agent():
            self.state.append_event(
                "agent_started",
                key=key,
                workflowAgentKey=key,
                scope=path,
                label=label,
                phase=resolved_phase,
                engine=prior_child.engine,
                mode="work",
                priorLabel=prior_label,
            )
            try:
                result = self._run_followup_attempts(
                    prior_child,
                    prompt,
                    key=key,
                    label=label,
                    schema=schema,
                    timeout=timeout,
                    retries=retries,
                )
            except SupervisorWatchdogExit:
                raise
            except Exception as exc:
                self.state.append_event(
                    "agent_failed",
                    key=key,
                    scope=path,
                    engine=prior_child.engine,
                    error=str(exc),
                )
                result = None
        if result is not None:
            self.state.tombstoned_keys.discard(key)
            self.state.replay_keys.add(key)
            self.state.replay[key] = result
            self.state.append_event(
                "agent_finished",
                key=key,
                scope=path,
                engine=prior_child.engine,
                result=result,
            )
            run_id = getattr(self.state.thread_local, "last_run_id", None)
            if label is not None and isinstance(run_id, str):
                self.state.record_completed_child(label, run_id, prior_child.engine, resumable=True)
            return result
        self.state.replay_keys.add(key)
        self.state.replay[key] = None
        self.state.append_event("agent_finished", key=key, scope=path, result=None, exhausted=True)
        return None

    def _run_followup_attempts(
        self,
        prior_child: CompletedChild,
        prompt: str,
        *,
        key: str,
        label: str | None = None,
        schema: JsonObject | None,
        timeout: int | float | None,
        retries: int | None,
    ) -> JsonValue:
        engine = prior_child.engine
        engine_sem = self.state.engine_semaphores.get(engine)
        with self.state.agent_semaphore:
            if engine_sem is None:
                return self._run_followup_structured_or_text(
                    prior_child,
                    prompt,
                    schema=schema,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                    label=label,
                )
            with engine_sem:
                return self._run_followup_structured_or_text(
                    prior_child,
                    prompt,
                    schema=schema,
                    timeout=timeout,
                    retries=retries,
                    key=key,
                    label=label,
                )

    def _run_followup_structured_or_text(
        self,
        prior_child: CompletedChild,
        prompt: str,
        *,
        schema: JsonObject | None,
        timeout: int | float | None,
        retries: int | None,
        key: str,
        label: str | None = None,
    ) -> JsonValue:
        if schema is None:
            return self._run_delegate_followup(
                prior_child.run_id,
                prompt,
                engine=prior_child.engine,
                timeout=timeout,
                prefer_assistant=False,
                workflow_agent_key=key,
                label=label,
            )
        workflow_schema.validate_schema_subset(schema)
        attempts = retries if retries is not None else _structured_retries(self.state.config)
        prior_output = ""
        prior_error = ""
        last_parsed_candidate: JsonValue | None = None
        candidate_present = False
        for attempt in range(attempts + 1):
            attempt_prompt = _structured_prompt(prompt, schema, prior_output, prior_error)
            text = self._run_delegate_followup(
                prior_child.run_id,
                attempt_prompt,
                engine=prior_child.engine,
                timeout=timeout,
                prefer_assistant=True,
                workflow_agent_key=key,
                label=label,
            )
            try:
                value = workflow_schema.parse_json_tolerant(text or "", schema)
                last_parsed_candidate = value
                candidate_present = True
                workflow_schema.validate_value(value, schema)
                self._record_structured_attempt(key, None)
                return value
            except Exception as exc:
                prior_output = text or ""
                prior_error = str(exc)
                self.state.append_event(
                    "agent_structured_retry",
                    engine=prior_child.engine,
                    attempt=attempt,
                    error=prior_error,
                    key=key,
                    label=label,
                )
        outcome = StructuredAttemptOutcome(
            last_parsed_candidate=last_parsed_candidate,
            validation_error=prior_error,
            candidate_present=candidate_present,
        )
        self._record_structured_attempt(key, outcome)
        self.state.append_durable_event(
            "agent_structured_exhausted",
            key=key,
            scope=self.state.current_scope(),
            label=label,
            engine=prior_child.engine,
            attempts=attempts + 1,
            **outcome.as_json(),
        )
        return None

    def _run_delegate_followup(
        self,
        handle: str,
        prompt: str,
        *,
        engine: str,
        timeout: int | float | None,
        prefer_assistant: bool,
        workflow_agent_key: str,
        label: str | None = None,
    ) -> str | None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle_file:
            handle_file.write(prompt)
            prompt_path = handle_file.name
        try:
            argv = [
                *self.state.cli_argv,
                "--json",
                "--group",
                self.state.wf_id,
                "followup",
            ]
            if timeout is not None:
                argv.extend(["--timeout", str(max(1, math.ceil(timeout)))])
            argv.extend(["--prompt-file", prompt_path, handle])
            completed = _run_child_command_for_state(argv, state=self.state, timeout=timeout)
        except subprocess.TimeoutExpired:
            cancel_workflow_agent_child(self.state.workspace, self.state.wf_id, workflow_agent_key)
            self.state.append_event("agent_timeout", engine=engine, timeout=timeout)
            return None
        finally:
            Path(prompt_path).unlink(missing_ok=True)
        text = completed.stdout.decode("utf-8", errors="replace")
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = None
        if isinstance(result, dict):
            run_id = result.get("runId")
            if isinstance(run_id, str):
                self.state.thread_local.last_run_id = run_id
                event: JsonObject = {
                    "engine": engine,
                    "key": workflow_agent_key,
                    "workflowAgentKey": workflow_agent_key,
                    "runId": run_id,
                    "resumable": True,
                }
                if label is not None:
                    event["label"] = label
                self.state.append_event("agent_child", **event)
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                stderr or text or f"delegate followup child failed with {completed.returncode}"
            )
        if result is None:
            raise RuntimeError(f"delegate followup child returned invalid JSON: {text[:500]}")
        if not isinstance(result, dict) or not result.get("ok", False):
            return None
        if isinstance(result.get("text"), str):
            return result["text"]
        assistant = result.get("assistantText")
        if prefer_assistant and isinstance(assistant, str) and assistant.strip():
            return assistant
        report_path = result.get("completionReportPath")
        report = _read_completion_report(report_path, self.state.workspace)
        if report is not None:
            return report
        if isinstance(assistant, str):
            return assistant
        return ""


def _run_child_command_for_state(
    argv: list[str],
    *,
    state: object,
    timeout: int | float | None,
) -> subprocess.CompletedProcess[bytes]:
    """Invoke the child wait with lifecycle callbacks when the seam supports them.

    A few focused tests replace ``_run_child_command`` with a narrow
    side-effect function.  Filter only those injected callbacks while keeping
    the real runtime on the cancellation path.
    """
    kwargs: dict[str, object] = {"cwd": str(state.workspace), "timeout": timeout}
    cancel_event = getattr(state, "cancel_event", None)
    if cancel_event is not None and hasattr(cancel_event, "is_set"):
        kwargs["cancel_event"] = cancel_event
    side_effect = getattr(_run_child_command, "side_effect", None)
    if callable(side_effect):
        try:
            params = inspect.signature(side_effect).parameters.values()
        except (TypeError, ValueError):
            params = ()
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
            accepted = {param.name for param in params}
            kwargs = {key: value for key, value in kwargs.items() if key in accepted}
    return _run_child_command(argv, **kwargs)


def _run_child_command(
    argv: list[str],
    *,
    cwd: str,
    timeout: int | float | None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # Row children keep DELEGATE_WORKFLOW_PIN (nested delegate invocations
    # must run the pinned runtime) but never the lock-fd var: close_fds means
    # the fd is not inherited, so the number is at best EBADF and at worst an
    # unrelated file (wp-ptw, observed live 2026-08-31 as suite-wide EBADF in
    # rows entering _held_workflow_lock).
    child_env = {key: value for key, value in os.environ.items() if key != WORKFLOW_LOCK_FD_ENV}
    process = subprocess.Popen(  # nosec B603 - argv is Delegate's own validated CLI.
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=child_env,
    )
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        wait_seconds = CHILD_WAIT_POLL_SECONDS
        if deadline is not None:
            wait_seconds = min(wait_seconds, max(deadline - time.monotonic(), 0.0))
        try:
            stdout, stderr = process.communicate(timeout=wait_seconds)
        except subprocess.TimeoutExpired as exc:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_and_reap_child(process)
                raise SupervisorWatchdogExit("child wait interrupted") from exc
            if deadline is not None and time.monotonic() >= deadline:
                _terminate_process_group(process, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                exc.output = stdout
                exc.stderr = stderr
                raise exc
            continue
        break
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout,
        stderr,
    )


def _terminate_and_reap_child(process: subprocess.Popen[bytes]) -> None:
    _terminate_process_group(process, signal.SIGTERM)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, signal.SIGKILL)
        process.communicate()


def _terminate_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(process.pid), sig)


def _native_schema(engine: str, schema: JsonObject) -> JsonObject | None:
    """Schema to hand the child as --output-schema, or None for the prompt-and-parse path.

    Codex only accepts strict closed objects; Claude's --json-schema takes the
    validated subset as-is, so it is enforced natively for every workflow schema.
    """
    if engine == "codex":
        return _codex_native_schema(schema)
    if engine == "claude":
        return schema
    return None


def _codex_native_schema(schema: JsonObject) -> JsonObject | None:
    """Codex strict output only accepts fully-required, closed objects.

    Return the schema when it qualifies (the child injects the strict-mode
    defaults itself and warns, as for any direct run); otherwise None,
    and the caller falls back to the prompt-and-parse path the other engines
    use. Handing an optional-field schema to `--output-schema` would make the
    child fail its preflight before launch.
    """
    try:
        structured_output.normalize_codex_schema(json.loads(json.dumps(schema)))
    except structured_output.SchemaPreflightError:
        return None
    return schema


def _structured_prompt(prompt: str, schema: JsonObject, prior_output: str, prior_error: str) -> str:
    parts = [
        prompt,
        "",
        "Return ONLY a JSON value matching this schema. Fenced JSON is allowed, but no prose.",
        json.dumps(schema, sort_keys=True),
    ]
    if prior_output or prior_error:
        parts.extend(
            [
                "",
                "Your prior output was invalid. Correct it now.",
                "Prior output:",
                prior_output,
                "Validation error:",
                prior_error,
            ]
        )
    return "\n".join(parts)


def _correction_prompt(prompt: str, prior_output: str, prior_error: str) -> str:
    if not prior_output and not prior_error:
        return prompt
    return "\n".join(
        [
            prompt,
            "",
            "Your prior output was invalid. Correct it now.",
            "Prior output:",
            prior_output,
            "Validation error:",
            prior_error,
        ]
    )


def _structured_resume_prompt(prior_error: str) -> str:
    return "\n".join(
        [
            "Your prior StructuredOutput failed validation:",
            prior_error,
            "",
            "Re-emit the StructuredOutput now.",
        ]
    )


def _parse_engine_spec(value: object) -> tuple[str, str | None, str | None]:
    if isinstance(value, dict):
        return (
            str(value.get("engine", DEFAULT_ENGINE)),
            value.get("model"),
            _validate_workflow_effort(value.get("effort")),
        )
    if isinstance(value, str):
        if value in KNOWN_ENGINES:
            return value, None, None
        return "droid", value, None
    return DEFAULT_ENGINE, None, None


def _validate_workflow_effort(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in WORKFLOW_EFFORT_VALUES:
        allowed = ", ".join(WORKFLOW_EFFORT_VALUES)
        raise ValueError(f"effort must be one of: {allowed}")
    return value


def _engine_chain(value: object) -> list[str]:
    if isinstance(value, list):
        chain = [str(item) for item in value if isinstance(item, str) and item]
        return chain or [DEFAULT_ENGINE]
    if isinstance(value, str) and value:
        return [value]
    return [DEFAULT_ENGINE]


def _agent_key(scope_path: str, prompt: str, opts: JsonObject, *, version: int = 1) -> str:
    canonical_opts = _canonical_json(opts)
    return _stable_hash(f"v{version}:{scope_path}{prompt}{canonical_opts}")


def _followup_key(
    scope_path: str, prior_label: str, prompt: str, opts: JsonObject, *, version: int = 1
) -> str:
    canonical_opts = _canonical_json(opts)
    return _stable_hash(f"followup-v{version}:{scope_path}{prior_label}{prompt}{canonical_opts}")


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _gate_result_hash(result: object) -> str:
    return _stable_hash(_canonical_json(result))


def _gate_failed(result: object) -> bool:
    return result is None or (isinstance(result, dict) and result.get("ok") is False)


def _replay_result_failed(result: object, *, exhausted: bool = False) -> bool:
    return exhausted or _gate_failed(result)


def resolve_workflow_reference(name_or_path: str, parent_script_dir: Path) -> Path:
    raw = str(name_or_path)
    try:
        saved = registry.saved_workflow_path(raw).resolve()
    except ValueError:
        saved = None
    if saved is not None and saved.exists():
        return saved
    candidate = Path(raw).expanduser()
    user_root = registry.user_workflow_root().resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved.exists() and resolved.is_relative_to(user_root):
            return resolved
        raise RuntimeError(
            "nested workflow paths must be saved workflow names, absolute paths inside "
            "~/.delegate/workflows, or paths inside the parent workflow directory"
        )
    parent_root = parent_script_dir.resolve()
    resolved = (parent_root / candidate).resolve()
    if resolved.exists() and resolved.is_relative_to(parent_root):
        return resolved
    raise RuntimeError(
        "nested workflow paths must be saved workflow names, absolute paths inside "
        "~/.delegate/workflows, or paths inside the parent workflow directory"
    )


def load_args(root: Path) -> JsonValue:
    payload = registry.read_json(root / registry.ARGS_FILE)
    if not isinstance(payload, dict):
        return None
    return payload.get("args")


def cancel_workflow_children(workspace: Path, wf_id: str) -> list[JsonObject]:
    return _cancel_workflow_runs(workspace, wf_id, workflow_agent_key=None)


def cancel_workflow_agent_child(
    workspace: Path, wf_id: str, workflow_agent_key: str
) -> list[JsonObject]:
    return _cancel_workflow_runs(workspace, wf_id, workflow_agent_key=workflow_agent_key)


def _cancel_workflow_runs(
    workspace: Path, wf_id: str, *, workflow_agent_key: str | None
) -> list[JsonObject]:
    root = run_registry.registry_root_if_exists(workspace) or run_registry.registry_root(workspace)
    if not root.exists():
        return []
    index = run_registry.load_index(root)
    handles: list[str] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("group") != wf_id:
            continue
        if workflow_agent_key is not None and entry.get("workflowAgentKey") != workflow_agent_key:
            continue
        state = run_registry.load_run_state_or_none(root, run_id)
        if (
            run_registry.status_fields(state).get("effectiveStatus")
            not in run_registry.TERMINAL_STATUSES
        ):
            handles.append(run_id)
    if not handles:
        return []
    command = wait_cancel_commands.CancelCommand(tuple(handles), json_mode=True)
    out = io.StringIO()
    wait_cancel_commands.emit_cancel(command, workspace_path=str(workspace), stdout=out)
    try:
        payload = json.loads(out.getvalue())
    except json.JSONDecodeError:
        return []
    cancelled = payload.get("runs") if isinstance(payload, dict) else None
    return cancelled if isinstance(cancelled, list) else []


def _run_registry_root(workspace: Path) -> Path:
    return run_registry.registry_root_if_exists(workspace) or run_registry.registry_root(workspace)


def _find_workflow_agent_run(workspace: Path, wf_id: str, workflow_agent_key: str) -> str | None:
    root = _run_registry_root(workspace)
    if not root.exists():
        return None
    index = run_registry.load_index(root)
    matches: list[tuple[int, str]] = []
    for run_id, entry in index.get("runs", {}).items():
        if not isinstance(run_id, str) or not isinstance(entry, dict):
            continue
        if entry.get("group") != wf_id or entry.get("workflowAgentKey") != workflow_agent_key:
            continue
        ordinal = entry.get("registrationOrdinal", 0)
        matches.append((ordinal if isinstance(ordinal, int) else 0, run_id))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1]


def _workflow_agent_run_engine(workspace: Path, run_id: str) -> str | None:
    root = _run_registry_root(workspace)
    index = run_registry.load_index(root)
    entry = index.get("runs", {}).get(run_id)
    if isinstance(entry, dict):
        harness = entry.get("harness")
        if isinstance(harness, str) and harness:
            return harness
    manifest = run_registry.load_run_manifest_or_none(root, run_id)
    if isinstance(manifest, dict):
        harness = manifest.get("harness")
        if isinstance(harness, str) and harness:
            return harness
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    if isinstance(snapshot, dict):
        engine = snapshot.get("harness") or snapshot.get("engine")
        if isinstance(engine, str) and engine:
            return engine
    return None


def _workflow_agent_run_resumable(workspace: Path, run_id: str) -> bool:
    root = _run_registry_root(workspace)
    manifest = run_registry.load_run_manifest_or_none(root, run_id)
    if isinstance(manifest, dict) and manifest.get("resumable") is True:
        return True
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    if isinstance(snapshot, dict) and snapshot.get("resumable") is True:
        return True
    state = run_registry.load_run_state_or_none(root, run_id)
    return bool(isinstance(state, dict) and state.get("resumable") is True)


def _workflow_agent_child_event_exists(journal_path: Path, run_id: str, key: str) -> bool:
    for event in registry.iter_journal(journal_path):
        if event.get("type") != "agent_child" or event.get("runId") != run_id:
            continue
        event_key = event.get("workflowAgentKey") or event.get("key")
        if event_key == key:
            return True
    return False


def _workflow_run_terminal(workspace: Path, run_id: str) -> bool:
    root = _run_registry_root(workspace)
    state = run_registry.load_run_state_or_none(root, run_id)
    return (
        run_registry.status_fields(state).get("effectiveStatus") in run_registry.TERMINAL_STATUSES
    )


def _wait_for_workflow_agent_run(workspace: Path, run_id: str, timeout: int | float | None) -> bool:
    command = wait_cancel_commands.WaitCommand(
        (run_id,),
        timeout_seconds=int(timeout or wait_cancel_commands.WAIT_DEFAULT_TIMEOUT_SECONDS),
        interval_seconds=1,
        json_mode=True,
    )
    out = io.StringIO()
    exit_code = wait_cancel_commands.emit_wait(command, workspace_path=str(workspace), stdout=out)
    return exit_code != 124


def _workflow_agent_run_result(
    workspace: Path,
    run_id: str,
    *,
    prefer_assistant: bool,
) -> str | None:
    root = _run_registry_root(workspace)
    state = run_registry.load_run_state_or_none(root, run_id)
    if run_registry.status_fields(state).get("effectiveStatus") != run_registry.STATUS_SUCCEEDED:
        return None
    snapshot = run_registry.load_run_snapshot_or_none(root, run_id)
    assistant = snapshot.get("assistantText") if isinstance(snapshot, dict) else None
    structured_codex = (
        prefer_assistant
        and isinstance(snapshot, dict)
        and (snapshot.get("harness") == "codex" or snapshot.get("engine") == "codex")
    )
    if structured_codex:
        return _snapshot_child_completion_report(snapshot, workspace)
    if prefer_assistant and isinstance(assistant, str) and assistant.strip():
        return assistant
    completion = snapshot.get("completionReport") if isinstance(snapshot, dict) else None
    report_path = completion.get("path") if isinstance(completion, dict) else None
    report = _read_completion_report(report_path, workspace)
    if report is not None:
        return report
    if isinstance(assistant, str):
        return assistant
    return ""


def _live_child_completion_report(result: JsonObject, workspace: Path) -> str | None:
    if result.get("completionReportSource") != "child":
        return None
    report_path = result.get("completionReportPath")
    return _read_completion_report(report_path, workspace)


def _snapshot_child_completion_report(snapshot: JsonObject, workspace: Path) -> str | None:
    if snapshot.get("completionReportSource") != "child":
        return None
    completion = snapshot.get("completionReport")
    report_path = completion.get("path") if isinstance(completion, dict) else None
    return _read_completion_report(report_path, workspace)


def _final_child_completion_report(child: _DelegateChildResult, workspace: Path) -> str | None:
    """Read only the final child-sourced report, never a synthesized report."""
    if child.completion_report_source == "child":
        report = _read_completion_report(child.completion_report_path, workspace)
        if report is not None:
            return report
    elif child.completion_report_source is not None:
        return None
    if child.run_id is None or not run_registry.RUN_ID_RE.fullmatch(child.run_id):
        return None
    snapshot = run_registry.load_run_snapshot_or_none(_run_registry_root(workspace), child.run_id)
    return (
        _snapshot_child_completion_report(snapshot, workspace)
        if isinstance(snapshot, dict)
        else None
    )


def _last_fenced_json_block(report: str) -> str | None:
    blocks: list[tuple[str, str]] = []
    info: str | None = None
    content: list[str] = []
    saw_fence = False
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            if info is not None:
                content.append(line)
            continue
        saw_fence = True
        if info is None:
            info = stripped[3:].strip()
            content = []
        else:
            blocks.append((info, "\n".join(content).strip()))
            info = None
            content = []
    for info, content in reversed(blocks):
        if not info or info.casefold() == "json":
            return content
    if saw_fence:
        return None
    return report.strip()


def _structured_completion_report_fallback(
    child: _DelegateChildResult, workspace: Path, schema: JsonObject
) -> JsonValue | _MissingType:
    try:
        report = _final_child_completion_report(child, workspace)
    except Exception:
        return _MISSING
    if report is None:
        return _MISSING
    candidate = _last_fenced_json_block(report)
    if candidate is None:
        return _MISSING
    try:
        value = workflow_schema.parse_json_tolerant(candidate, schema)
        workflow_schema.validate_value(value, schema)
    except Exception:
        return _MISSING
    return value


def _read_completion_report(report_path: object, workspace: Path) -> str | None:
    if not isinstance(report_path, str):
        return None
    path = Path(report_path)
    if not path.is_absolute():
        path = workspace / path
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


@contextlib.contextmanager
def _held_workflow_lock(root: Path) -> Iterator[None]:
    fd: int | None = None
    raw_fd = os.environ.get(WORKFLOW_LOCK_FD_ENV)
    if raw_fd is not None:
        # Trust the inherited fd only when it still refers to this workflow's
        # lock file. Processes spawned with close_fds (e.g. verify rows under a
        # live supervisor) inherit the env var without the fd, so the number
        # may be closed (EBADF) or reused for an unrelated file; both must fall
        # back to fresh acquisition instead of flocking a stranger.
        try:
            fd_num = int(raw_fd)
            fd_stat = os.fstat(fd_num)
            lock_stat = os.stat(root / registry.LOCK_FILE)
        except (ValueError, OSError):
            pass
        else:
            if (fd_stat.st_dev, fd_stat.st_ino) == (lock_stat.st_dev, lock_stat.st_ino):
                fcntl.flock(fd_num, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = fd_num
    if fd is None:
        fd = registry.acquire_workflow_lock(root)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


class _SupervisorWatchdog:
    def __init__(
        self,
        state: WorkflowState,
        *,
        interval_seconds: float,
    ) -> None:
        self.state = state
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"delegate-workflow-watchdog-{state.wf_id}",
            daemon=True,
        )
        self.reason: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(self.interval_seconds * 4, 1.0))

    def _run(self) -> None:
        bad_samples = 0
        while not self._stop.wait(self.interval_seconds):
            reason = self._check()
            if reason == "terminal":
                return
            if reason is None:
                bad_samples = 0
                continue
            bad_samples += 1
            if bad_samples < 2:
                continue
            self.reason = reason
            record_fire = getattr(self.state, "_record_watchdog_fire", None)
            recorded = record_fire(reason) if callable(record_fire) else True
            if recorded:
                self.state.cancel_event.set()
            return

    def _check(self) -> str | None:
        """Cancel only on positive evidence: a confirmed-deleted state file or a
        terminal status. An unreadable or malformed file is no information and
        never fires."""
        status_path = self.state.root / registry.STATUS_FILE
        try:
            os.stat(status_path)
        except FileNotFoundError:
            return "state_missing"
        except OSError:
            return None
        status = registry.read_json(status_path)
        if isinstance(status, dict) and status.get("status") in {"succeeded", "failed", "killed"}:
            return "terminal"
        return None


def run_supervisor(
    *,
    workspace: Path,
    wf_id: str,
    cli_argv: list[str],
    config: JsonObject,
) -> int:
    root = registry.workflow_dir(workspace, wf_id)
    with _held_workflow_lock(root):
        status = registry.read_json(root / registry.STATUS_FILE) or {}
        notify_spec = status.get("notify")
        script_path = root / registry.SCRIPT_FILE
        args = load_args(root)
        budget_payload = status.get("budget")
        total = budget_payload.get("total") if isinstance(budget_payload, dict) else None
        spent = budget_payload.get("spent") if isinstance(budget_payload, dict) else None
        total_budget = total if isinstance(total, int) else None
        replay_journal = status.get("replayJournal") is not False
        key_version = status.get("workflowKeyVersion")
        workflow_key_version = key_version if key_version in {1, 2} else 1
        prior_attempt = status.get("replayAttempt")
        replay_attempt = (
            prior_attempt if isinstance(prior_attempt, int) and prior_attempt >= 0 else 0
        )
        spent_budget = spent if isinstance(spent, int) and spent >= 0 else 0
        state = WorkflowState(
            wf_id=wf_id,
            workspace=workspace,
            root=root,
            script_path=script_path,
            config=config,
            cli_argv=cli_argv,
            args=args,
            budget=Budget(total_budget, spent_budget),
            replay_journal=replay_journal,
            workflow_key_version=workflow_key_version,
            replay_attempt=replay_attempt,
            notify_target=notify_spec if isinstance(notify_spec, str) and notify_spec else None,
        )
        state.write_status("running")
        watchdog = _SupervisorWatchdog(
            state,
            interval_seconds=WORKFLOW_WATCHDOG_INTERVAL_SECONDS,
        )
        watchdog.start()
        try:
            result = execute_workflow(state)
        except GateExit as exc:
            # A gate must carry metadata all the way to the supervisor.  A
            # metadata-less unwind is an execution failure, not a successful
            # pause that an operator can never approve.
            if exc.gate_key is None:
                # A sibling can be between agent() calls after the durable
                # gate row closed admission.  Its GateExit has no local
                # metadata, but the journal does.  Journal authority wins
                # over this lossy concurrent unwind.
                latest_gate = state.latest_gate_event()
                latest_key = latest_gate.get("key") if latest_gate is not None else None
                if isinstance(latest_key, str):
                    exc = GateExit(
                        str(exc),
                        gate_key=latest_key,
                        child=(
                            latest_gate.get("child")
                            if isinstance(latest_gate.get("child"), str)
                            else None
                        ),
                        result=latest_gate.get("result"),
                        result_hash=(
                            latest_gate.get("gateResultHash")
                            if isinstance(latest_gate.get("gateResultHash"), str)
                            else None
                        ),
                    )
                else:
                    tb = traceback.format_exc()[-4000:]
                    with contextlib.suppress(Exception):
                        state.append_event(
                            "workflow_failed",
                            error="gate exit missing durable gate key",
                            detail=str(exc),
                            traceback=tb,
                        )
                    with contextlib.suppress(Exception):
                        registry.write_result(
                            root,
                            {
                                "ok": False,
                                "wfId": wf_id,
                                "error": "gate exit missing durable gate key",
                                "traceback": tb,
                            },
                        )
                    with contextlib.suppress(Exception):
                        state.write_status(
                            "failed", error="gate exit missing durable gate key", traceback=tb
                        )
                    state.notify_event("failed", detail="gate exit missing durable gate key")
                    return 1
            # A concurrent unwind may have left status.json stale or absent;
            # rebuild its projection from the journal-authoritative gate event.
            state.ensure_gate_durable(exc)
            gate = registry.read_json(root / registry.STATUS_FILE) or {}
            gate_key = gate.get("gateKey")
            gate_result_hash = gate.get("gateResultHash")
            if (
                gate.get("status") != "paused"
                or gate_key != exc.gate_key
                or (gate_result_hash if isinstance(gate_result_hash, str) else None)
                != exc.result_hash
            ):
                tb = traceback.format_exc()[-4000:]
                with contextlib.suppress(Exception):
                    state.append_event(
                        "workflow_failed",
                        error="gate exit missing durable gate projection",
                        detail=str(exc),
                        traceback=tb,
                    )
                with contextlib.suppress(Exception):
                    state.write_status(
                        "failed", error="gate exit missing durable gate projection", traceback=tb
                    )
                return 1
            # A checkpoint is the state most worth ringing about: the workflow is
            # alive, correct, and will sit there indefinitely until a human acts.
            state.notify_event(
                "paused",
                detail=f"awaiting approval at {gate_key}",
            )
            return 0
        except SoftParkExit as exc:
            # Soft parks are not human approval gates.  The scheduler has
            # already drained every unrelated runnable item and each parked
            # worker has unwound its item slot; resume simply replays the
            # script and re-enters the same explicit named scopes.
            state.write_status(
                "paused",
                parkedItems=list(exc.names),
                softPark=True,
            )
            state.notify_event(
                "paused",
                detail=f"soft-parked items: {', '.join(exc.names)}",
            )
            return 0
        except SupervisorWatchdogExit as exc:
            tb = traceback.format_exc()[-4000:]
            watchdog_reason = watchdog.reason or exc.reason
            if root.exists() and (root / registry.STATUS_FILE).exists():
                with contextlib.suppress(Exception):
                    state.append_event("workflow_watchdog", reason=watchdog_reason, traceback=tb)
                gate_event = state.latest_gate_event()
                if isinstance(gate_event, dict) and isinstance(gate_event.get("key"), str):
                    # A watchdog can interrupt the drain after the gate row was
                    # fsynced.  Keep that journal-authoritative checkpoint
                    # recoverable rather than overwriting it with failed state.
                    with contextlib.suppress(Exception), state.journal_lock:
                        state._write_gate_projection_locked(gate_event)
                    state.notify_event(
                        "paused",
                        detail=f"awaiting approval at {gate_event['key']}",
                    )
                    return 0
                with contextlib.suppress(Exception):
                    registry.write_result(
                        root,
                        {
                            "ok": False,
                            "wfId": wf_id,
                            "error": str(exc),
                            "traceback": tb,
                        },
                    )
                with contextlib.suppress(Exception):
                    state.write_status(
                        "failed", error=str(exc), traceback=tb, watchdogReason=watchdog_reason
                    )
                state.notify_event("failed", detail=str(exc)[:160])
            return 1
        except BaseException as exc:
            tb = traceback.format_exc()[-4000:]
            state.append_event("workflow_failed", error=str(exc), traceback=tb)
            registry.write_result(
                root, {"ok": False, "wfId": wf_id, "error": str(exc), "traceback": tb}
            )
            state.write_status("failed", error=str(exc), traceback=tb)
            state.notify_event("failed", detail=str(exc)[:160])
            return 1
        else:
            registry.write_result(root, {"ok": True, "wfId": wf_id, "result": result})
            state.append_event("workflow_finished", result=result)
            state.write_status("succeeded")
            state.notify_event("succeeded")
            return 0
        finally:
            watchdog.stop()
            for run_id in tuple(state.retry_worktree_runs):
                with contextlib.suppress(Exception):
                    _release_structured_retry_worktree_for_state(state, run_id)
                state.retry_worktree_runs.discard(run_id)


def detach_supervisor(argv: list[str], *, cwd: Path, lock_fd: int | None = None) -> None:
    env = os.environ.copy()
    pass_fds: tuple[int, ...] = ()
    if lock_fd is not None:
        os.set_inheritable(lock_fd, True)
        env[WORKFLOW_LOCK_FD_ENV] = str(lock_fd)
        pass_fds = (lock_fd,)
    if os.environ.get("DELEGATE_WORKFLOW_NO_DAEMON") == "1":
        subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            pass_fds=pass_fds,
        )
        return
    first = os.fork()
    if first > 0:
        os.waitpid(first, 0)
        return
    os.setsid()
    second = os.fork()
    if second > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.execvpe(argv[0], argv, env)


def kill_supervisor(pid: int, pgid: int | None, *, force: bool = False) -> bool:
    if pid <= 1 or pgid is None or pgid <= 1:
        return False
    try:
        live_pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if live_pgid != pgid:
        return False
    sig = signal.SIGKILL if force else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
    if force:
        return True
    deadline = time.monotonic() + KILL_SUPERVISOR_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        else:
            time.sleep(0.05)
            continue
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    return True


def wait_for_workflow_lock(root: Path, *, timeout_seconds: float) -> bool:
    """Block until the workflow lock can be acquired (supervisor has exited)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = registry.acquire_workflow_lock(root)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
            continue
        with contextlib.suppress(OSError):
            os.close(fd)
        return True


__all__ = [
    "KILL_SUPERVISOR_FORCE_WAIT_SECONDS",
    "KILL_SUPERVISOR_WAIT_SECONDS",
    "Budget",
    "BudgetExceeded",
    "SoftPark",
    "SoftParkExit",
    "WorkflowState",
    "cancel_workflow_agent_child",
    "cancel_workflow_children",
    "cleanup_workflow_agent_workspaces",
    "detach_supervisor",
    "execute_workflow",
    "kill_supervisor",
    "run_supervisor",
    "wait_for_workflow_lock",
]
