"""Provider errors as data: one engine-keyed signature table and the providerError record.

A child harness reports a provider failure as an HTTP-ish status, a provider
code, and a message. This module owns the single table that turns those into a
named *signature* with a persistence class (does retrying the same lane help?), a
scope (does the failure poison the lane, or only this request?), the failure
reason the rest of Delegate already branches on, and a one-line hint naming the
fix.

Classification is status-first:

1. a structured provider code (``insufficient_quota``, ``model_not_found``);
2. the HTTP-ish status, qualified by the message where one status covers several
   causes (a 429 that names a quota is a usage limit, a bare 429 is throttling;
   a 403 from the estate broker is a broker rejection);
3. only when neither decided, regexes over the terminal error text.

A message can therefore never override a status: an OMP 400 whose text says
"invalid token" is a rejected request, not an authentication failure.

Callers pass trusted diagnostic text only (stderr and normalized harness error
events), never assistant output. Everything that leaves this module is redacted
and size-bounded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from delegate_agent import redaction
from delegate_agent.json_types import JsonObject

CLASS_PERSISTENT = "persistent"
CLASS_TRANSIENT = "transient"
CLASS_UNKNOWN = "unknown"
PROVIDER_ERROR_CLASSES = (CLASS_PERSISTENT, CLASS_TRANSIENT, CLASS_UNKNOWN)

# `lane`: every launch on the same engine + model + account fails the same way
# until something changes. `request`: this prompt or session is the problem; the
# lane itself is healthy, so it never earns a known-bad marker.
SCOPE_LANE = "lane"
SCOPE_REQUEST = "request"

UNCLASSIFIED = "unclassified"

MESSAGE_LIMIT = 400
CODE_LIMIT = 96

# `redaction.redact_string` masks credentials, not addresses, and a provider's own
# words often name the account ("the token for alice@example.com was rejected").
# Every field is length-capped so a hostile run of address characters cannot make
# the scan quadratic.
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
EMAIL_MASK = "[email]"


def scrub(text: str) -> str:
    """Provider text made safe to store and show: credentials redacted, addresses masked."""
    return _EMAIL_ADDRESS.sub(EMAIL_MASK, redaction.redact_string(text))


THREAD_LOSS_PATTERNS = (
    re.compile(r"\bno thread with id\b", re.IGNORECASE),
    re.compile(r"\bno thread (?:found )?with id\b", re.IGNORECASE),
    re.compile(r"\bthread\b[^\n]{0,80}\b(?:not found|does not exist|unknown)\b", re.IGNORECASE),
    re.compile(
        r"\bstate (?:data)?base\b[^\n]{0,80}\bthread\b[^\n]{0,40}\b(?:lookup|find|load)[^\n]{0,20}\b(?:fail|error)",
        re.IGNORECASE,
    ),
    re.compile(r"\bthread lookup\b[^\n]{0,40}\b(?:fail|error)", re.IGNORECASE),
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
# problem, so it only classifies as a usage limit when account-context wording appears
# on the same line and within a tight window of the trigger, in either order. "HTTP 429
# Too Many Requests, backing off" next to an unrelated "memory usage: 82%" (or a wrapped
# command's `usage:` help line) must stay transient throttling: pairing them sent
# orchestrators to credential rotation for a retryable 429.
_RATE_LIMIT_TRIGGER = (
    r"\brate limit(?:s|ed)?\b"
    r"|\b(?:HTTP|status|code)[ :=]*429\b"
    r"|\b429\b[^\n]{0,40}\btoo many requests\b"
)
_ACCOUNT_CONTEXT_WORD = r"\b(?:quota|usage|billing|subscription|account|credit)\b"
_CONTEXT_WINDOW = r"[^\n]{0,80}"
_RATE_LIMIT_ACCOUNT_PATTERN = re.compile(
    rf"(?:{_RATE_LIMIT_TRIGGER}){_CONTEXT_WINDOW}(?:{_ACCOUNT_CONTEXT_WORD})"
    rf"|(?:{_ACCOUNT_CONTEXT_WORD}){_CONTEXT_WINDOW}(?:{_RATE_LIMIT_TRIGGER})",
    re.IGNORECASE,
)
USAGE_LIMIT_PATTERNS = (*_USAGE_PATTERNS, _RATE_LIMIT_ACCOUNT_PATTERN)
_THROTTLE_PATTERNS = (
    re.compile(_RATE_LIMIT_TRIGGER, re.IGNORECASE),
    re.compile(r"\btoo many requests\b", re.IGNORECASE),
)


def matches_usage_limit(text: str) -> bool:
    return any(pattern.search(text) for pattern in USAGE_LIMIT_PATTERNS)


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


@dataclass(eq=False)
class Signature:
    """One row of the table.

    Matching, in order: a row whose ``codes`` name the structured provider code;
    a row whose ``statuses`` name the status (a row with ``patterns`` and no
    ``status_alone`` also needs a pattern hit, so one status can carry several
    causes); a row whose ``patterns`` hit the terminal text when no status row
    decided. ``engines`` empty means every engine.
    """

    id: str
    klass: str
    scope: str
    # The detailed failureReason the rest of Delegate branches on. Reasons the
    # closed failureKind enum already knows (auth_failed, usage_limit,
    # provider_error, ...) keep their kind; the enum itself is not widened.
    reason: str
    summary: str
    hint: str
    engines: tuple[str, ...] = ()
    statuses: tuple[int, ...] = ()
    status_alone: bool = False
    codes: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    hints_by_engine: Mapping[str, str] = field(default_factory=dict)
    # A transient signature whose immediate continuation is worth one automatic
    # try (a dropped stream), as opposed to throttling or capacity, where an
    # instant retry only repeats the refusal.
    auto_resume: bool = False
    # False keeps `child_failures.classify` returning None for this row: a bare
    # throttle is not an account problem and must not steer credential rotation.
    failure_code: bool = True

    def hint_for(self, engine: str | None, provider: str | None = None) -> str:
        text = self.hints_by_engine.get(engine or "", self.hint)
        return text.replace("{engine}", engine or "harness").replace(
            "{provider}", provider or "that provider"
        )


_BROKER_HINT = (
    "The estate broker refused this launch (HTTP 403); no vendor process ran. Launch from a "
    "cell whose identity is bound and mapped, or ask the estate admin to bind this one."
)
_MODEL_HINT = (
    "This account cannot use the requested model on this lane; pick another alias "
    "(`delegate models`) or fix the account's access."
)
_REQUEST_ONLY = " The lane itself is healthy; other prompts are unaffected."

SIGNATURES: tuple[Signature, ...] = (
    Signature(
        id="broker_binding_inactive",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="binding_not_active",
        summary="The estate broker says this cell's identity binding is inactive.",
        hint=(
            "A single refusal while sibling launches succeed is usually launch-slot "
            "contention: relaunch once (add --force-launch if the lane is now marked "
            "known-bad). A repeat means the binding is inactive: rebind the cell or "
            "launch from a healthy one."
        ),
        statuses=(403,),
        patterns=(_BINDING_NOT_ACTIVE_PATTERN,),
    ),
    Signature(
        id="broker_uid_unmapped",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="broker_rejected",
        summary="The estate broker has no principal mapped to this Unix user (uid_unmapped).",
        hint=_BROKER_HINT,
        statuses=(403,),
        patterns=_rx(r"\buid_unmapped\b"),
    ),
    Signature(
        id="broker_principal_not_cell",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="broker_rejected",
        summary="The estate broker says the caller is not this cell's principal.",
        hint=_BROKER_HINT,
        statuses=(403,),
        patterns=_rx(r"\bprincipal_not_cell_principal\b"),
    ),
    Signature(
        id="harness_config_rejected",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="harness_config_rejected",
        summary="The harness rejected its own configuration before running.",
        hint=(
            "Fix the harness configuration (an unsupported `-c` override or config.toml key) "
            "or upgrade the harness, then relaunch."
        ),
        patterns=_rx(
            r"\bError loading configuration\b",
            r"\binvalid type: [^\n]{0,80}\bexpected struct \w+",
        ),
    ),
    Signature(
        id="request_image_limit",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_REQUEST,
        reason="provider_error",
        summary="The request carries too many or oversized images for the model.",
        hint="Attach fewer or smaller images." + _REQUEST_ONLY,
        statuses=(400, 413, 422),
        patterns=_rx(
            r"\btoo many images\b",
            r"\bimages?\b[^\n]{0,60}\b(?:exceed\w*|limit|too large|too many|maximum)\b",
            r"\b(?:maximum|max)\b[^\n]{0,30}\bimages?\b",
        ),
    ),
    Signature(
        id="content_flagged",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_REQUEST,
        reason="provider_refusal",
        summary="The provider flagged this request as a possible cybersecurity risk.",
        hint="Rephrase the task or use another lane." + _REQUEST_ONLY,
        patterns=_rx(r"\bflagged for (?:possible|potential) cyber ?security risk\b"),
    ),
    Signature(
        id="model_unavailable",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The account cannot use the requested model.",
        hint=_MODEL_HINT,
        statuses=(404,),
        status_alone=True,
        codes=("model_not_found", "model_not_supported", "unsupported_model"),
        patterns=_rx(
            r"\bcannot use this model\b",
            r"\bdoes not have access to (?:the )?model\b",
            r"\bmodel\b[^\n]{0,40}\b(?:not found|not available|is not supported)\b",
        ),
    ),
    Signature(
        id="age_confirmation_required",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The provider requires an age confirmation on this account.",
        hint="Complete the provider's age confirmation for this account, then relaunch.",
        patterns=_rx(r"\b18\+ age confirmation required\b", r"\bage confirmation required\b"),
    ),
    Signature(
        id="thread_lost",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_REQUEST,
        reason="codex_thread_lost",
        summary="Codex session state could not find the requested thread.",
        hint=(
            "Relaunch without the saved thread; it is gone or lives under a different "
            "CODEX_HOME." + _REQUEST_ONLY
        ),
        patterns=THREAD_LOSS_PATTERNS,
    ),
    Signature(
        id="auth_token_rejected",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="Child harness authentication failed because its token expired or was rejected.",
        hint="Re-authenticate the {engine} CLI, then relaunch.",
        patterns=_AUTH_PATTERNS,
    ),
    Signature(
        id="usage_limit",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="usage_limit",
        summary="Child harness usage or quota limit reached.",
        hint="Wait for the limit to reset, or use another account or lane.",
        statuses=(429,),
        codes=(
            "insufficient_quota",
            "billing_hard_limit_reached",
            "resource_exhausted",
            "quota_exceeded",
        ),
        patterns=USAGE_LIMIT_PATTERNS,
    ),
    Signature(
        id="payment_required",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="usage_limit",
        summary="The account has insufficient funds or credit for this provider.",
        hint="Add credit to the {engine} account, then relaunch.",
        hints_by_engine={"kimi": "Top up the Kimi credits, then relaunch."},
        statuses=(402,),
        status_alone=True,
        patterns=_rx(
            r"\binsufficient (?:account )?(?:funds|balance|credits?)\b",
            r"\bpayment required\b",
            r"\bcredit balance is too low\b",
        ),
    ),
    Signature(
        id="usage_balance_exhausted",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="usage_limit",
        summary="The provider's usage balance for this account is exhausted.",
        hint="Top up the {engine} usage balance or wait for it to reset, then relaunch.",
        patterns=_rx(
            r"\busage balance (?:is )?exhausted\b",
            r"\bbalance (?:is )?exhausted\b",
            r"\bout of (?:credits|usage)\b",
        ),
    ),
    Signature(
        id="cursor_auth_required",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="Cursor is not signed in.",
        hint="Run `estate-cursor login`, then relaunch.",
        engines=("cursor",),
        patterns=_rx(
            r"\bAuthentication required\b[^\n]{0,80}\blogin\b",
            r"\bplease run agent login\b",
        ),
    ),
    Signature(
        id="api_key_missing",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="The provider behind this alias has no API key.",
        hint="The {engine} provider {provider} has no API key: set it or pick another alias.",
        engines=("omp", "pi", "opencode"),
        patterns=_rx(
            r"\bNo API key found for (?P<provider>[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,62}[A-Za-z0-9])?)"
        ),
    ),
    Signature(
        id="claude_login_required",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="Claude is not signed in or its login expired.",
        hint="Sign the Claude CLI in again (`/login`), then relaunch.",
        engines=("claude",),
        patterns=_rx(
            r"\bInvalid API key\b[^\n]{0,40}\blogin\b",
            r"\bplease run /login\b",
            r"\bOAuth token has expired\b",
        ),
    ),
    Signature(
        id="auth_rejected",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="The provider rejected this lane's credentials (HTTP 401).",
        hint="Re-authenticate the {engine} CLI, then relaunch.",
        hints_by_engine={"cursor": "Run `estate-cursor login`, then relaunch."},
        statuses=(401,),
        status_alone=True,
        codes=(
            "invalid_api_key",
            "authentication_error",
            "invalid_authentication",
            "token_expired",
            "unauthenticated",
        ),
    ),
    Signature(
        id="forbidden",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_LANE,
        reason="auth_failed",
        summary="The provider refused this lane's credentials (HTTP 403).",
        hint=(
            "Check that the {engine} account may use this model and region, then relaunch; "
            "re-authenticate if the login changed."
        ),
        statuses=(403,),
        status_alone=True,
        codes=("permission_denied", "permission_error"),
    ),
    Signature(
        id="request_too_large",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_REQUEST,
        reason="provider_error",
        summary="The request exceeds the provider's size or context limit.",
        hint="Shorten the prompt or attachments." + _REQUEST_ONLY,
        statuses=(413,),
        status_alone=True,
        codes=("context_length_exceeded", "string_above_max_length"),
    ),
    Signature(
        id="request_rejected",
        klass=CLASS_PERSISTENT,
        scope=SCOPE_REQUEST,
        reason="provider_error",
        summary="The provider rejected this request as malformed or invalid.",
        hint="Fix the prompt or options named in the message." + _REQUEST_ONLY,
        statuses=(400, 422),
        status_alone=True,
        codes=("invalid_request_error", "invalid_request", "invalid_prompt"),
    ),
    Signature(
        id="rate_limited",
        klass=CLASS_TRANSIENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The provider is throttling requests.",
        hint="Wait a moment and relaunch; this clears on its own.",
        statuses=(429,),
        status_alone=True,
        codes=("rate_limit_exceeded", "rate_limit_error", "too_many_requests"),
        patterns=_THROTTLE_PATTERNS,
        failure_code=False,
    ),
    Signature(
        id="model_at_capacity",
        klass=CLASS_TRANSIENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The provider says the model is at capacity.",
        hint="Retry later or use another lane; this clears on its own.",
        statuses=(503, 529),
        patterns=_rx(
            r"\bat capacity\b",
            r"\bcapacity\b[^\n]{0,40}\b(?:exceeded|reached|exhausted)\b",
        ),
    ),
    Signature(
        id="provider_unavailable",
        klass=CLASS_TRANSIENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The provider had a server-side failure.",
        hint=(
            "Retry shortly; this clears on its own. Codex and Claude work runs with a saved "
            "session already got one automatic resume (providerErrors.autoResume); after that, "
            "`delegate followup` the run."
        ),
        statuses=(500, 502, 503, 504, 529),
        status_alone=True,
        codes=("overloaded_error", "server_error", "service_unavailable", "api_error"),
        auto_resume=True,
    ),
    Signature(
        id="stream_disconnected",
        klass=CLASS_TRANSIENT,
        scope=SCOPE_LANE,
        reason="provider_error",
        summary="The provider stream dropped before the response completed.",
        hint=(
            "Relaunch; this clears on its own. Codex and Claude work runs with a saved "
            "session already got one automatic resume (providerErrors.autoResume); after that, "
            "`delegate followup` the run."
        ),
        patterns=_rx(
            r"\bwebsocket closed\b",
            r"\bstream disconnected\b",
            r"\bstream closed before\b",
            r"\bstream ended unexpectedly\b",
            r"\bconnection (?:reset by peer|reset|dropped)\b",
            r"\bconnection closed before\b",
            r"\bECONNRESET\b",
            r"\bsocket hang up\b",
        ),
        auto_resume=True,
    ),
)

SIGNATURES_BY_ID: dict[str, Signature] = {signature.id: signature for signature in SIGNATURES}
if len(SIGNATURES_BY_ID) != len(SIGNATURES):  # pragma: no cover - table integrity
    raise RuntimeError("provider_errors.SIGNATURES has a duplicate id")


@dataclass(frozen=True)
class Classification:
    signature: Signature
    # The line of text that decided the match, for the message when the caller
    # had only free text (a stderr tail) and no structured error message.
    line: str
    provider: str | None = None


def _normalize_code(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or None


def coerce_status(value: object) -> int | None:
    """An HTTP-ish error status (400-599) from an int or numeric string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit() and len(value.strip()) == 3:
        number = int(value.strip())
    else:
        return None
    return number if 400 <= number <= 599 else None


