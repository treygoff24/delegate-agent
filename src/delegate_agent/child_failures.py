"""Typed classification for known child-harness diagnostic failures.

Callers must pass only stderr or normalized harness error/terminal-event text.
Assistant/model output is untrusted content and must never be classified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from delegate_agent import redaction


@dataclass(frozen=True)
class ChildFailure:
    code: str
    message: str


_THREAD_LOSS_PATTERNS = (
    re.compile(r"\bno thread with id\b", re.IGNORECASE),
    re.compile(r"\bno thread (?:found )?with id\b", re.IGNORECASE),
    re.compile(r"\bthread\b[^\n]{0,80}\b(?:not found|does not exist|unknown)\b", re.IGNORECASE),
    re.compile(
        r"\bstate (?:data)?base\b[^\n]{0,80}\bthread\b[^\n]{0,40}\b(?:lookup|find|load)[^\n]{0,20}\b(?:fail|error)",
        re.IGNORECASE,
    ),
    re.compile(r"\bthread lookup\b[^\n]{0,40}\b(?:fail|error)", re.IGNORECASE),
)
_CLAUDE_SESSION_LOSS_PATTERNS = (
    re.compile(r"\bunknown session\b", re.IGNORECASE),
    re.compile(
        r"\b(?:session|conversation)\b[^\n]{0,80}\b(?:not found|does not exist|unknown)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno (?:conversation|session) (?:found|exists?)\b", re.IGNORECASE),
)
_BINDING_NOT_ACTIVE_PATTERN = re.compile(
    r"^estate-harness: binding_not_active: Broker returned HTTP 403: binding_not_active[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_AUTH_PATTERNS = (
    re.compile(r"\btoken_expired\b", re.IGNORECASE),
    re.compile(r"\brefresh token was revoked\b", re.IGNORECASE),
    re.compile(
        r"\b(?:access |refresh )?token\b[^\n]{0,50}\b(?:expired|invalid|revoked)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:expired|invalid|revoked)\b[^\n]{0,50}\b(?:access |refresh )?token\b", re.IGNORECASE
    ),
    re.compile(r"\b401\b[^\n]{0,80}\b(?:unauthorized|token|auth(?:entication)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:unauthorized|auth(?:entication)? failed)\b[^\n]{0,80}\b401\b", re.IGNORECASE),
)
_USAGE_PATTERNS = (
    re.compile(r"\busage limit\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\bexceeded your current quota\b", re.IGNORECASE),
    # Google's canonical quota status (Gemini/Vertex surface it as 429
    # RESOURCE_EXHAUSTED); it names quota exhaustion, never transient load.
    re.compile(r"\bRESOURCE_EXHAUSTED\b"),
)
# A bare "rate limit" or HTTP 429 is often transient throttling, not an account/quota
# problem, so it only classifies when account-context wording appears on the same
# line and within a tight window of the trigger, in either order. "HTTP 429 Too
# Many Requests, backing off" next to an unrelated "memory usage: 82%" (or a
# wrapped command's `usage:` help line) must stay transient throttling: pairing
# them sent orchestrators to credential rotation for a retryable 429.
_RATE_LIMIT_TRIGGER = (
    r"\brate limit(?:s|ed)?\b"
    r"|\b(?:HTTP|status|code)[ :=]*429\b"
    r"|\b429\b[^\n]{0,40}\btoo many requests\b"
)
_ACCOUNT_CONTEXT_WORD = r"\b(?:quota|usage|billing|subscription|account|credit)\b"
_CONTEXT_WINDOW = r"[^\n]{0,80}"
_RATE_LIMIT_PATTERN = re.compile(
    rf"(?:{_RATE_LIMIT_TRIGGER}){_CONTEXT_WINDOW}(?:{_ACCOUNT_CONTEXT_WORD})"
    rf"|(?:{_ACCOUNT_CONTEXT_WORD}){_CONTEXT_WINDOW}(?:{_RATE_LIMIT_TRIGGER})",
    re.IGNORECASE,
)


def _matches_usage_limit(text: str) -> bool:
    if any(pattern.search(text) for pattern in _USAGE_PATTERNS):
        return True
    return bool(_RATE_LIMIT_PATTERN.search(text))


_RESET_PATTERN = re.compile(
    r"\b(?:resets?(?: again)?|reset time|try again|available again)\b[^\n.\"}]{0,120}",
    re.IGNORECASE,
)


def classify(text: str) -> ChildFailure | None:
    """Classify trusted child-harness diagnostics, never assistant/model text."""
    if not text.strip():
        return None
    if _BINDING_NOT_ACTIVE_PATTERN.search(text):
        return ChildFailure(
            "binding_not_active",
            "Broker rejected this launch with HTTP 403: this cell's identity binding is inactive. "
            "No vendor process ran; rebind the cell or retry from a healthy cell. "
            "This is not a Codex quota/token failure.",
        )
    if any(pattern.search(text) for pattern in _THREAD_LOSS_PATTERNS):
        return ChildFailure(
            "codex_thread_lost",
            "Codex session state could not find the requested thread.",
        )
    if any(pattern.search(text) for pattern in _AUTH_PATTERNS):
        return ChildFailure(
            "auth_failed",
            "Child harness authentication failed because its token expired or was rejected.",
        )
    if _matches_usage_limit(text):
        reset = _RESET_PATTERN.search(text)
        # This is the one branch that splices caller text into the message, and
        # the message is persisted to state.json and the completion report, so
        # the spliced window is redacted even though callers pass trusted text.
        window = redaction.redact_string(reset.group(0).strip().rstrip(".")) if reset else ""
        suffix = f" {window}." if window else ""
        return ChildFailure("usage_limit", f"Child harness usage or quota limit reached.{suffix}")
    return None


def is_usage_limit(text: str) -> bool:
    return bool(text.strip()) and _matches_usage_limit(text)


def _retry_advice(source: str | None) -> str:
    if source is None:
        return "Retry."
    return (
        f"Retry the followup, or carry its report into a new Run with `delegate resume {source}`."
    )


def classify_followup_session_failure(
    text: str, engine: str, *, source: str | None = None
) -> ChildFailure | None:
    """Normalize a native-session lookup failure for a Run that resumed a session.

    Only a Run whose source recorded a native session can reach a lookup failure
    (`followup` refuses one that did not before launching), so the message never
    tells the caller to relaunch with `--resumable`: the session was saved, and the
    launch could not find it. The usual cause is that the estate launcher's account
    autoselect landed on a different account than the one holding the session, so
    the message says so. ``source`` is the handle to offer to `delegate resume`.
    """
    if not text.strip():
        return None
    if engine == "codex" and any(pattern.search(text) for pattern in _THREAD_LOSS_PATTERNS):
        return ChildFailure(
            "session_expired",
            "Codex could not find this Run's native session (thread lookup failed). The session "
            "was saved, so this launch probably used a different CODEX_HOME or account than "
            "the one holding it (a launcher may have switched accounts), or the session was "
            f"pruned. {_retry_advice(source)}",
        )
    if engine == "claude" and any(
        pattern.search(text) for pattern in _CLAUDE_SESSION_LOSS_PATTERNS
    ):
        return ChildFailure(
            "session_expired",
            'Claude could not find this Run\'s native session ("No conversation found"). The '
            "session was saved, so this launch probably ran under a different account than the "
            "one holding it: the estate launcher's usage autoselect may have switched "
            f"accounts on --resume. {_retry_advice(source)}",
        )
    return None
