from __future__ import annotations

from typing import Literal, TypeAlias

from delegate_agent.degraded import strip_degraded
from delegate_agent.json_types import JsonObject

TerminalState: TypeAlias = Literal[
    "completed_verified",
    "completed_unverified",
    "blocked_human",
    "blocked_dependency",
    "provider_refusal",
    "provider_cancelled",
    "provider_max_turns",
    "stalled",
    "failed",
]

COMPLETED_VERIFIED: TerminalState = "completed_verified"
COMPLETED_UNVERIFIED: TerminalState = "completed_unverified"
BLOCKED_HUMAN: TerminalState = "blocked_human"
BLOCKED_DEPENDENCY: TerminalState = "blocked_dependency"
PROVIDER_REFUSAL: TerminalState = "provider_refusal"
PROVIDER_CANCELLED: TerminalState = "provider_cancelled"
PROVIDER_MAX_TURNS: TerminalState = "provider_max_turns"
STALLED: TerminalState = "stalled"
FAILED: TerminalState = "failed"

TERMINAL_STATES = frozenset(
    {
        COMPLETED_VERIFIED,
        COMPLETED_UNVERIFIED,
        BLOCKED_HUMAN,
        BLOCKED_DEPENDENCY,
        PROVIDER_REFUSAL,
        PROVIDER_CANCELLED,
        PROVIDER_MAX_TURNS,
        STALLED,
        FAILED,
    }
)

PROVIDER_FAILURE_STATES = frozenset({PROVIDER_REFUSAL, PROVIDER_CANCELLED, PROVIDER_MAX_TURNS})

# What each state means, in words, for the terminal line and the JSON receipt.
# `completed_unverified` is the ordinary success state: every successful run
# lands there unless a verification step marked it verified, so a reader must
# not take "unverified" as a sign the output is suspect.
TERMINAL_STATE_MEANINGS: dict[str, str] = {
    COMPLETED_VERIFIED: "completed, and a verification step checked the result",
    COMPLETED_UNVERIFIED: (
        "completed normally; no verification step ran, so nothing checked the output"
    ),
    BLOCKED_HUMAN: "stopped to wait for a person's decision",
    BLOCKED_DEPENDENCY: "stopped to wait on something outside the run",
    PROVIDER_REFUSAL: "the model provider refused the request",
    PROVIDER_CANCELLED: "the model provider cancelled the response",
    PROVIDER_MAX_TURNS: "the harness hit its turn limit before finishing",
    STALLED: "the run stopped making progress and was stopped",
    FAILED: "the run failed or was cancelled",
}


def terminal_state_meaning(state: object) -> str | None:
    """The plain-words meaning of a terminal state, or None for an unknown value."""
    if not isinstance(state, str):
        return None
    return TERMINAL_STATE_MEANINGS.get(state)


OPERATOR_CANCEL_REASON = "cancelled_by_user"


def apply_operator_cancel_override(extra: JsonObject) -> None:
    """Replace any child/provider terminal receipt with the operator outcome."""
    extra["failureReason"] = OPERATOR_CANCEL_REASON
    extra["failureKind"] = "cancelled"
    extra["exitCode"] = 1
    extra["terminalState"] = FAILED
    extra["terminalStateMeaning"] = TERMINAL_STATE_MEANINGS[FAILED]

    existing_record = extra.get("terminalRecord")
    terminal_record: JsonObject = dict(existing_record) if isinstance(existing_record, dict) else {}
    terminal_record["state"] = FAILED
    terminal_record["reason"] = OPERATOR_CANCEL_REASON
    for key in ("error", "message", "nextActions", "reasonTruncated", "reasonChars"):
        terminal_record.pop(key, None)
    extra["terminalRecord"] = terminal_record

    for key in (
        "error",
        "message",
        "nextActions",
        "terminalEvent",
        "terminalStatus",
        "outcomeEvidence",
    ):
        extra.pop(key, None)
    # A cancelled Run is not "succeeded but degraded".
    strip_degraded(extra)