# A status stated in prose: "HTTP 403", "status code 429", or a bare leading
# "402 Insufficient account funds". Narrow on purpose (the bare form only counts at
# the very start of the text); it only ever runs over trusted provider error text.
_TEXT_STATUS_PATTERN = re.compile(
    r"\b(?:HTTP|API Error|status(?: code)?)[\s:=]+([45]\d{2})\b|\A\s*([45]\d{2})(?=[\s:.-])",
    re.IGNORECASE,
)


def status_from_text(text: str) -> int | None:
    match = _TEXT_STATUS_PATTERN.search(text)
    if match is None:
        return None
    return coerce_status(match.group(1) or match.group(2))


def _line_around(text: str, start: int) -> str:
    begin = text.rfind("\n", 0, start) + 1
    end = text.find("\n", start)
    return text[begin : end if end != -1 else len(text)].strip()


def _first_pattern_hit(
    patterns: tuple[re.Pattern[str], ...], text: str
) -> tuple[re.Match[str], str] | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found is not None:
            return found, _line_around(text, found.start())
    return None


def _engine_ok(signature: Signature, engine: str | None) -> bool:
    return not signature.engines or engine is None or engine in signature.engines


def _classification(signature: Signature, hit: tuple[re.Match[str], str] | None) -> Classification:
    if hit is None:
        return Classification(signature, "")
    found, line = hit
    provider = found.groupdict().get("provider")
    return Classification(signature, line, provider)


