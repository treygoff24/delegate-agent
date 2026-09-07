from pathlib import Path

from delegate_agent import harness_events, run_registry, runner, worktree_execution, worktree_mgmt


def run_context(workspace: Path) -> runner.RunContext:
    root = run_registry.ensure_registry(workspace, workspace_kind="directory")
    run_id, alias = run_registry.register_run(root, harness="codex")
    return runner.RunContext(
        registry_root=root,
        run_id=run_id,
        alias=alias,
        harness="codex",
        engine="codex",
        mode="work",
        model="example-model",
        source_cwd=str(workspace),
        execution_cwd=str(workspace / "planned-worktree"),
        workspace_kind="directory",
        isolated_workspace=True,
        started_at="2026-09-07T00:00:00Z",
        isolation_mode="worktree",
        effective_isolation="worktree",
        isolation_lifecycle="persistent",
        source_git_root=str(workspace),
        branch="delegate/example",
        worktree_status="present",
        auth_profile="example-profile",
        temporary_workspace_cleanup={"method": "example", "path": "temporary"},
    )


def registration(ctx: runner.RunContext) -> worktree_execution.PersistentWorktreeRegistration:
    path = run_registry.run_directory(ctx.registry_root, ctx.run_id)
    runner.write_manifest(path, runner.build_manifest(ctx, ["example-engine"]))
    return worktree_execution.PersistentWorktreeRegistration(
        run_id=ctx.run_id,
        alias=ctx.alias,
        run_path=path,
        branch=ctx.branch,
        worktree_path=ctx.execution_cwd,
        creation_context={},
        pre_ctx=ctx,
    )


def test_terminal_worktree_metadata_updates_one_record_and_selection(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    with run_registry.registry_lock(ctx.registry_root):
        run_registry.publish_terminal_record_locked(
            ctx.registry_root,
            ctx.run_id,
            runner.build_run_record(ctx, status="succeeded", exit_code=0),
        )
    worktree_mgmt._persist_completion_worktree_fields(
        worktree_mgmt.RetirementContext(ctx.registry_root, ctx.run_id, "work", "persistent"),
        {"worktreeStatus": "removed"},
    )
    assert not (registered.run_path / run_registry.SNAPSHOT_FILE).exists()
    assert run_registry.load_run_state(ctx.registry_root, ctx.run_id)["worktreeStatus"] == "removed"
    entry = run_registry.load_index(ctx.registry_root)["runs"][ctx.run_id]
    assert run_registry.terminal_selection_state(ctx.registry_root, ctx.run_id, entry) is not None


def test_snapshot_preserves_immutable_launch_metadata(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    runner.persist_progress(
        registered.run_path,
        ctx,
        harness_events.StreamAccumulator(harness="codex"),
        status="creating_isolation",
    )
    snapshot = run_registry.load_run_snapshot(ctx.registry_root, ctx.run_id)
    assert snapshot["workspaceRoot"] == ctx.execution_cwd
    assert snapshot["authProfile"] == ctx.auth_profile
    assert snapshot["promptInstructionMode"] == ctx.prompt_instruction_mode
    assert snapshot["temporaryWorkspaceCleanup"] == ctx.temporary_workspace_cleanup
    assert snapshot["worktreeCleanupCommands"]["safe"] == f"delegate worktree remove {ctx.alias}"


def test_precreation_failure_has_one_terminal_record_and_only_planned_paths(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    worktree_execution._record_persistent_worktree_failure(
        registered, error="worktree_creation_failed", message="fixture failure"
    )
    state = run_registry.load_run_state(ctx.registry_root, ctx.run_id)
    assert state["status"] == "failed"
    assert state["ok"] is False
    assert state["plannedExecutionCwd"] == ctx.execution_cwd
    assert not (registered.run_path / run_registry.SNAPSHOT_FILE).exists()
    snapshot = run_registry.load_run_snapshot(ctx.registry_root, ctx.run_id)
    assert "executionCwd" not in snapshot
    assert "branch" not in snapshot


def test_precreation_failure_cannot_overwrite_cancellation(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    runner.write_state(
        registered.run_path,
        runner.build_run_record(ctx, status="creating_isolation", extra={"cancelRequested": True}),
    )
    worktree_execution._record_persistent_worktree_failure(
        registered, error="worktree_creation_failed", message="fixture failure"
    )
    state = run_registry.load_run_state(ctx.registry_root, ctx.run_id)
    assert state["status"] == "cancelled"
    assert state["failureReason"] == "cancelled_by_user"
