"""The normalized usage record: what a producer emits and what a readback trusts.

The runner persists a normalized usage object in a run's `state.json`, but that
file lives in the run registry a non-isolated child shares, so a stored record
is not proof of the runner's own normalization. Every readback surface
(run-output, `runs`/`ps` summaries, snapshot) therefore reconstructs the record
through this one allowlist instead of copying whatever the record holds:

- the basis must be one a producer in this package emits: `reported`
  (`harness_events._normalize_reported_usage`), `exact` (`runner._claude_usage`,
  preserved by `runner._aggregate_usage`), or `unavailable` (the runner's
  default and `_aggregate_usage`'s degradation);
- counters are taken only when they are non-negative integers, so a bool,
  string, float, or negative value is dropped rather than echoed;
- `costUsd` is taken only when it is a finite non-negative number.

Everything else -- unknown keys, alternate spellings, free text, newlines, a
credential-shaped value -- is discarded, and what survives is routed through the
shared redaction contract so a future field cannot be the one value that
bypasses it. A record whose basis is not in this vocabulary is not a normalized
record at all: readback surfaces omit usage instead of inventing one, because
the record itself is not evidence that any harness reported numbers.
"""

from __future__ import annotations

import math

from delegate_agent.json_types import JsonObject, is_non_negative_int
from delegate_agent.redaction import redact_value

# The counters a normalized record carries. `harness_events._normalize_reported_usage`
# emits exactly these names (mapping each harness's own spelling onto them), and
# `runner._aggregate_usage` sums them.
USAGE_COUNTER_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
)

# The bases a persisted run's usage record can carry; see the module docstring
# for the producer of each one.
PERSISTED_USAGE_BASES = ("reported", "exact", "unavailable")


def normalized_usage(value: object) -> JsonObject | None:
    """The allowlisted normalized usage a persisted record may contribute."""
    if not isinstance(value, dict):
        return None
    basis = value.get("basis")
    if basis not in PERSISTED_USAGE_BASES:
        return None
    usage: JsonObject = {"basis": basis}
    for key in USAGE_COUNTER_KEYS:
        counter = value.get(key)
        if is_non_negative_int(counter):
            usage[key] = counter
    cost = value.get("costUsd")
    if _is_non_negative_number(cost) and (isinstance(cost, int) or math.isfinite(cost)):
        usage["costUsd"] = cost
    redacted = redact_value(usage)
    return redacted if isinstance(redacted, dict) else usage


def _is_non_negative_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
