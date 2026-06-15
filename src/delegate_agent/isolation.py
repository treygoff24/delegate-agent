"""Worktree isolation planning and creation helpers.

This module owns effective-isolation resolution, source Git validation,
persistent worktree creation, and prompt context injection. Worktree removal
lives in worktree_mgmt.py.
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404 - isolation helpers intentionally run fixed git argv with shell=False.
from dataclasses import dataclass
from pathlib import Path

# Re-export isolation constants from config for convenience
from delegate_agent.config import (  # noqa: F401  # re-exported
    ISOLATION_AUTO,
    ISOLATION_NONE,
    ISOLATION_WORKTREE,
    VALID_ISOLATION_VALUES,
    ConfigError,
)
from delegate_agent.git_utils import (
    GIT_QUICK_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RETURN_CODE,
)
from delegate_agent.git_utils import (
    run_git as _run_git,
)
from delegate_agent.json_types import JsonObject
from delegate_agent.prompt_instructions import SKILL_REVIEW_PREFIX


@dataclass(frozen=True)
class IsolationContext:
    """Resolved isolation metadata shared by dry-run output, run metadata, and launch."""

    source_workspace: str
    effective_isolation: str
    isolation_mode: str
    isolation_lifecycle: str
    preserved_workspace: bool
    planned_branch: str | None = None
    planned_execution_cwd: str | None = None
    source_git_root: str | None = None
    source_git_common_dir: str | None = None
    source_head_oid: str | None = None
    source_head_ref: str | None = None
    source_branch: str | None = None
    safe_workspace_method: str | None = None
    warnings: tuple[str, ...] = ()


def compute_repo_fingerprint_from_common_dir(git_common_dir: str) -> str:
    """Return a stable 12-char hash of the resolved Git common directory path."""
    resolved = Path(git_common_dir).resolve(strict=True).as_posix()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _raise_if_git_timed_out(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == GIT_TIMEOUT_RETURN_CODE:
        raise IsolationExecutionError("git_timeout", f"{action} timed out: {result.stderr}")


def short_run_id(run_id: str) -> str:
    """Return a run-id segment safe for branch and path names."""
    s = run_id
    if s.startswith("del_"):
        s = s[4:]
    s = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in s)
    return s[:32]


def branch_label(engine: str, model_alias: str | None) -> str:
    """Return the engine/model segment used in Delegate worktree branch names."""
    if engine == "cursor":
        return "cursor"
    if engine == "codex":
        return "codex"
    if engine == "kimi":
        return "kimi"
    if engine == "droid":
        if not model_alias:
            return "droid"
        slug = model_alias.lower()
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return f"droid-{slug}" if slug else "droid"
    return engine


def plan_branch_name(label: str, short_id: str) -> str:
    return f"delegate/{label}-{short_id}"


def plan_worktree_path(data_home: Path, fingerprint: str, label: str, short_id: str) -> Path:
    return data_home / fingerprint / f"{label}-{short_id}"


def worktrees_data_home(config: JsonObject) -> Path:
    """Return the persistent worktree root, defaulting to ~/.delegate/worktrees."""
    cfg = config.get("worktrees", {})
    if not isinstance(cfg, dict):
        return Path.home() / ".delegate" / "worktrees"
    data_home = cfg.get("dataHome")
    if isinstance(data_home, str) and data_home:
        expanded = Path(data_home).expanduser()
        if not expanded.is_absolute():
            raise ConfigError(
                "invalid_worktrees_config",
                "worktrees.dataHome must be an absolute path or start with ~/.",
            )
        return expanded
    return Path.home() / ".delegate" / "worktrees"


def _map_auto_isolation(engine: str, mode: str) -> str:
    """Preserve safe-mode isolation for local engines while leaving work mode in-place."""
    if mode == "safe" and engine in ("cursor", "codex", "droid", "kimi"):
        return ISOLATION_WORKTREE
    return ISOLATION_NONE


def _isolation_lifecycle(isolation_mode: str, mode: str) -> str:
    if isolation_mode == ISOLATION_NONE:
        return "none"
    if isolation_mode == ISOLATION_WORKTREE:
        if mode == "work":
            return "persistent"
        return "temporary"
    return "none"


def build_isolation_context(
    source_workspace: str,
    resolved_isolation: str,
    *,
    engine: str = "",
    mode: str = "",
    model_alias: str | None = None,
    source_git_root: str | None = None,
    source_git_common_dir: str | None = None,
    source_head_oid: str | None = None,
    source_head_ref: str | None = None,
    source_branch: str | None = None,
    config: JsonObject | None = None,
    run_short_id: str | None = None,
) -> IsolationContext:
    """Resolve isolation into launch metadata and planned persistent-worktree paths."""
    # isolation_mode preserves the raw resolved value (auto/none/worktree).
    # effective_isolation is the mapped behavior (none/worktree, never auto).
    isolation_mode_raw = resolved_isolation
    if resolved_isolation == ISOLATION_AUTO:
        effective = _map_auto_isolation(engine, mode)
    else:
        effective = resolved_isolation

    lifecycle = _isolation_lifecycle(effective, mode)
    preserved = lifecycle == "persistent"

    # Compute named paths only for persistent worktree runs. Temporary safe-mode
    # isolation uses an ephemeral detached worktree or directory copy, not a
    # Delegate-managed branch under the persistent worktree data home.
    planned_branch: str | None = None
    planned_execution_cwd: str | None = None
    if lifecycle == "persistent" and source_git_common_dir is not None:
        try:
            fp = compute_repo_fingerprint_from_common_dir(source_git_common_dir)
        except (FileNotFoundError, OSError):
            fp = None
        if fp is not None:
            short_id = run_short_id if run_short_id else "<short-run-id-placeholder>"
            label = branch_label(engine, model_alias)
            planned_branch = plan_branch_name(label, short_id)
            dh = worktrees_data_home(config or {})
            planned_execution_cwd = str(plan_worktree_path(dh, fp, label, short_id))

    return IsolationContext(
        source_workspace=source_workspace,
        effective_isolation=effective,
        isolation_mode=isolation_mode_raw,
        isolation_lifecycle=lifecycle,
        preserved_workspace=preserved,
        planned_branch=planned_branch,
        planned_execution_cwd=planned_execution_cwd,
        source_git_root=source_git_root,
        source_git_common_dir=source_git_common_dir,
        source_head_oid=source_head_oid,
        source_head_ref=source_head_ref,
        source_branch=source_branch,
    )


class IsolationExecutionError(Exception):
    """Machine-readable isolation failure converted to DelegateError by cli.py."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def require_valid_head(source_git_root: str) -> str:
    """Return HEAD's OID or raise missing_git_head for an unborn repository."""
    result = _run_git(
        source_git_root,
        ["rev-parse", "--verify", "HEAD"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    _raise_if_git_timed_out(result, "git rev-parse HEAD")
    if result.returncode != 0:
        raise IsolationExecutionError(
            "missing_git_head",
            "--isolation worktree requires a Git workspace with at least one commit.",
        )
    return result.stdout.strip()


def require_clean_source(source_git_root: str) -> None:
    """Require no staged, unstaged, untracked, or submodule dirtiness."""
    result = _run_git(
        source_git_root,
        ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    _raise_if_git_timed_out(result, "git status")
    if result.returncode != 0:
        raise IsolationExecutionError(
            "dirty_source_check_failed",
            f"git status failed with code {result.returncode}: {result.stderr.strip()}",
        )
    if result.stdout.strip():
        raise IsolationExecutionError(
            "dirty_source_workspace",
            (
                "--isolation worktree for work mode requires a clean source workspace. "
                "Commit/stash/delete local changes, run in-place with --isolation none, "
                "or wait for a future --include-dirty option."
            ),
        )


def create_persistent_worktree(
    source_git_root: str,
    branch: str,
    worktree_path: str,
    base_oid: str,
) -> None:
    """Create a persistent Git worktree on a new branch from base_oid."""
    branch_probe = _run_git(
        source_git_root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        timeout_seconds=GIT_QUICK_TIMEOUT_SECONDS,
    )
    _raise_if_git_timed_out(branch_probe, "git branch availability check")
    if branch_probe.returncode == 0:
        raise IsolationExecutionError(
            "branch_collision",
            f"Branch '{branch}' already exists. This indicates registry corruption or a clock anomaly.",
        )
    if branch_probe.returncode != 1:
        raise IsolationExecutionError(
            "worktree_create_failed",
            f"Failed to verify branch availability for '{branch}': {branch_probe.stderr.strip()}",
        )
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        source_git_root,
        ["worktree", "add", "-b", branch, worktree_path, base_oid],
    )
    _raise_if_git_timed_out(result, "git worktree add")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise IsolationExecutionError(
            "worktree_create_failed",
            f"Failed to create git worktree: {stderr}",
        )


# Keep this text stable; child prompts and tests rely on the exact wording.
PERSISTENT_WORKTREE_CONTEXT_NOTE = (
    "You are running in a Delegate-created isolated Git worktree. "
    "Make changes in this execution workspace only. "
    "Do not attempt to modify, merge into, or clean the source checkout. "
    "Do not delete, rename, or `git worktree remove` this workspace; "
    "the orchestrator manages worktree lifecycle. "
    "Report changed files, verification, and suggested integration steps. "
    "Your orchestrator can inspect this run via `delegate worktree show <alias>` "
    "and retire it via `delegate worktree remove <alias>` "
    "(refuses on dirty or unmerged), "
    "`delegate worktree remove <alias> --force-branch` "
    "(allow unmerged-branch deletion), "
    "`delegate worktree remove <alias> --discard-uncommitted` "
    "(DISCARDS uncommitted edits), "
    "`delegate worktree remove <alias> --force` "
    "(shorthand for both destructive overrides), "
    "or `delegate worktree prune --merged` for bulk integrated entries."
    "\n\n"
)


def prepend_persistent_worktree_context(prompt: str) -> str:
    """Insert the worktree note after the skill prefix so the user prompt stays last."""
    if prompt.startswith(SKILL_REVIEW_PREFIX):
        insert_at = len(SKILL_REVIEW_PREFIX)
        return prompt[:insert_at] + PERSISTENT_WORKTREE_CONTEXT_NOTE + prompt[insert_at:]
    return PERSISTENT_WORKTREE_CONTEXT_NOTE + prompt
