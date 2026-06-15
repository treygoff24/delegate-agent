from __future__ import annotations

import re

from delegate_agent.json_types import JsonValue

SLACK_WEBHOOK_SCHEME = "https"
URL_SCHEME_SEPARATOR = "://"
SLACK_WEBHOOK_HOST_PATTERN = r"hooks\.slack(?:-gov)?\.com"
SLACK_WEBHOOK_PATH_PATTERN = r"/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"

REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization header value, quoted or bare. The optional quote after the key
    # tolerates JSON ({"Authorization": "..."}); the value is bounded on the right
    # so we don't swallow trailing structure (closing quote/brace/&/,).
    (
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&}\"'\r\n][^\r\n,&}]*)"
        ),
        r"\1***",
    ),
    # Bare scheme token not behind an Authorization key (e.g. "Bearer eyJ...").
    (
        re.compile(r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1***",
    ),
    # Bracketed environment assignments, e.g. os.environ["OPENAI_API_KEY"] = "..."
    # and env['DB_PASSWORD']='...'. Keep this before the generic key matcher: the
    # separator between the secret key and value is outside the bracketed lookup.
    (
        re.compile(
            r"(?i)\b((?:os\.environ|env)\[\s*[\"'][^\"'\]\r\n]*"
            r"(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|private[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|auth[_-]?token|authtoken|"
            r"client[_-]?secret|password|passwd|secret|token)"
            r"[^\"'\]\r\n]*[\"']\s*\]\s*=\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&};\"'\r\n][^\r\n,&};]*)"
        ),
        r"\1***",
    ),
    # Named credential keys with the value quoted, bare, or JSON-quoted. The left
    # edge anchors on a non-alphanumeric character (or string start) rather than
    # \b, so env-style prefixes joined by "_" still redact (OPENAI_API_KEY=,
    # DB_PASSWORD=, aws_secret_access_key=). Separator is preserved so the shape
    # stays readable.
    (
        re.compile(
            r"(?i)(?:(?<=[^A-Za-z0-9])|^)("
            r"api[_-]?key|apikey|access[_-]?key|secret[_-]?key|private[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|auth[_-]?token|authtoken|"
            r"client[_-]?secret|password|passwd|secret|token"
            r")([\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,&};\"'\r\n][^\r\n,&};]*)"
        ),
        r"\1\2***",
    ),
    # Password embedded in a connection string: scheme://[user]:PASS@host. The
    # scheme length is bounded so a long dotted string cannot backtrack quadratically.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,40}://[^\s:/@]*:)[^\s:/@]+(@)"),
        r"\1***\2",
    ),
    # JWTs are anchored on the eyJ header (base64url of '{"') so this does not shred
    # ordinary dotted identifiers and tracebacks the parent agent needs to read.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "***",
    ),
    # PEM private key blocks are handled by _redact_pem_blocks() before this list
    # runs. A DOTALL regex here is both easier to make backtracking-prone and can
    # let keyed values like SECRET_KEY=<PEM> leak the body after the first newline.
    # Provider token shapes are prefix-anchored; we deliberately avoid a blanket
    # high-entropy matcher, which would redact legitimate hashes/IDs/output.
    (re.compile(r"\bsk-(?:proj|ant|svcacct)-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"), "sk-***"),
    (re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"), "gh***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_***"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{32,}\b"), "AIza***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "xox***"),
    # Stripe secret/restricted keys (live or test); publishable pk_ keys are
    # public by design and intentionally excluded.
    (re.compile(r"\b([sr]k_(?:live|test)_)[0-9A-Za-z]{10,}\b"), r"\1***"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{10,}\b"), "whsec_***"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"), "npm_***"),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"), "SG.***"),
    (
        re.compile(
            SLACK_WEBHOOK_SCHEME
            + URL_SCHEME_SEPARATOR
            + SLACK_WEBHOOK_HOST_PATTERN
            + SLACK_WEBHOOK_PATH_PATTERN
        ),
        "***",
    ),
]


PEM_BLOCK_PLACEHOLDER = "***PRIVATE KEY REDACTED***"
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _redact_pem_blocks(value: str) -> str:
    match = _PEM_BEGIN.search(value)
    if match is None:
        return value
    parts: list[str] = []
    pos = 0
    while match is not None:
        parts.append(value[pos : match.start()])
        end = _PEM_END.search(value, match.end())
        parts.append(PEM_BLOCK_PLACEHOLDER)
        if end is None:
            return "".join(parts)
        pos = end.end()
        match = _PEM_BEGIN.search(value, pos)
    parts.append(value[pos:])
    return "".join(parts)


def redact_string(value: str) -> str:
    redacted = _redact_pem_blocks(value)
    for pattern, replacement in REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value
