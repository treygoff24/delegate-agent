"""Degraded runs: the child ended its turn with its job unfinished.

A headless child ends when the model stops talking. A Claude Run that starts
its full test gate as a background task, or arms a Monitor, and then says
"Waiting on the full gate" has ended: the background task dies with the
session and nothing wakes the model. Delegate used to record such a Run as
``succeeded`` with ``resultQuality=ok`` and a completion report that reads
"Monitor armed; waiting for both suites to finish".

Status deliberately stays ``succeeded`` (a quiet Run whose changes landed must
stay adoptable; see ``outcome.py``). A Run that ends this way carries
``degraded: true`` and a ``degradedReason`` instead, so ``wait``, snapshots,
the launch envelope, the completion-report view, and workflow ``agent_meta()``
can tell a finished job from an abandoned one. Independent signals, each
chosen to keep false positives low against real Runs:

- Text (every engine): the final message is short, is not shaped like a
  finished report, and says it is waiting on, or will act after, work that has
  not finished. A finished report is long and structured, and a message that
  merely mentions a background process in the past tense does not match.
- Stream (Claude): Claude Code's ``background_tasks_changed`` snapshot still
  listed a running task when the ``result`` event arrived. That task was killed
  with the session, so whatever it was verifying never finished.
- Announced next step (every engine): the final message is short, not report
  shaped, and its last sentence announces an action the child is about to take
  ("Now let me write my report."). The turn ended before the action ran.
- Awaiting input (work mode): a short final message says the child is awaiting
  approval, confirmation, or an answer before it will implement anything, and the
  Run changed no files. Nobody can answer during a Run, so the job never started.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from delegate_agent.json_types import JsonObject
from delegate_agent.redaction import redact_string

# Closed enum, like ``outcome.FAILURE_KINDS``: consumers may switch on it.
DEGRADED_ENDED_WAITING = "ended_waiting_on_background_work"
DEGRADED_BACKGROUND_UNFINISHED = "background_work_unfinished_at_exit"
DEGRADED_ANNOUNCED_NEXT_STEP = "ended_announcing_next_step"
DEGRADED_AWAITING_INPUT = "ended_awaiting_input"
DEGRADED_REASONS = frozenset(
    {
        DEGRADED_ENDED_WAITING,
        DEGRADED_BACKGROUND_UNFINISHED,
        DEGRADED_ANNOUNCED_NEXT_STEP,
        DEGRADED_AWAITING_INPUT,
    }
)
# The record keys a degraded verdict writes (see ``Degraded.extra``).
DEGRADED_KEYS = ("degraded", "degradedReason", "degradedEvidence")
DEGRADED_WARNING_PREFIX = "degraded="

# A finished completion report is long; every real mid-job final message seen in
# the field was under 200 characters, every finished report over 1,800.
WAITING_TEXT_MAX_CHARS = 600
_SNIPPET_CHARS = 160
_TASK_DESCRIPTION_CHARS = 100
_TASKS_SHOWN = 3

_I = re.IGNORECASE | re.DOTALL
_I_WILL = r"i(?:'ll|\u2019ll| will)"
# Waiting on the requester is a blocked report, not unfinished background work.
_NOT_REQUESTER = (
    r"(?!\s+(?:your|you\b|the\s+user|the\s+operator|the\s+parent|the\s+maintainer|"
    r"approval|confirmation|a\s+decision|an\s+answer|input|a\s+reply|review\b|feedback))"
)
_JOB_WORDS = (
    r"suite|suites|gate|tests?|build|builds|run|runs|job|jobs|checks?|enumeration|"
    r"process|scan|lint|verify|pytest|compile|deploy|migration|import|sweep|replay"
)
_JOB = r"(?:" + _JOB_WORDS + r")"
# "Waiting on X" describes the child's own unfinished work only when X is a job it
# ran (or something completing, or the harness waking it), never a third party's
# independent result: `Done. Waiting for CI to post its independent result.` is a
# finished child handing off, not an abandoned gate.
_THIRD_PARTY_WORDS = (
    r"ci|reviewers?|reviews?|maintainers?|humans?|upstream|third[- ]party|"
    r"independent(?:ly)?|external(?:ly)?|someone|somebody|teammates?|owner"
)
_NOT_THIRD_PARTY = r"(?![^.;\n]{0,80}?\b(?:" + _THIRD_PARTY_WORDS + r")\b)"
_OWN_WORK = (
    r"(?=[^.;\n]{0,80}?\b(?:" + _JOB_WORDS + r"|finish(?:es|ed)?|complete[sd]?|done|returns?|"
    r"lands?|exits?|wake|notify|ping)\b)" + _NOT_THIRD_PARTY
)
_THIRD_PARTY = re.compile(r"\b(?:" + _THIRD_PARTY_WORDS + r")\b", re.IGNORECASE)

_WAITING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Waiting on the full gate." / "Monitor armed; waiting for both suites".
    re.compile(r"\bwaiting\s+(?:on|for)\b" + _NOT_REQUESTER + _OWN_WORK, _I),
    # Possessive "(on|for)" so backtracking cannot skip it and defeat the requester guard.
    re.compile(
        r"\b(?:"
        + _I_WILL
        + r"|let me|going to)\s+wait\b(?:\s+(?:on|for))?+"
        + _NOT_REQUESTER
        + _OWN_WORK,
        _I,
    ),
    # "The full suite is still running": a job noun near "still running".
    re.compile(
        r"\b" + _JOB + r"\b[^.;\n]{0,40}\bstill\s+(?:running|going|in\s+progress|executing)\b",
        _I,
    ),
    # "Monitor armed", "the monitors will report ...".
    re.compile(
        r"\b(?:monitors?|watchers?)\b[^.\n]{0,40}"
        r"\b(?:armed|will\s+(?:fire|report|flag|wake|notify|ping|tell))\b",
        _I,
    ),
    # "the harness will wake me", "I'll be notified".
    re.compile(
        r"\bwill\s+(?:wake|notify|ping)\s+me\b"
        r"|\b" + _I_WILL + r"\s+be\s+(?:notified|pinged|woken)\b",
        _I,
    ),
    # "I'll commit when it finishes", "I'll pick this up when they complete".
    # The condition must be the child's own job: "I'll commit when you approve"
    # and "I'll report after CI posts the result" are handoffs, not abandoned work.
    re.compile(
        r"\b" + _I_WILL + r"\s+"
        r"(?:pick\s+(?:this|it)\s+up|finish|commit|write|continue|resume|come\s+back|"
        r"check\s+back|circle\s+back|report|wrap\s+up|follow\s+up|proceed)\b"
        r"[^.\n]{0,100}?\b(?:when|once|after|as\s+soon\s+as)\b" + _NOT_REQUESTER + _OWN_WORK,
        _I,
    ),
    # "The commit and report follow once both are back.", "results come next".
    re.compile(
        r"\bfollows?\s+(?:once|when|after)\b"
        + _NOT_REQUESTER
        + _NOT_THIRD_PARTY
        + r"|\bresults?\s+(?:come|comes|coming)\s+next\b",
        _I,
    ),
)
# Checked even in a message shaped like a finished report: an explicit
# present-tense statement that the child's own job is still running ("Status:
# completed implementation; the full gate is still running") is not a finished
# job however the message is laid out. Past tense ("was still running when I
# checked") and third-party work do not match.
_EXPLICIT_UNFINISHED = re.compile(
    r"\b"
    + _JOB
    + r"\b[^.;\n]{0,40}\b(?:is|are)\s+still\s+(?:running|going|in\s+progress|executing)\b",
    _I,
)
# "not waiting", "no need to wait", "without waiting": the match is a denial.
_NEGATION = re.compile(
    r"\b(?:not|no|never|nothing|without|don'?t|doesn'?t|didn'?t|isn'?t|aren'?t|wasn'?t)\W+"
    r"(?:\w+\W+){0,3}$",
    re.IGNORECASE,
)

# What a child quotes (a tool's output, a log line, a code span) is not its own
# statement: `the tool printed "Waiting on the gate."` reports on another program.
_QUOTED = re.compile(r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d", re.DOTALL)

_STATUS_LINE = re.compile(
    r"^\W*(?:status|verdict)\W*(?:\w+\W+){0,2}?(?:completed?|done|failed|blocked|passed|ok)\b",
    re.IGNORECASE | re.MULTILINE,
)
_REPORT_HEADER = re.compile(
    r"^\s*#{1,6}\s+(?:summary|status|what\b|verification|files\b|completion report|delegate completion)",
    re.IGNORECASE | re.MULTILINE,
)
_BULLET_LINE = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)


@dataclass(frozen=True)
class Degraded:
    reason: str
    evidence: tuple[str, ...]

    def extra(self) -> JsonObject:
        """The record fields, merged into the run record and every envelope."""
        return {
            "degraded": True,
            "degradedReason": self.reason,
            "degradedEvidence": list(self.evidence),
        }

    def warning(self) -> str:
        return degraded_warning(self.reason)


def degraded_warning(reason: str) -> str:
    if reason == DEGRADED_ENDED_WAITING:
        detail = (
            "the child ended its turn saying it was waiting on unfinished work. Ending the "
            "turn ended the Run, so nothing was waiting and the job may never have finished"
        )
    elif reason == DEGRADED_BACKGROUND_UNFINISHED:
        detail = (
            "background work was still running when the child ended its turn and was killed "
            "with the session, so whatever it was checking never finished"
        )
    elif reason == DEGRADED_ANNOUNCED_NEXT_STEP:
        detail = (
            "the child's final message announced its next step and stopped, so that step never "
            "ran (a report or deliverable it was about to write may not exist). Check the "
            "deliverable exists; if not, continue the Run with: "
            'delegate resume <run> "Do the step you announced, then finish with a full report"'
        )
    elif reason == DEGRADED_AWAITING_INPUT:
        detail = (
            "the child ended its turn awaiting approval or an answer nobody can give during a "
            "Run, and changed no files, so the job never started. Re-run or resume with the "
            'approval stated: delegate resume <run> "Approved: carry out the task now, do not '
            'ask for confirmation"'
        )
    else:
        detail = "the Run ended before its work did"
    return (
        f"{DEGRADED_WARNING_PREFIX}{reason}: {detail}. The Run counts as succeeded so its work "
        "can be adopted; read the completion report and confirm the job before accepting it."
    )


def strip_degraded(extra: JsonObject) -> None:
    """Drop the degraded verdict from a record that did not end up succeeded.

    A Run cancelled (or already terminal some other way) while it was being
    finalized is not "succeeded but degraded"; the flag, its reason and evidence,
    and its warning all go.
    """
    for key in DEGRADED_KEYS:
        extra.pop(key, None)
    warnings = extra.get("warnings")
    if isinstance(warnings, list):
        extra["warnings"] = [
            warning
            for warning in warnings
            if not (isinstance(warning, str) and warning.startswith(DEGRADED_WARNING_PREFIX))
        ]


def is_report_shaped(text: str) -> bool:
    """Does this final message read as a finished, structured report?"""
    if _STATUS_LINE.search(text) or _REPORT_HEADER.search(text):
        return True
    return len(_BULLET_LINE.findall(text)) >= 2


def waiting_on_unfinished_work(text: str | None) -> str | None:
    """The clause where a short final message says it is waiting, else None.

    Only a short message that is not shaped like a finished report can match, so
    a long report that discusses waiting, or a blocked report that names its
    background process in the past tense, never does.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > WAITING_TEXT_MAX_CHARS:
        return None
    # Same length as the message, so match offsets index the original text too.
    own = _QUOTED.sub(lambda quoted: " " * len(quoted.group()), stripped)
    patterns = (_EXPLICIT_UNFINISHED,) if is_report_shaped(stripped) else _WAITING_PATTERNS
    for pattern in patterns:
        for match in pattern.finditer(own):
            if _NEGATION.search(own[max(0, match.start() - 40) : match.start()]):
                continue
            if pattern is _EXPLICIT_UNFINISHED and _THIRD_PARTY.search(
                _clause_text(own, match.start(), match.end())
            ):
                continue
            return _clause(stripped, match.start(), match.end())
    return None


