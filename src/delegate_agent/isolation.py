"""Creation-side helpers for worktree isolation planning and execution.

This module owns isolation constants, IsolationContext, effective-isolation resolution,
Git clean/HEAD checks, creation-context capture, worktree branch/path planning,
persistent worktree creation, and prompt injection.

Wave 3 scope: planning + execution helpers (validation, creation, prompt context).
No removal helpers (those belong in worktree_mgmt.py in Wave 5).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
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
    GIT_MUTATION_TIMEOUT_SECONDS,
    GIT_QUICK_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RETURN_CODE,
    timeout_completed_process,
)
from delegate_agent.json_types import JsonObject


@dataclass(frozen=True)
class IsolationContext:
    """Generalized isolation context replacing the old SafeIsolationContext.

    Fields:
        source_workspace: The original source workspace path.
        effective_isolation: Final resolved isolation value (auto/none/worktree).
        isolation_mode: The isolation mode after auto-mapping.
        isolation_lifecycle: "none" | "temporary" | "persistent".
        preserved_workspace: Whether the isolated workspace persists after the run.
        planned_branch: Planned branch name (for worktree isolation; None otherwise).
        planned_execution_cwd: Planned execution workspace path (None for source-only).
        source_git_root: Git root of source workspace (None if non-git).
        source_git_common_dir: Git common dir of source workspace (None if non-git).
        source_head_oid: Full SHA of HEAD in source workspace (None if unavailable).
        source_head_ref: Symbolic ref of HEAD, e.g. "refs/heads/main" (None if detached).
        source_branch: Short branch name (None if detached or unavailable).
    """

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


def compute_repo_fingerprint_from_common_dir(git_common_dir: str) -> str:
    """Compute a deterministic 12-char fingerprint for a git common directory.

    Resolves the filesystem path, then hashes the resolved posix path with
    SHA-256 and truncates to 12 hex chars.
    Stable across paths with spaces, unicode, and symlinks pointing to the same dir.
    Distinct repos produce distinct fingerprints.
    """
    resolved = Path(git_common_dir).resolve(strict=True).as_posix()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def repo_fingerprint(git_common_dir: str) -> str:
    """Backward-compatible alias for compute_repo_fingerprint_from_common_dir."""
    return compute_repo_fingerprint_from_common_dir(git_common_dir)


def _run_git(
    source_git_root: str,
    args: list[str],
    *,
    timeout_seconds: int = GIT_MUTATION_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", source_git_root, *args]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return timeout_completed_process(
            command,
            exc,
            timeout_seconds=timeout_seconds,
        )


def _raise_if_git_timed_out(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == GIT_TIMEOUT_RETURN_CODE:
        raise IsolationExecutionError("git_timeout", f"{action} timed out: {result.stderr}")


def short_run_id(run_id: str) -> str:
    """Convert a full delegate run id to a short, filesystem-safe identifier.

    Strips the 'del_' prefix, replaces non-alphanumeric chars (except ._-) with '-',
    and truncates to 32 characters.
    """
    s = run_id
    if s.startswith("del_"):
        s = s[4:]
    s = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in s)
    return s[:32]


def branch_label(engine: str, model_alias: str | None) -> str:
    """Compute the label segment of a worktree branch name.

    - cursor: "cursor"
    - codex: "codex"
    - droid: "droid-<slug>" where slug is lowercase, non-alnum→'-', collapsed.
    """
    if engine == "cursor":
        return "cursor"
    if engine == "codex":
        return "codex"
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
    """Plan the full branch name: delegate/<label>-<short-id>."""
    return f"delegate/{label}-{short_id}"


def plan_worktree_path(data_home: Path, fingerprint: str, label: str, short_id: str) -> Path:
    """Plan the worktree directory path.

    The path is <dataHome>/<fingerprint>/<label>-<short-id>.
    data_home comes from worktrees_data_home() and replaces the
    ~/.delegate/worktrees default prefix.
    """
    return data_home / fingerprint / f"{label}-{short_id}"


def worktrees_data_home(config: JsonObject) -> Path:
    """Return the data home directory for persistent worktrees.

    If config worktrees.dataHome is set (non-empty string), uses that.
    Otherwise defaults to ~/.delegate/worktrees.
    The returned path is used as the base for <fingerprint>/<label>-<id>.
    """
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
    """Map 'auto' isolation to the legacy execution behavior for the engine/mode pair.

    Returns 'worktree' for cursor/codex safe (temp isolated workspace),
    'none' for droid safe and any work mode.
    """
    if mode == "safe" and engine in ("cursor", "codex"):
        return ISOLATION_WORKTREE
    return ISOLATION_NONE


def _isolation_lifecycle(isolation_mode: str, mode: str) -> str:
    """Determine isolation lifecycle."""
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
    """Build an IsolationContext from a resolved isolation value.

    Maps auto → legacy mode, sets lifecycle, and computes planned
    worktree paths when effective isolation is worktree and a git
    common directory is available.

    model_alias: the raw model alias string (e.g. "qwen") used for
        branch label computation (droid only). Passed as the model
        parameter to branch_label().
    run_short_id is used for planned path/branch names (e.g. a short
    run-id or a placeholder string like '<short-run-id-placeholder>').
    """
    # isolation_mode preserves the raw resolved value (auto/none/worktree).
    # effective_isolation is the mapped behavior (none/worktree, never auto).
    isolation_mode_raw = resolved_isolation
    if resolved_isolation == ISOLATION_AUTO:
        effective = _map_auto_isolation(engine, mode)
    else:
        effective = resolved_isolation

    lifecycle = _isolation_lifecycle(effective, mode)
    preserved = lifecycle == "persistent"

    # Compute planned paths when effective isolation is worktree.
    planned_branch: str | None = None
    planned_execution_cwd: str | None = None
    if effective == ISOLATION_WORKTREE and source_git_common_dir is not None:
        try:
            fp = compute_repo_fingerprint_from_common_dir(source_git_common_dir)
        except (FileNotFoundError, OSError):
            fp = None
        if fp is not None:
            short_id = run_short_id if run_short_id else "<short-run-id-placeholder>"
            label = branch_label(engine, model_alias)
            planned_branch = plan_branch_name(label, short_id)
            dh = worktrees_data_home(config or {})
            planned_execution_cwd = str(
                plan_worktree_path(dh, fp, label, short_id)
            )

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


# ---------------------------------------------------------------------------
# Wave 3: execution-side helpers (validation, creation, prompt injection)
# ---------------------------------------------------------------------------


class IsolationExecutionError(Exception):
    """Lightweight error raised by isolation execution helpers.

    Callers (cli.py) catch and convert to DelegateError with the
    appropriate exit code.
    """

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def require_valid_head(source_git_root: str) -> str:
    """Validate that HEAD exists and return its OID.

    Raises IsolationExecutionError("missing_git_head", ...) when the
    repository has no commits (empty/unborn repo).
    """
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
    """Require the source workspace to be clean (no staged, unstaged,
    untracked, or submodule dirtiness).

    Raises IsolationExecutionError("dirty_source_workspace", ...) when
    git status --porcelain produces any output or exits non-zero.
    """
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
    """Create a persistent Git worktree on a new branch from base_oid.

    Raises IsolationExecutionError("branch_collision", ...) when the
    branch name already exists. Raises IsolationExecutionError(
    "worktree_create_failed", ...) for other git failures (including
    "branch already checked out").
    """
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


# Exact text from spec § "Prompt context note".
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
    """Prepend the persistent-worktree context note AFTER the mandatory
    skill-review prefix and BEFORE the user prompt.

    The skill-review prefix is always at position 0 (added by
    effective_prompt / prepend_skill_review_instructions). This
    function inserts the worktree context between the skill-review
    prefix and the rest of the prompt.
    """
    from delegate_agent.runner import (
        SKILL_REVIEW_PREFIX,  # lazy: avoids runner -> cli -> isolation cycle
    )

    if prompt.startswith(SKILL_REVIEW_PREFIX):
        insert_at = len(SKILL_REVIEW_PREFIX)
        return prompt[:insert_at] + PERSISTENT_WORKTREE_CONTEXT_NOTE + prompt[insert_at:]
    return PERSISTENT_WORKTREE_CONTEXT_NOTE + prompt
