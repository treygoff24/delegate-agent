"""Read-only journal summaries that tell an operator why a workflow is stopped.

``workflow status`` used to show ``paused`` with no reason; the operator had to
read the journal. These helpers derive the reason from journal rows so status
and ``approve`` errors can name the gate, the last failure, and the next command.
"""

from __future__ import annotations

from delegate_agent.json_types import JsonObject
from delegate_agent.workflows import runtime

FAILURE_EVENTS = frozenset(
    {"agent_failed", "agent_attempt_failed", "agent_timeout", "workflow_failed"}
)
_MAX_LINE = 240
_MAX_TIMEOUTS = 5


def _clip(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= _MAX_LINE else one_line[: _MAX_LINE - 3] + "..."


def _real(event: JsonObject) -> bool:
    return event.get("simulated") is not True and event.get("dryRun") is not True


def _who(event: JsonObject) -> str:
    who = str(event.get("label") or event.get("key") or "workflow")
    item = event.get("item")
    return f"{who} (item {item})" if item else who


def soft_park_reason(wf_id: object, parked_items: object) -> JsonObject:
    """Why a soft-parked workflow is paused: no gate, just items waiting for a resume.

    A soft park is not an approval gate. The scheduler already ran every other
    runnable item; resuming replays the script and re-enters the parked scopes.
    """
    items = [str(item) for item in parked_items] if isinstance(parked_items, list) else []
    named = ", ".join(items) if items else "some items"
    command = f"delegate workflow resume {wf_id}"
    return {
        "softPark": True,
        "parkedItems": items,
        "summary": _clip(
            f"soft-parked (not a gate): {named} wait for a resume; every other runnable "
            "item already ran"
        ),
        "next": command,
        "nextActions": [command],
    }


def failure_summary(event: JsonObject) -> str:
    """One line naming what failed and why, from a failure journal row."""
    kind = event.get("type")
    if kind == "agent_timeout":
        detail = f"timed out after {event.get('timeout')}s"
        if event.get("engine"):
            detail += f" on {event['engine']}"
        if event.get("nextEngine"):
            detail += f"; next seat {event['nextEngine']}"
    elif kind == "agent_attempt_failed":
        outcome = event.get("childAttemptOutcome")
        reason = outcome.get("failureReason") if isinstance(outcome, dict) else None
        detail = f"attempt {event.get('attempt')} failed: {reason or 'unknown'}"
        if event.get("engine"):
            detail += f" on {event['engine']}"
    else:
        detail = str(event.get("error") or kind)
    return _clip(f"{_who(event)}: {detail}")


def timeouts(events: list[JsonObject]) -> list[JsonObject]:
    """The most recent agent timeouts, oldest first, as compact rows."""
    rows: list[JsonObject] = []
    for event in events:
        if event.get("type") != "agent_timeout" or not _real(event):
            continue
        row: JsonObject = {"seq": event.get("seq"), "at": event.get("at")}
        for name in ("key", "label", "item", "engine", "timeout", "nextEngine", "runId"):
            if event.get(name) is not None:
                row[name] = event[name]
        rows.append(row)
    return rows[-_MAX_TIMEOUTS:]


def pause_reason(gate_key: object, result_hash: object, events: list[JsonObject]) -> JsonObject:
    """Why a paused workflow is paused: the gate, the last failure, any rejection."""
    gate: JsonObject | None = None
    for index, event in enumerate(events):
        if (
            event.get("type") == "gate"
            and _real(event)
            and event.get("key") == gate_key
            and (not isinstance(result_hash, str) or event.get("gateResultHash") == result_hash)
        ):
            gate = event
            gate_index = index
    pause: JsonObject = {"gateKey": gate_key}
    parts: list[str] = []
    horizon = events
    if gate is not None:
        actions = runtime._declared_gate_actions(gate)
        name = gate.get("gateName")
        result = gate.get("result")
        pause["gateName"] = name
        pause["actions"] = actions
        # A script may put a title and an assignee in the question it asks.
        for field in ("title", "assignee"):
            value = result.get(field) if isinstance(result, dict) else None
            if isinstance(value, str) and value:
                pause[field] = value
        if gate.get("child") is not None:
            pause["child"] = gate["child"]
        horizon = events[:gate_index]
        described = f"gate {name or gate_key}"
        if pause.get("assignee"):
            described += f" assigned to {pause['assignee']}"
        parts.append(f"paused at {described}; actions: {', '.join(actions)}")
    else:
        parts.append(f"paused at gate {gate_key}")
    failure = next(
        (e for e in reversed(horizon) if e.get("type") in FAILURE_EVENTS and _real(e)), None
    )
    if failure is not None:
        pause["failure"] = failure_summary(failure)
        parts.append(f"last failure before it: {pause['failure']}")
    rejection = next(
        (e for e in reversed(events) if e.get("type") == "agent_rejected" and _real(e)), None
    )
    if rejection is not None:
        pause["rejection"] = {
            name: rejection[name]
            for name in ("key", "label", "item", "reason", "rejectedBy")
            if rejection.get(name) is not None
        }
        parts.append(
            f"agent_rejected {rejection.get('label') or rejection.get('key')}: "
            f"{_clip(str(rejection.get('reason')))}"
        )
    pause["summary"] = _clip("; ".join(parts)) if not rejection else "; ".join(parts)
    return pause


def approve_refusal_hint(wf_id: str, status: object, journal_failure: str | None = None) -> str:
    """The real next step when ``approve`` finds no open gate."""
    if status in {"failed", "stalled", "killed"}:
        tail = f" Last failure: {journal_failure}." if journal_failure else ""
        return (
            f"Workflow {wf_id} is {status}, not paused at a gate, so there is nothing to "
            f"approve.{tail} To continue it, run `delegate workflow resume {wf_id}`."
        )
    if status in {"running", "starting", "created"}:
        return (
            f"Workflow {wf_id} is still {status}; its gate is not open yet. Run "
            f"`delegate workflow wait {wf_id}` until it is paused, then approve."
        )
    if status == "succeeded":
        return f"Workflow {wf_id} already succeeded; nothing to approve."
    return f"Workflow {wf_id} is not waiting on a gate (status: {status})."
