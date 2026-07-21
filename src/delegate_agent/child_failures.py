"""Typed classification for known child-harness diagnostic failures.

Callers must pass only stderr or normalized harness error/terminal-event text.
Assistant/model output is untrusted content and must never be classified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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
    re.compile(
        r"\brate limit\b[^\n]{0,80}\b(?:quota|usage|billing|subscription|account|credit)\b",
        re.IGNORECASE,
    ),
)
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
    if any(pattern.search(text) for pattern in _USAGE_PATTERNS):
        reset = _RESET_PATTERN.search(text)
        suffix = f" {reset.group(0).strip().rstrip('.')}." if reset else ""
        return ChildFailure("usage_limit", f"Child harness usage or quota limit reached.{suffix}")
    return None


def is_usage_limit(text: str) -> bool:
    return bool(text.strip()) and any(pattern.search(text) for pattern in _USAGE_PATTERNS)
