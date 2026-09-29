"""Typed classification for known child-harness diagnostic failures.

Callers must pass only stderr or normalized harness error/terminal-event text.
Assistant/model output is untrusted content and must never be classified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from delegate_agent import provider_errors, redaction


@dataclass(frozen=True)
class ChildFailure:
    code: str
    message: str


# The patterns live in the provider-error signature table; these names stay for the
# followup session-loss classifier below.
_THREAD_LOSS_PATTERNS = provider_errors.THREAD_LOSS_PATTERNS
_CLAUDE_SESSION_LOSS_PATTERNS = (
    re.compile(r"\bunknown session\b", re.IGNORECASE),
    re.compile(
        r"\b(?:session|conversation)\b[^\n]{0,80}\b(?:not found|does not exist|unknown)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno (?:conversation|session) (?:found|exists?)\b", re.IGNORECASE),
)
_RESET_PATTERN = re.compile(
    r"\b(?:resets?(?: again)?|reset time|try again|available again)\b[^\n.\"}]{0,120}",
    re.IGNORECASE,
)
_LEGACY_MESSAGES = {
    "broker_binding_inactive": (
        "Broker rejected this launch with HTTP 403: this cell's identity binding is inactive. "
        "No vendor process ran; rebind the cell or retry from a healthy cell. "
        "This is not a Codex quota/token failure."
    ),
    "thread_lost": "Codex session state could not find the requested thread.",
    "auth_token_rejected": (
        "Child harness authentication failed because its token expired or was rejected."
    ),
}


def _compose_message(signature: provider_errors.Signature, line: str, hint: str) -> str:
    """What the signature says, the provider's own words, and the fix."""
    said = redaction.redact_string(" ".join(line.split()))[: provider_errors.MESSAGE_LIMIT]
    quoted = f' Provider said: "{said}".' if said else ""
    return f"{signature.summary}{quoted} {hint}"


def _message_for(hit: provider_errors.Classification, text: str) -> str:
    signature = hit.signature
    legacy = _LEGACY_MESSAGES.get(signature.id)
    if legacy is not None:
        return legacy
    if signature.id == "usage_limit":
        reset = _RESET_PATTERN.search(text)
        # This is the one branch that splices caller text into the message, and
        # the message is persisted to state.json and the completion report, so
        # the spliced window is redacted even though callers pass trusted text.
        window = redaction.redact_string(reset.group(0).strip().rstrip(".")) if reset else ""
        suffix = f" {window}." if window else ""
        return f"Child harness usage or quota limit reached.{suffix}"
    return _compose_message(signature, hit.line, signature.hint_for(None, hit.provider))


def classify(
    text: str,
    *,
    engine: str | None = None,
    status: int | None = None,
    code: str | None = None,
) -> ChildFailure | None:
    """Classify trusted child-harness diagnostics, never assistant/model text.

    Status and provider code decide first when the caller has them; the text
    patterns are the fallback. A throttle is deliberately not a failure code: a
    retryable 429 must never read as an account problem.
    """
    if not text.strip():
        return None
    hit = provider_errors.match(text, engine=engine, status=status, code=code)
    if hit is None or not hit.signature.failure_code:
        return None
    return ChildFailure(hit.signature.reason, _message_for(hit, text))


def failure_from_provider_error(
    record: dict[str, object] | None,
) -> ChildFailure | None:
    """The typed failure a `providerError` record names, or None when it names none.

    The record was classified status-first, so this never re-reads the message to
    decide the code; the message only supplies the reset window of a usage limit.
    """
    signature = provider_errors.signature_for_record(record)
    if signature is None or record is None or not signature.failure_code:
        return None
    message = record.get("message")
    text = message if isinstance(message, str) else ""
    legacy = _LEGACY_MESSAGES.get(signature.id)
    if legacy is not None:
        return ChildFailure(signature.reason, legacy)
    if signature.id == "usage_limit":
        hit = provider_errors.Classification(signature, "")
        return ChildFailure(signature.reason, _message_for(hit, text))
    hint = record.get("hint")
    return ChildFailure(
        signature.reason,
        _compose_message(
            signature, text, hint if isinstance(hint, str) and hint else signature.hint
        ),
    )


def is_usage_limit(text: str) -> bool:
    return bool(text.strip()) and provider_errors.matches_reason(text, "usage_limit")


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
