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
    re.compile(
        r"\bstate (?:data)?base\b[^\n]{0,80}\bthread\b[^\n]{0,40}\b(?:lookup|find|load)[^\n]{0,20}\b(?:fail|error)",
        re.IGNORECASE,
    ),
    re.compile(r"\bthread lookup\b[^\n]{0,40}\b(?:fail|error)", re.IGNORECASE),
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
)
# A bare "rate limit" is often transient throttling, not an account/quota
# problem, so it only classifies when account-context wording appears anywhere
# in the same diagnostic text (before or after, any line).
_RATE_LIMIT_PATTERN = re.compile(r"\brate limit(?:s|ed)?\b", re.IGNORECASE)
_ACCOUNT_CONTEXT_PATTERN = re.compile(
    r"\b(?:quota|usage|billing|subscription|account|credit)\b", re.IGNORECASE
)


def _matches_usage_limit(text: str) -> bool:
    if any(pattern.search(text) for pattern in _USAGE_PATTERNS):
        return True
    return bool(_RATE_LIMIT_PATTERN.search(text)) and bool(_ACCOUNT_CONTEXT_PATTERN.search(text))


_RESET_PATTERN = re.compile(
    r"\b(?:resets?(?: again)?|reset time|try again|available again)\b[^\n.\"}]{0,120}",
    re.IGNORECASE,
)


def classify(text: str) -> ChildFailure | None:
    """Classify trusted child-harness diagnostics, never assistant/model text."""
    if not text.strip():
        return None
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
