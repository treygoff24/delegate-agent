"""The one outcome contract for a finished child run.

Before this module, "did the run succeed" was answered in several places that
could disagree: the CLI envelope's ``ok`` and the process exit code followed the
child's exit code, while ``record.ok`` and ``terminalState`` also consulted the
result quality. A child that exited 0 with no assistant text was therefore
``ok: true`` with exit 0 on the envelope and ``ok: false`` in state.json.

``compute_outcome`` is now the only function that turns a child's exit code and
the rest of the finalization evidence into a status, a machine-readable
``failureKind`` and the exit code the CLI returns. Every surface (envelope
``ok``/``status``/``exitCode``, the persisted record, wait, run-output, and the
workflow ``agent()`` return path that reads the envelope) reads its result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from delegate_agent import child_failures
from delegate_agent.harness_events import NO_OUTPUT_RESULT_QUALITIES
from delegate_agent.json_types import JsonObject

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Closed enum. Adding a member is a contract change: consumers switch on it.
FAILURE_EXIT_NONZERO = "exit_nonzero"
FAILURE_NO_ASSISTANT_TEXT = "no_assistant_text"
FAILURE_PROVIDER_QUOTA = "provider_quota"
FAILURE_PROVIDER_AUTH = "provider_auth"
FAILURE_PROVIDER_ERROR = "provider_error"
FAILURE_PROVIDER_REFUSAL = "provider_refusal"
FAILURE_PROVIDER_MAX_TURNS = "provider_max_turns"
FAILURE_SESSION_LOST = "session_lost"
FAILURE_DELIVERABLE_MISSING = "deliverable_missing"
FAILURE_STRUCTURED_INVALID = "structured_invalid"
FAILURE_POLICY_VIOLATION = "policy_violation"
FAILURE_MODEL_CONTINUITY = "model_continuity"
FAILURE_OUTPUT_LIMIT = "output_limit"
FAILURE_STALLED = "stalled"
# The runner process that owned the child is gone (wait found a dead or
# missing pid). Not transient: a crashed or OOM-killed runner is not an idle child.
FAILURE_RUNNER_LOST = "runner_lost"
FAILURE_TIMEOUT = "timeout"
FAILURE_CANCELLED = "cancelled"

FAILURE_KINDS = frozenset(
    {
        FAILURE_EXIT_NONZERO,
        FAILURE_NO_ASSISTANT_TEXT,
        FAILURE_PROVIDER_QUOTA,
        FAILURE_PROVIDER_AUTH,
        FAILURE_PROVIDER_ERROR,
        FAILURE_PROVIDER_REFUSAL,
        FAILURE_PROVIDER_MAX_TURNS,
        FAILURE_SESSION_LOST,
        FAILURE_DELIVERABLE_MISSING,
        FAILURE_STRUCTURED_INVALID,
        FAILURE_POLICY_VIOLATION,
        FAILURE_MODEL_CONTINUITY,
        FAILURE_OUTPUT_LIMIT,
        FAILURE_STALLED,
        FAILURE_RUNNER_LOST,
        FAILURE_TIMEOUT,
        FAILURE_CANCELLED,
    }
)

# The detailed ``failureReason`` codes stay (they carry remediation-specific
# meaning); ``failureKind`` is the stable, closed projection callers branch on.
_REASON_KINDS: dict[str, str] = {
    "usage_limit": FAILURE_PROVIDER_QUOTA,
    "usage_limit_preflight": FAILURE_PROVIDER_QUOTA,
    "auth_failed": FAILURE_PROVIDER_AUTH,
    "binding_not_active": FAILURE_PROVIDER_AUTH,
    "provider_error": FAILURE_PROVIDER_ERROR,
    "provider_refusal": FAILURE_PROVIDER_REFUSAL,
    "provider_max_turns": FAILURE_PROVIDER_MAX_TURNS,
    "provider_cancelled": FAILURE_CANCELLED,
    "codex_thread_lost": FAILURE_SESSION_LOST,
    "session_expired": FAILURE_SESSION_LOST,
    "empty_result": FAILURE_NO_ASSISTANT_TEXT,
    "no_assistant_text": FAILURE_NO_ASSISTANT_TEXT,
    "deliverable_missing": FAILURE_DELIVERABLE_MISSING,
    "structured": FAILURE_STRUCTURED_INVALID,
    "structured_invalid": FAILURE_STRUCTURED_INVALID,
    "call_output_invalid": FAILURE_STRUCTURED_INVALID,
    "commit_policy_violated": FAILURE_POLICY_VIOLATION,
    "commit_policy_unverified": FAILURE_POLICY_VIOLATION,
    "model_continuity_paused": FAILURE_MODEL_CONTINUITY,
    "output_limit_exceeded": FAILURE_OUTPUT_LIMIT,
    "stalled": FAILURE_STALLED,
    "stall": FAILURE_STALLED,
    "timeout": FAILURE_TIMEOUT,
    "call_timeout": FAILURE_TIMEOUT,
    "agent_timeout": FAILURE_TIMEOUT,
    "cancelled_by_user": FAILURE_CANCELLED,
    "harness_cancelled": FAILURE_CANCELLED,
    "cancelled": FAILURE_CANCELLED,
}

NO_ASSISTANT_TEXT_WITH_CHANGES_WARNING = (
    "no_assistant_text: the work run exited 0 without assistant text but its work "
    "summary shows file changes or commits; the run counts as succeeded so the work "
    "can be adopted. Inspect workSummary, or pass --expect-file for a stricter check."
)

ORPHANED_PROCESSES_WARNING = (
    "orphanedProcesses: the child's process group still had live members after the "
    "harness exited; Delegate terminated them. A background task the harness started "
    "may not have finished."
)


def failure_kind_for_reason(reason: object, *, exit_code: int | None = None) -> str | None:
    """Project a detailed failureReason code onto the closed failureKind enum."""
    if not isinstance(reason, str) or not reason:
        return None
    kind = _REASON_KINDS.get(reason)
    if kind is not None:
        return kind
    if reason in FAILURE_KINDS:
        return reason
    return FAILURE_EXIT_NONZERO if exit_code != 0 else FAILURE_PROVIDER_ERROR


@dataclass(frozen=True)
class Outcome:
    status: str
    failure_kind: str | None
    exit_code: int
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    def as_json(self) -> JsonObject:
        payload: JsonObject = {"status": self.status, "failureKind": self.failure_kind}
        if self.evidence:
            payload["evidence"] = list(self.evidence)
        return payload


def _failed_exit(child_exit_code: int) -> int:
    return child_exit_code if child_exit_code != 0 else 1


def work_summary_shows_changes(summary: object) -> bool:
    """Does a run's ``workSummary`` record file changes or commits?"""
    if not isinstance(summary, dict):
        return False
    return any(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (summary.get("changedFilesCount"), summary.get("commitsCreatedCount"))
    )


