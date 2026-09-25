from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import command_errors, redaction, run_registry, run_status, snapshot_view
from delegate_agent import rendering as delegate_rendering
from delegate_agent.errors import DelegateError
from delegate_agent.git_utils import GIT_QUICK_TIMEOUT_SECONDS, run_git
from delegate_agent.json_types import JsonObject

# Upper bound on linked-worktree registries folded into a main-worktree listing.
LINKED_REGISTRY_LIMIT = 256

STRUCTURAL_RUN_KEYS = (
    "runId",
    "alias",
    "harness",
    "group",
    "mode",
    "modelAlias",
    "modelResolved",
    "rawStatus",
    "effectiveStatus",
    "status",
    "terminalStatus",
    "activityAt",
    "initiatorRoot",
    "registryWorkspace",
)


def linked_registry_roots(workspace_path: str) -> list[tuple[str, Path]]:
    """Registries of this repository's linked worktrees, seen from the main one.

    A run launched from inside a linked worktree (a Delegate lane's own
    worktree, typically) is registered in that worktree's ``.delegate``. The
    main worktree finds those registries read-only through ``git worktree
    list`` so ``delegate runs`` there lists them; nothing is written across
    worktrees. Returns [] when the workspace is not a repository's main
    worktree or Git cannot answer.
    """
    try:
        result = run_git(
            workspace_path,
            ["worktree", "list", "--porcelain"],
            timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
        )
    except OSError:  # no git binary: nothing to fold in.
        return []
    if result.returncode != 0:
        return []
    paths = [
        line[len("worktree ") :]
        for line in result.stdout.split("\n")
        if line.startswith("worktree ")
    ]
    if not paths:
        return []
    try:
        if Path(paths[0]).resolve() != Path(workspace_path).resolve():
            return []
    except (OSError, RuntimeError):
        return []
    # A linked worktree whose `.delegate` is a symlink (or a copy) of the main
    # registry resolves to the same root: folding it in would list every run
    # twice, once per worktree path.
    main_root = run_registry.registry_root_if_exists(Path(workspace_path))
    try:
        main_resolved = main_root.resolve() if main_root is not None else None
    except (OSError, RuntimeError):
        main_resolved = None
    roots: list[tuple[str, Path]] = []
    seen_roots: set[Path] = set()
    for path in paths[1:]:
        root = run_registry.registry_root_if_exists(Path(path))
        if root is None:
            continue
        try:
            resolved = root.resolve()
        except (OSError, RuntimeError):
            resolved = root
        if resolved in seen_roots or (main_resolved is not None and resolved == main_resolved):
            continue
        seen_roots.add(resolved)
        roots.append((path, root))
        if len(roots) >= LINKED_REGISTRY_LIMIT:
            break
    return roots


@dataclass(frozen=True)
class SnapshotCommand:
    handle: str | None
    latest_harness: str | None = None
    no_redact: bool = False
    json_mode: bool = False


@dataclass(frozen=True)
class RunsCommand:
    action: str | None = None
    active: bool = False
    running: bool = False
    stale: bool = False
    harness: str | None = None
    group: str | None = None
    limit: int | None = None
    older_than_days: int | None = None
    dry_run: bool = False
    structural: bool = False
    summary: bool = False
    json_mode: bool = False


class InspectionError(command_errors.CommandError):
    pass


def emit_snapshot(command: SnapshotCommand, *, workspace_path: str, stdout: TextIO) -> int:
    workspace = Path(workspace_path)
    registry_root = run_registry.registry_root_if_exists(workspace)
    if registry_root is None:
        if command.latest_harness is None and command.handle is None:
            raise InspectionError("missing_handle", "snapshot requires a run handle or --latest.")
        registry_root = run_registry.registry_root(workspace)
    target = run_registry.resolve_run_target(
        registry_root,
        handle=command.handle,
        latest_harness=command.latest_harness,
    )
    if isinstance(target, run_registry.RunTargetLookupError):
        raise InspectionError(target.error, target.message)
    run_id = target.run_id
    snapshot = run_registry.load_run_snapshot(registry_root, run_id)
    if snapshot is None:
        view = snapshot_view.merge_snapshot_view(
            registry_root,
            run_id,
            None,
            redact=not command.no_redact,
        )
    else:
        view = redaction.redact_value(snapshot) if not command.no_redact else dict(snapshot)
    run_registry.add_run_target_resolution(view, target)
    if command.json_mode:
        delegate_rendering.print_json(snapshot_view.snapshot_json_payload(view), stdout)
    else:
        delegate_rendering.render_snapshot_text(view, stdout)
    return 0