# The child's last sentence announces an action it is about to take. Deliberately a
# closed verb list: "Let me know ..." and "I'll leave the rest to you" are sign-offs.
_ANNOUNCE_VERBS = (
    r"write|writing|run|running|apply|fix|check|read|create|update|implement|start|begin|verify|"
    r"look|review|compile|produce|draft|put|make|add|commit|generate|finish|summari[sz]e|compose|"
    r"prepare|assemble|compile|save|edit|patch|test|inspect|examine|go|do|proceed|continue|"
    r"kick|launch|execute|build|search|grep|open|dig"
)
_ANNOUNCE = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|alright|so|and|now|next|then|first|finally|right)\b[\s,:;-]*)*"
    r"(?:(?:let me|let\u2019s|let's)|" + _I_WILL + r"(?:\s+now)?|i(?:'m|\u2019m| am) going to|"
    r"i(?:'m|\u2019m| am) about to)"
    r"\s+(?:now\s+|just\s+|also\s+|go\s+ahead\s+and\s+)?(?:" + _ANNOUNCE_VERBS + r")\b",
    re.IGNORECASE,
)
# A conditional or handed-off action is an offer, not an announcement.
_ANNOUNCE_HEDGE = re.compile(
    r"\b(?:if|unless|when|whenever|once|should|otherwise|in case|want|need me)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
ANNOUNCE_TEXT_MAX_CHARS = 400


def announcing_next_step(text: str | None) -> str | None:
    """The last sentence when a short, non-report message ends announcing an action."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > ANNOUNCE_TEXT_MAX_CHARS or is_report_shaped(stripped):
        return None
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(stripped) if part.strip()]
    if not sentences:
        return None
    last = sentences[-1]
    if not _ANNOUNCE.match(last) or _ANNOUNCE_HEDGE.search(last):
        return None
    return _clause(last, 0, len(last))


# Work-mode self-gate: the child stopped for an approval nobody can give.
_AWAITING_INPUT = re.compile(
    r"\b(?:awaiting|waiting\s+(?:on|for)|pending)\s+(?:your\s+|the\s+(?:user|operator|parent|"
    r"maintainer)(?:'s|\u2019s)?\s+|human\s+)?(?:approval|confirmation|go-?ahead|sign-?off|"
    r"a\s+decision|an\s+answer|a\s+(?:reply|response)|(?:input|reply|response|decision|answer)\b)",
    re.IGNORECASE,
)
_GATED_JOB = re.compile(
    r"\b(?:implement\w*|design|plan|proceed\w*|changes?|fix(?:es)?|edit\w*|appl(?:y|ied|ying)|"
    r"execut\w+|begin|start|approach|patch\w*|refactor\w*|migrat\w+)\b",
    re.IGNORECASE,
)
# "The plan is complete." then "Awaiting approval of the implementation": a plan-only
# task that finished its deliverable is a report, not a parked Run.
_DELIVERED = re.compile(r"\b(?:complete[d]?|done|delivered|attached|finished)\b", re.IGNORECASE)
_ASKS_TO_PROCEED = re.compile(
    r"\b(?:should|shall)\s+i\s+(?:proceed|go\s+ahead|implement|apply|continue)\b[^.!]*\?\W*$"
    r"|\bdo\s+you\s+want\s+me\s+to\s+(?:proceed|go\s+ahead|implement|apply)\b[^.!]*\?\W*$"
    r"|\bplease\s+(?:approve|confirm)\b[^.!]{0,80}\b(?:proceed|continue|implement)",
    re.IGNORECASE,
)


def awaiting_input(text: str | None) -> str | None:
    """The clause where a short final message parks on an approval, else None."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > WAITING_TEXT_MAX_CHARS:
        return None
    own = _QUOTED.sub(lambda quoted: " " * len(quoted.group()), stripped)
    for pattern in (_AWAITING_INPUT, _ASKS_TO_PROCEED):
        for match in pattern.finditer(own):
            if _NEGATION.search(own[max(0, match.start() - 40) : match.start()]):
                continue
            # The message must not say the requested deliverable is already complete:
            # "The plan is complete. Should I proceed?" is a finished plan-only Run.
            sentence = _clause_text(own, match.start(), match.end())
            rest = own.replace(sentence, " ")
            if _DELIVERED.search(rest):
                continue
            # An "awaiting approval" clause must also gate unfinished work.
            if pattern is _AWAITING_INPUT and not _GATED_JOB.search(sentence):
                continue
            return _clause(stripped, match.start(), match.end())
    return None


def _clause_text(text: str, start: int, end: int) -> str:
    """The sentence around a match, unbounded and unredacted."""
    left = max(text.rfind(mark, 0, start) for mark in (". ", "! ", "? ", "; ", "\n")) + 1
    right_candidates = [text.find(mark, end) for mark in (". ", "! ", "? ", "; ", "\n")]
    right = min((pos for pos in right_candidates if pos != -1), default=len(text) - 1)
    return text[left : right + 1].strip()


def _clause(text: str, start: int, end: int) -> str:
    """The sentence around a match, bounded and redacted for the record."""
    left = max(text.rfind(mark, 0, start) for mark in (". ", "! ", "? ", "\n")) + 1
    right_candidates = [text.find(mark, end) for mark in (". ", "! ", "? ", "\n")]
    right = min((pos for pos in right_candidates if pos != -1), default=len(text) - 1)
    clause = text[left : right + 1].strip()
    if len(clause) > _SNIPPET_CHARS:
        clause = clause[: _SNIPPET_CHARS - 1].rstrip() + "…"
    return redact_string(clause)


def _task_summary(tasks: Sequence[str]) -> str:
    shown: list[str] = []
    for task in tasks[:_TASKS_SHOWN]:
        label = " ".join(task.split())
        if len(label) > _TASK_DESCRIPTION_CHARS:
            label = label[: _TASK_DESCRIPTION_CHARS - 1].rstrip() + "…"
        shown.append(f'"{redact_string(label)}"')
    extra = len(tasks) - len(shown)
    tail = f" and {extra} more" if extra > 0 else ""
    return ", ".join(shown) + tail


def assess(
    report_text: str | None,
    *,
    background_tasks: Sequence[str] = (),
    mode: str | None = None,
    files_changed: bool | None = None,
) -> Degraded | None:
    """Judge a succeeded Run's final message and stream for an abandoned job.

    ``report_text`` is the child's own final message. ``background_tasks`` are
    the descriptions of tasks the stream showed still running when the turn
    ended (empty when the harness reports none or is not Claude). ``mode`` is the
    Run mode; only ``work`` can be awaiting input. ``files_changed`` is the work
    summary's verdict (None when the Run has no work summary to consult).
    """
    clause = waiting_on_unfinished_work(report_text)
    evidence: list[str] = []
    if clause is not None:
        evidence.append(f'final message says it is waiting: "{clause}"')
    if background_tasks:
        evidence.append(
            f"{len(background_tasks)} background task(s) still running when the turn ended: "
            f"{_task_summary(background_tasks)}"
        )
    if clause is not None:
        return Degraded(DEGRADED_ENDED_WAITING, tuple(evidence))
    if background_tasks:
        return Degraded(DEGRADED_BACKGROUND_UNFINISHED, tuple(evidence))
    if mode == "work" and files_changed is not True:
        parked = awaiting_input(report_text)
        if parked is not None:
            return Degraded(
                DEGRADED_AWAITING_INPUT,
                (f'final message is waiting for input nobody can give: "{parked}"',),
            )
    announced = announcing_next_step(report_text)
    if announced is not None:
        return Degraded(
            DEGRADED_ANNOUNCED_NEXT_STEP,
            (f'final message ends announcing a step that never ran: "{announced}"',),
        )
    return None


def degraded_fields(record: object) -> JsonObject:
    """``degraded`` and ``degradedReason`` from a persisted record, or ``{}``.

    Surfaces that decide whether a caller may accept a Run copy these two keys;
    the evidence list stays on the record and the snapshot.
    """
    if not isinstance(record, dict) or record.get("degraded") is not True:
        return {}
    fields: JsonObject = {"degraded": True}
    reason = record.get("degradedReason")
    if isinstance(reason, str) and reason:
        fields["degradedReason"] = reason
    return fields
