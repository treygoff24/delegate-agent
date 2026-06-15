from __future__ import annotations

from pathlib import Path

from delegate_agent import run_registry


class CommandError(Exception):
    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def resolve_run_target(
    registry_root: Path,
    *,
    handle: str | None,
    latest_harness: str | None,
    error_cls: type[CommandError],
) -> tuple[str, str | None]:
    target = run_registry.resolve_run_target(
        registry_root,
        handle=handle,
        latest_harness=latest_harness,
    )
    if isinstance(target, run_registry.RunTargetLookupError):
        raise error_cls(target.error, target.message)
    return target.run_id, target.alias
