"""Stop a workflow stage from launching into a lane it has already learned is bad.

A stage is one ``phase()`` label; a lane is one engine on one model. When the
first ``stop_after`` results of a stage on a lane all failed with the same
persistent, lane-scoped provider signature (say ``auth_rejected``), the rest of
the stage's cells would fail the same way, slowly and at cost. The guard trips
and the stage stops launching further cells on that lane; each skipped call is
journaled with why and returns the typed ``provider_exhausted`` outcome.

Only the first ``stop_after`` results decide. A success, or any other kind of
result, among them means the lane is not uniformly bad and the guard never
trips for that stage and lane. Transient and request-scoped failures never
count, mirroring the known-bad lane marker.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from delegate_agent import lane_health, provider_errors
from delegate_agent.json_types import JsonObject

DEFAULT_STAGE = "(no phase)"


def lane_label(engine: str, model: str | None) -> str:
    return f"{engine}:{model}" if model else engine


def persistent_signature(provider_error: JsonObject | None) -> str | None:
    """The signature id when this provider error would earn a lane marker, else None."""
    if lane_health.earns_marker(provider_error):
        signature = provider_errors.signature_for_record(provider_error)
        return signature.id if signature is not None else None
    return None


@dataclass
class _Tally:
    results: list[str | None] = field(default_factory=list)
    tripped: JsonObject | None = None
    decided: bool = False


class StageLaneGuard:
    """Per-workflow tally of each stage's first results on each lane. Thread-safe."""

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self._lock = threading.Lock()
        self._tallies: dict[tuple[str, str], _Tally] = {}

    @property
    def enabled(self) -> bool:
        return self.stop_after > 0

    def tripped(self, stage: str | None, lane: str) -> JsonObject | None:
        """The trip record when the stage has stopped launching on this lane."""
        if not self.enabled:
            return None
        with self._lock:
            tally = self._tallies.get((stage or DEFAULT_STAGE, lane))
            return dict(tally.tripped) if tally is not None and tally.tripped else None

    def record(self, stage: str | None, lane: str, *, signature: str | None) -> JsonObject | None:
        """Note one finished call; return the trip record if this result tripped the guard.

        ``signature`` is the persistent lane-scoped signature the call failed with, or
        None for a success or any other kind of result.
        """
        if not self.enabled:
            return None
        stage_name = stage or DEFAULT_STAGE
        with self._lock:
            tally = self._tallies.setdefault((stage_name, lane), _Tally())
            if tally.decided:
                return None
            tally.results.append(signature)
            if signature is None or tally.results[0] != signature:
                tally.decided = True
                return None
            if len(tally.results) < self.stop_after:
                return None
            tally.decided = True
            tally.tripped = {
                "stage": stage_name,
                "lane": lane,
                "signature": signature,
                "count": len(tally.results),
            }
            return dict(tally.tripped)