def match(
    text: str,
    *,
    engine: str | None = None,
    status: int | None = None,
    code: str | None = None,
) -> Classification | None:
    """The signature for a provider error, status-first; None when nothing named it."""
    text = text or ""
    rows = [signature for signature in SIGNATURES if _engine_ok(signature, engine)]
    normalized_code = _normalize_code(code)
    if normalized_code is not None:
        for row in rows:
            if normalized_code in row.codes:
                return _classification(row, _first_pattern_hit(row.patterns, text))
    if status is not None:
        for row in rows:
            if status in row.statuses and row.patterns and not row.status_alone:
                hit = _first_pattern_hit(row.patterns, text)
                if hit is not None:
                    return _classification(row, hit)
        for row in rows:
            if status in row.statuses and (row.status_alone or not row.patterns):
                return _classification(row, _first_pattern_hit(row.patterns, text))
    for row in rows:
        if not row.patterns or (row.statuses and status is not None):
            continue
        hit = _first_pattern_hit(row.patterns, text)
        if hit is not None:
            return _classification(row, hit)
    return None


def matches_reason(text: str, reason: str) -> bool:
    """True when any row carrying ``reason`` recognizes the text, in any table order."""
    return any(
        pattern.search(text)
        for signature in SIGNATURES
        if signature.reason == reason
        for pattern in signature.patterns
    )