def emit_runs(command: RunsCommand, *, workspace_path: str, stdout: TextIO) -> int:
    registry_root = run_registry.registry_root_if_exists(Path(workspace_path))
    if command.action == "prune":
        older_than_days = (
            command.older_than_days
            if command.older_than_days is not None
            else run_registry.DEFAULT_RUN_PRUNE_DAYS
        )
        payload = (
            run_registry.prune_runs(
                registry_root,
                older_than_days=older_than_days,
                dry_run=command.dry_run,
            )
            if registry_root is not None
            else run_registry.empty_run_prune_payload(
                older_than_days=older_than_days,
                dry_run=command.dry_run,
            )
        )
        if command.json_mode:
            delegate_rendering.print_json(payload, stdout)
        else:
            delegate_rendering.render_runs_prune_text(payload, stdout)
        if payload.get("ok") is False:
            exit_code = payload.get("exitCode")
            return exit_code if isinstance(exit_code, int) else 1
        return 0
    if command.action is not None:
        raise InspectionError("unknown_runs_action", f"Unknown runs action: {command.action}")
    limit = command.limit or run_registry.DEFAULT_RUNS_LIMIT
    if command.running:
        mode = "running"
        status_filter = run_registry.STATUS_FILTER_RUNNING
    elif command.stale:
        mode = "stale"
        status_filter = run_registry.STATUS_FILTER_STALE
    elif command.active:
        mode = "active"
        status_filter = None
    else:
        mode = "recent"
        status_filter = None
    sources: list[tuple[str | None, Path]] = [(None, registry_root)] if registry_root else []
    sources.extend(linked_registry_roots(workspace_path))
    if command.summary:
        return _emit_runs_summary(
            command, sources, mode=mode, status_filter=status_filter, stdout=stdout
        )
    summaries: list[JsonObject] = []
    seen_run_ids: set[str] = set()
    total = 0
    scope_total = 0
    for linked_workspace, root in sources:
        try:
            index = run_registry.load_index(root)
            found, found_total, found_scope = run_registry.list_run_summaries(
                root,
                index,
                active=command.active,
                status_filter=status_filter,
                harness=command.harness,
                group=command.group,
                limit=limit,
            )
        except (OSError, ValueError, DelegateError):
            # One unreadable linked registry must not hide the main listing.
            if linked_workspace is None:
                raise
            continue
        # Two worktrees can name one registry (a linked `.delegate` symlink or
        # copy, a path Git reports twice), and one run is one row regardless of
        # how many worktree paths reach it. The dropped rows are subtracted
        # from this root's totals too: `found_total`/`found_scope` count the
        # copy's rows, so an uncorrected "N of M" footer counts every one of
        # them twice.
        found_kept = [
            summary
            for summary in found
            if not (isinstance(summary.get("runId"), str) and summary["runId"] in seen_run_ids)
        ]
        duplicates = len(found) - len(found_kept)
        found = found_kept
        for summary in found:
            if isinstance(summary.get("runId"), str):
                seen_run_ids.add(summary["runId"])
        if linked_workspace is not None:
            for summary in found:
                summary["registryWorkspace"] = linked_workspace
        summaries.extend(found)
        total += max(0, found_total - duplicates)
        scope_total += max(0, found_scope - duplicates)
    if len(sources) > 1:
        summaries.sort(key=lambda summary: str(summary.get("activityAt") or ""), reverse=True)
        summaries = summaries[:limit]
    if command.structural:
        summaries = [
            redaction.redact_value(
                {key: summary[key] for key in STRUCTURAL_RUN_KEYS if key in summary}
            )
            for summary in summaries
        ]
    else:
        summaries = [redaction.redact_value(summary) for summary in summaries]
    warnings: list[str] = []
    if not summaries:
        # An empty table reads as "every lane died" unless the listing says why.
        # The workspace-scope hint is not a property of --group/--harness: a bare
        # status-filtered listing against an empty Registry needs it just as much.
        status_filter_present = command.running or command.stale or command.active
        if status_filter_present and scope_total > 0:
            if command.running:
                warnings.append("No running runs matched. Drop --running to include terminal runs.")
            elif command.stale:
                warnings.append("No stale runs matched. Drop --stale to include terminal runs.")
            else:
                warnings.append("No active runs matched. Drop --active to include terminal runs.")
        elif scope_total == 0:
            warnings.append(
                "No matching runs in this workspace Registry. "
                "The run Registry is workspace-scoped; use --cwd PATH to target another "
                "workspace's Registry."
            )
    if command.json_mode:
        delegate_rendering.print_json(
            delegate_rendering.runs_json_payload(
                summaries,
                limit=limit,
                mode=mode,
                total=total,
                warnings=warnings or None,
            ),
            stdout,
        )
    else:
        delegate_rendering.render_runs_text(
            summaries,
            stdout,
            mode=mode,
            total=total,
            warnings=warnings or None,
        )
    return 0


RUNS_SUMMARY_SCHEMA = "delegate.runs.summary.v1"


def _emit_runs_summary(
    command: RunsCommand,
    sources: list[tuple[str | None, Path]],
    *,
    mode: str,
    status_filter: str | None,
    stdout: TextIO,
) -> int:
    """Counts for every matching run, with no rows (``runs --summary``)."""
    counts: dict[str, dict[str, int]] = {"byStatus": {}, "byHarness": {}, "byGroup": {}}
    total = 0
    for linked_workspace, root in sources:
        try:
            found, found_total, _scope = run_status.count_run_summaries(
                root,
                run_registry.load_index(root),
                active=command.active,
                status_filter=status_filter,
                harness=command.harness,
                group=command.group,
            )
        except (OSError, ValueError, DelegateError):
            if linked_workspace is None:
                raise
            continue
        total += found_total
        for bucket, values in found.items():
            for label, count in values.items():
                counts[bucket][label] = counts[bucket].get(label, 0) + count
    ordered = {
        bucket: dict(sorted(values.items(), key=lambda item: (-item[1], item[0])))
        for bucket, values in counts.items()
    }
    if command.json_mode:
        delegate_rendering.print_json(
            {"schema": RUNS_SUMMARY_SCHEMA, "ok": True, "mode": mode, "total": total, **ordered},
            stdout,
        )
        return 0
    print(f"mode: {mode}", file=stdout)
    print(f"total: {total}", file=stdout)
    for bucket, label in (("byStatus", "status"), ("byHarness", "harness"), ("byGroup", "group")):
        values = ordered[bucket]
        rendered = ", ".join(f"{name} {count}" for name, count in values.items()) or "-"
        print(f"{label}: {rendered}", file=stdout)
    return 0