def compute_outcome(
    *,
    child_exit_code: int,
    harness_terminal_status: str | None = None,
    provider_terminal_state: str | None = None,
    failure_reason: str | None = None,
    result_quality: str | None = None,
    cancelled: bool = False,
    signal_text: str = "",
    final_attempt_signal_text: str | None = None,
    diagnosed_reason: str | None = None,
    unrecovered_error: str | None = None,
    missing_deliverables: tuple[str, ...] = (),
    orphaned_processes: bool = False,
    work_changed: bool = False,
) -> Outcome:
    """Compute the one Outcome for a finished child.

    ``failure_reason`` is a failure Delegate already established independently
    of the exit code (timeout, stall, output cap, commit policy, pinned
    continuity pause). ``signal_text`` is trusted diagnostic text only (stderr
    tail and normalized harness error events), never assistant output; it names
    the kind of a failure, and fails an exit-0 run only when that run produced no
    usable output. ``diagnosed_reason`` is a caller-side classification of the
    same trusted text that is more specific than the generic classifier (for
    example a follow-up's lost native session). ``unrecovered_error`` is the
    last provider error the stream never recovered from; a quota error there
    fails an exit-0 run even when some output was produced, because the
    provider's final word was a quota refusal. ``final_attempt_signal_text``
    is the same trusted text restricted to the last attempt of a retried run;
    only it may turn an exit-0 empty result into a quota failure, because an
    earlier attempt's quota error was already handled by the retry.
    ``work_changed`` says a tracked work run's work summary shows file changes
    or commits. Such a run that exits 0 without assistant text succeeded with a
    warning (its work landed and an orchestrator must be able to adopt it);
    without changes it fails as ``no_assistant_text``. ``missing_deliverables``
    is checked before the no-output rules: a run with a missing ``--expect-file``
    deliverable fails as ``deliverable_missing`` even when it also produced no
    assistant text, because that is the verdict the caller asked for.
    """
    warnings: list[str] = []
    if orphaned_processes:
        warnings.append(ORPHANED_PROCESSES_WARNING)
    evidence: list[str] = []
    if missing_deliverables:
        evidence.extend(f"expected file missing: {path}" for path in missing_deliverables)

    def done(status: str, kind: str | None, exit_code: int) -> Outcome:
        return Outcome(status, kind, exit_code, tuple(evidence), tuple(warnings))

    def diagnosed_kind(default: str) -> str:
        if diagnosed_reason:
            return failure_kind_for_reason(diagnosed_reason) or default
        classified = child_failures.classify(signal_text)
        if classified is not None:
            return failure_kind_for_reason(classified.code) or default
        return default

    if cancelled:
        return done(STATUS_CANCELLED, FAILURE_CANCELLED, 1)
    if provider_terminal_state is not None:
        kind = failure_kind_for_reason(provider_terminal_state) or FAILURE_PROVIDER_ERROR
        evidence.append(f"provider terminal state: {provider_terminal_state}")
        status = STATUS_CANCELLED if kind == FAILURE_CANCELLED else STATUS_FAILED
        return done(status, kind, 1)
    if harness_terminal_status == STATUS_CANCELLED:
        return done(STATUS_CANCELLED, FAILURE_CANCELLED, 1)
    if failure_reason:
        kind = failure_kind_for_reason(failure_reason, exit_code=child_exit_code)
        if kind == FAILURE_EXIT_NONZERO:
            kind = diagnosed_kind(FAILURE_EXIT_NONZERO)
        evidence.append(f"failure reason: {failure_reason}")
        return done(STATUS_FAILED, kind, _failed_exit(child_exit_code))
    if child_exit_code != 0:
        evidence.append(f"child exit code {child_exit_code}")
        return done(STATUS_FAILED, diagnosed_kind(FAILURE_EXIT_NONZERO), child_exit_code)
    if harness_terminal_status == STATUS_FAILED:
        evidence.append("harness reported a failed terminal event")
        return done(STATUS_FAILED, diagnosed_kind(FAILURE_PROVIDER_ERROR), 1)
    if unrecovered_error and child_failures.is_usage_limit(unrecovered_error):
        evidence.append("provider's last unrecovered error was a quota or usage limit")
        return done(STATUS_FAILED, FAILURE_PROVIDER_QUOTA, 1)
    # The deliverable verdict outranks the result-quality one: a caller that
    # passed ``--expect-file`` asked whether the deliverable exists, and an
    # exit-0 run with no assistant text and a missing file is both.
    if missing_deliverables:
        return done(STATUS_FAILED, FAILURE_DELIVERABLE_MISSING, 1)
    if result_quality in NO_OUTPUT_RESULT_QUALITIES and work_changed:
        evidence.append(f"child exited 0 with resultQuality={result_quality}")
        evidence.append("work summary shows file changes or commits")
        warnings.append(NO_ASSISTANT_TEXT_WITH_CHANGES_WARNING)
        return done(STATUS_SUCCEEDED, None, 0)
    if result_quality in NO_OUTPUT_RESULT_QUALITIES:
        evidence.append(f"child exited 0 with resultQuality={result_quality}")
        final_text = signal_text if final_attempt_signal_text is None else final_attempt_signal_text
        kind = (
            FAILURE_PROVIDER_QUOTA
            if child_failures.is_usage_limit(final_text)
            else FAILURE_NO_ASSISTANT_TEXT
        )
        return done(STATUS_FAILED, kind, 1)
    return done(STATUS_SUCCEEDED, None, 0)