def _bound(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def raw_error(
    *,
    message: str | None,
    status: int | None = None,
    code: str | None = None,
    source: str | None = None,
) -> JsonObject | None:
    """The provider's own error as captured from one event: redacted and bounded."""
    text = _bound(scrub(message), MESSAGE_LIMIT) if message else ""
    clean_code = _bound(scrub(code), CODE_LIMIT) if isinstance(code, str) and code.strip() else None
    if not text and status is None and clean_code is None:
        return None
    record: JsonObject = {
        "status": status,
        "providerCode": clean_code,
        "message": text,
    }
    if source:
        record["source"] = _bound(source, 64)
    return record


_STATUS_KEYS = (
    "status",
    "statusCode",
    "status_code",
    "httpStatus",
    "http_status",
    "errorStatus",
    "api_error_status",
)
_CODE_KEYS = ("code", "errorCode", "error_code")


def _scopes(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    scopes: list[Mapping[str, object]] = [payload]
    error = payload.get("error")
    if isinstance(error, dict):
        scopes.append(error)
        for nested in ("data", "response"):
            inner = error.get(nested)
            if isinstance(inner, dict):
                scopes.append(inner)
    return scopes


def status_from_payload(payload: Mapping[str, object]) -> int | None:
    for scope in _scopes(payload):
        for key in _STATUS_KEYS:
            status = coerce_status(scope.get(key))
            if status is not None:
                return status
        # A numeric `code` is how some providers spell the HTTP status.
        for key in _CODE_KEYS:
            status = coerce_status(scope.get(key)) if not isinstance(scope.get(key), str) else None
            if status is not None:
                return status
    return None


def code_from_payload(payload: Mapping[str, object]) -> str | None:
    for scope in _scopes(payload):
        keys = (*_CODE_KEYS, "type") if scope is not payload else _CODE_KEYS
        for key in keys:
            value = scope.get(key)
            if isinstance(value, str) and value.strip() and not value.strip().isdigit():
                return value.strip()
    return None


def provider_error_record(
    *,
    engine: str | None,
    raw: JsonObject | None,
    fallback_text: str = "",
) -> JsonObject | None:
    """The `providerError` object for a failed run, or None when the run said nothing.

    ``raw`` is the last unrecovered error the stream accumulator captured (the
    provider's terminal word); ``fallback_text`` is the trusted stderr and event
    text, consulted only when the terminal error alone named no signature.
    """
    message = raw.get("message") if isinstance(raw, dict) else None
    text = message if isinstance(message, str) else ""
    status = coerce_status(raw.get("status")) if isinstance(raw, dict) else None
    code = raw.get("providerCode") if isinstance(raw, dict) else None
    code = code if isinstance(code, str) else None
    if status is None and text:
        status = status_from_text(text)
    hit = match(text, engine=engine, status=status, code=code) if (text or status or code) else None
    if hit is None and fallback_text.strip():
        fallback_status = status if status is not None else status_from_text(fallback_text)
        hit = match(fallback_text, engine=engine, status=fallback_status, code=code)
        if hit is not None and status is None:
            status = fallback_status
    if hit is None and raw is None:
        return None
    if not text and hit is not None and hit.line:
        text = _bound(scrub(hit.line), MESSAGE_LIMIT)
    record: JsonObject = {
        "status": status,
        "providerCode": code,
        "message": text,
        "engine": engine,
    }
    if hit is None:
        record.update(
            signature=UNCLASSIFIED,
            **{"class": CLASS_UNKNOWN},
            scope=SCOPE_REQUEST,
            hint="No known signature matched; the message is the provider's own text.",
        )
        return record
    signature = hit.signature
    record.update(
        signature=signature.id,
        **{"class": signature.klass},
        scope=signature.scope,
        hint=signature.hint_for(engine, hit.provider),
    )
    return record


def signature_for_record(record: Mapping[str, object] | None) -> Signature | None:
    if not isinstance(record, Mapping):
        return None
    signature_id = record.get("signature")
    return SIGNATURES_BY_ID.get(signature_id) if isinstance(signature_id, str) else None


def reason_for_record(record: Mapping[str, object] | None) -> str | None:
    signature = signature_for_record(record)
    return signature.reason if signature is not None and signature.failure_code else None


def signature_catalog() -> list[JsonObject]:
    """The table as plain data, for docs and diagnostics."""
    return [
        {
            "id": signature.id,
            "class": signature.klass,
            "scope": signature.scope,
            "reason": signature.reason,
            "engines": list(signature.engines),
            "statuses": list(signature.statuses),
            "hint": signature.hint,
            "autoResume": signature.auto_resume,
        }
        for signature in SIGNATURES
    ]
