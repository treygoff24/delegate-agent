import json
from pathlib import Path

import pytest

from delegate_agent import (
    cli_parser,
    command_help,
    harness_events,
    run_registry,
    runner,
    worktree_execution,
    worktree_mgmt,
    worktree_records,
    worktree_remove,
)


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


def test_late_capture_enriches_cancelled_run_without_changing_outcome(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    runner.write_state(
        registered.run_path,
        runner.build_run_record(
            ctx,
            status="cancelled",
            stdout_bytes=20,
            extra={"cancelRequested": True, "warnings": ["signal refusal was observed"]},
        ),
    )
    accumulator = harness_events.StreamAccumulator(harness="codex")
    accumulator.ingest_line(
        json.dumps({"type": "message", "role": "assistant", "content": "late output"})
    )
    status, _ = runner._persist_final_progress(
        registered.run_path,
        ctx,
        accumulator,
        status="succeeded",
        exit_code=0,
        stdout_bytes=10,
        stderr_bytes=5,
        completion_report_written=False,
        extra={},
    )
    state = run_registry.load_run_state(ctx.registry_root, ctx.run_id)
    assert status == state["status"] == "cancelled"
    assert state["assistantText"] == "late output"
    assert state["stdoutBytes"] == 20
    assert state["stderrBytes"] == 5
    assert state["failureReason"] == "cancelled_by_user"
    assert state["cancelRequested"] is True
    assert "signal refusal was observed" in state["warnings"]


def test_pending_capture_enriches_cancellation_on_read_and_reconciliation(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    runner.write_state(registered.run_path, runner.build_run_record(ctx, status="cancelled"))
    record = runner.build_run_record(ctx, status="succeeded", stdout_bytes=12)
    record["assistantText"] = "captured before the lock was available"
    run_registry.write_finalize_wal(
        ctx.registry_root, ctx.run_id, status="succeeded", record=record
    )
    observed = run_registry.load_run_state(ctx.registry_root, ctx.run_id)
    assert observed["status"] == "cancelled"
    assert observed["assistantText"] == record["assistantText"]
    assert observed["stdoutBytes"] == 12
    with run_registry.registry_lock(ctx.registry_root):
        run_registry.reconcile_finalize_wal_locked(ctx.registry_root, ctx.run_id)
    assert run_registry.load_run_state(ctx.registry_root, ctx.run_id) == observed
    assert not (registered.run_path / run_registry.FINALIZE_WAL_FILE).exists()


@pytest.mark.parametrize(
    "option,value",
    [
        ("--auth-profile", "work"),
        ("--isolation", "worktree"),
        ("--pass-through", None),
        ("--group", "group"),
    ],
)
def test_workflow_children_inherit_global_refusals(option, value):
    argv = [
        option,
        *([value] if value is not None else []),
        "workflow",
        "status",
        "wf_0123456789ab",
    ]
    with pytest.raises(cli_parser.DelegateError, match="not supported"):
        cli_parser.parse_cli(argv)
    help_payload = command_help.command_help_payload(command_help.COMMAND_SPECS["workflow status"])
    assert option not in {row["flag"] for row in help_payload["globalOptions"]}


def test_no_completion_report_cannot_be_dropped_by_wait():
    with pytest.raises(cli_parser.DelegateError, match="not supported"):
        cli_parser.parse_cli(["--no-completion-report", "wait", "codex-1"])
    assert cli_parser.parse_cli(
        ["wait", "codex-1", "--completion-report"]
    ).payload.completion_report


@pytest.mark.parametrize("user_line", ["?? user-work.md", "R  user-work.md -> .beads/user-work.md"])
def test_retirement_never_hides_user_dirt_after_display_cap(tmp_path, monkeypatch, user_line):
    lines = [f"?? .beads/entry-{index:02d}" for index in range(50)] + [user_line]
    monkeypatch.setattr(worktree_mgmt, "porcelain_status", lambda _cwd: (lines, len(lines), []))
    dirty, paths, _warnings = worktree_mgmt._effective_dirty_for_retirement(
        {"executionCwd": str(tmp_path)}, "present", (".beads/**",)
    )
    assert dirty is True
    assert len(paths) == 1


def test_empty_retirement_globs_still_discount_unchanged_seeds(tmp_path, monkeypatch):
    seeded = tmp_path / "seed.txt"
    seeded.write_text("original")
    record = {
        "executionCwd": str(tmp_path),
        "creationContext": {
            worktree_records.SYNCED_FILE_DIGESTS_KEY: worktree_records.capture_file_content_digests(
                tmp_path, ["seed.txt"]
            )
        },
    }
    monkeypatch.setattr(worktree_mgmt, "_owner_run_block_reason", lambda *_args: None)
    monkeypatch.setattr(worktree_mgmt, "detect_worktree_status", lambda _record: ("present", []))
    monkeypatch.setattr(worktree_records, "live_attachments_for_path", lambda *_args: [])
    monkeypatch.setattr(worktree_mgmt, "porcelain_status", lambda _cwd: (["?? seed.txt"], 1, []))
    assert (
        worktree_mgmt.inspect_worktree(
            tmp_path, record, check_merge=False, retirement_ignore_globs=()
        ).dirty
        is False
    )
    seeded.write_text("user edit")
    assert (
        worktree_mgmt.inspect_worktree(
            tmp_path, record, check_merge=False, retirement_ignore_globs=()
        ).dirty
        is True
    )


def test_retirement_force_never_discards_a_new_user_edit(tmp_path, monkeypatch):
    ctx = run_context(tmp_path)
    record = {
        "runId": ctx.run_id,
        "alias": ctx.alias,
        "sourceGitRoot": str(tmp_path),
        "executionCwd": ctx.execution_cwd,
        "branch": ctx.branch,
    }
    inspection = worktree_mgmt.WorktreeInspection(
        record=record, status="present", dirty=True, dirty_paths=("new-user-work.txt",)
    )
    monkeypatch.setattr(worktree_mgmt, "resolve_record", lambda *_args, **_kwargs: record)
    monkeypatch.setattr(worktree_mgmt, "inspect_worktree", lambda *_args, **_kwargs: inspection)
    with pytest.raises(worktree_mgmt.WorktreeManagementError) as caught:
        worktree_remove.remove_worktree(
            ctx.registry_root,
            handle=ctx.alias,
            keep_branch=True,
            retirement_ignore_globs=(),
        )
    assert caught.value.code == "dirty_worktree"


def test_valid_state_is_inspectable_with_corrupt_legacy_snapshot(tmp_path):
    ctx = run_context(tmp_path)
    registered = registration(ctx)
    runner.write_state(registered.run_path, runner.build_run_record(ctx, status="succeeded"))
    (registered.run_path / run_registry.SNAPSHOT_FILE).write_text("not JSON")
    assert run_registry.load_run_snapshot(ctx.registry_root, ctx.run_id)["status"] == "succeeded"
    # Read-only display availability must not launder corrupt ownership evidence.
    record = worktree_records._record_for_run(ctx.registry_root, ctx.run_id, {})
    assert "unreadable snapshot.json" in record["recordWarnings"]
    assert record["sourceGitRoot"] is None
