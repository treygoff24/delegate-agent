from __future__ import annotations

from typing import Literal, TypeAlias

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

OPERATOR_CANCEL_REASON = "cancelled_by_user"


def apply_operator_cancel_override(extra: JsonObject) -> None:
    """Replace any child/provider terminal receipt with the operator outcome."""
    extra["failureReason"] = OPERATOR_CANCEL_REASON
    extra["exitCode"] = 1
    extra["terminalState"] = FAILED

    existing_record = extra.get("terminalRecord")
    terminal_record: JsonObject = dict(existing_record) if isinstance(existing_record, dict) else {}
    terminal_record["state"] = FAILED
    terminal_record["reason"] = OPERATOR_CANCEL_REASON
    for key in ("error", "message", "nextActions", "reasonTruncated", "reasonChars"):
        terminal_record.pop(key, None)
    extra["terminalRecord"] = terminal_record

    for key in ("error", "message", "nextActions", "terminalEvent", "terminalStatus"):
        extra.pop(key, None)
