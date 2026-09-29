"""One automatic resume after a transient provider drop.

A Codex or Claude work run that dies on a transient stream drop (the canonical
case is the Codex websocket closing before the response completes) and saved its
native session gets exactly one continuation, launched the way ``delegate
followup`` launches one: the same session, so the lineage is one conversation.
The continuation is a new run linked to the first by ``followupOf`` and stamped
``autoResume`` so nothing about it reads as an operator action.

This module holds the decision and the record; ``cli.main`` runs the launch,
because building a request is the CLI's job. Never applies to safe mode, call
mode, pass-through, structured-output runs (the workflow supervisor owns that
retry), or a run that saved no session. There is no second automatic attempt:
the continuation is launched once and its result is final.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from delegate_agent import provider_errors
from delegate_agent.json_types import JsonObject

AUTO_RESUME_ENGINES = ("codex", "claude")
# An automatic continuation must land where the first attempt's work still is:
# the plain checkout or a worktree that outlives the run. A temporary worktree is
# gone, and a fresh one would resume the conversation without the files.
CARRYING_LIFECYCLES = ("none", "persistent", "attached")

CONTINUATION_PROMPT = (
    "The provider connection dropped mid-turn (a transient stream error), so your "
    "previous turn did not finish. Continue the task from where you left off. "
    "Check the current state of the workspace first and do not redo work that is "
    "already done. When you finish, give your completion report as usual."
)


@dataclass(frozen=True)
class RunNote:
    """What a finished tracked run tells the auto-resume decision."""

    run_id: str
    alias: str
    engine: str
    mode: str
    status: str
    provider_error: JsonObject | None
    session_id: str | None
    isolation_lifecycle: str
    structured: bool


_NOTES: ContextVar[list[RunNote] | None] = ContextVar("delegate_auto_resume_notes", default=None)


@contextlib.contextmanager
def watching() -> Iterator[list[RunNote]]:
    """Collect the runs finalized inside the block (the runner reports each one)."""
    notes: list[RunNote] = []
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)


def note_run(note: RunNote) -> None:
    """Called by the runner at finalization; a no-op outside a `watching` block."""
    notes = _NOTES.get()
    if notes is not None:
        notes.append(note)


def skip_reason(note: RunNote, *, enabled: bool, already_automatic: bool) -> str | None:
    """Why this run gets no automatic resume, or None when it does.

    The reasons are for tests and diagnostics; only an eligible run whose
    continuation could not be built is ever surfaced to the caller.
    """
    if not enabled:
        return "disabled"
    if already_automatic:
        return "already_automatic"
    if note.engine not in AUTO_RESUME_ENGINES:
        return "engine"
    if note.mode != "work":
        return "mode"
    if note.structured:
        return "structured_output"
    if note.status != "failed":
        return "not_failed"
    signature = provider_errors.signature_for_record(note.provider_error)
    if (
        signature is None
        or signature.klass != provider_errors.CLASS_TRANSIENT
        or not signature.auto_resume
    ):
        return "not_a_transient_drop"
    if not note.session_id:
        return "no_saved_session"
    if note.isolation_lifecycle not in CARRYING_LIFECYCLES:
        return "workspace_not_carried"
    return None


def annotation(note: RunNote) -> JsonObject:
    """The record stamped on the continuation run and its envelope."""
    provider_error = note.provider_error or {}
    return {
        "automatic": True,
        "attempt": 1,
        "of": {"runId": note.run_id, "alias": note.alias},
        "trigger": {
            "signature": provider_error.get("signature"),
            "class": provider_error.get("class"),
        },
    }


def skipped_annotation(note: RunNote, reason: str) -> JsonObject:
    """The record on the first run's envelope when its resume could not be launched."""
    return {"automatic": True, "attempted": False, "of": annotation(note)["of"], "reason": reason}
