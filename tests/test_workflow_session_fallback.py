"""A structured retry whose resumed session is gone relaunches instead of ending (dlg-3pl.4).

The retry resumes the failed child's native session to ask for the structured
result alone. When the launch cannot find that session (the estate launcher landed
on a different account than the one holding it), the call used to end there. It now
redoes the attempt as a fresh launch, without spending a retry, unless the earlier
attempt already landed work in the tree.
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from delegate_agent.workflows import registry, runtime
from tests import test_workflow_retry_outcomes as helpers

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}
TASK = "review the entire repository"
CHANGED = {"changedFilesCount": 2, "commitsCreatedCount": 1, "noChanges": False}
VERIFIED_CLEAN = {
    "changedFilesCount": 0,
    "commitsCreatedCount": 0,
    "noChanges": True,
    "fileInspectionStatus": "verified",
    "commitInspectionStatus": "verified",
}
# What a failed `git status` looks like to a reader of the counts alone: zero files
# and zero commits, exactly as for a clean tree. Only the status fields tell them apart.
FILES_UNVERIFIED = {
    **VERIFIED_CLEAN,
    "noChanges": False,
    "fileInspectionStatus": "unverified",
    "warnings": ["git status --porcelain=v1 failed: fatal: index file corrupt"],
}
COMMITS_UNVERIFIED = {
    **VERIFIED_CLEAN,
    "commitsCreatedCount": None,
    "noChanges": False,
    "commitInspectionStatus": "unverified",
}
# A summary from a delegate that predates fileInspectionStatus.
NO_FILE_STATUS_FIELD = {
    key: value for key, value in VERIFIED_CLEAN.items() if key != "fileInspectionStatus"
}


@pytest.fixture
def dsl():
    case = helpers.ChildAttemptOutcomeTests()
    case.setUp()
    try:
        yield case._dsl()
    finally:
        case.doCleanups()


def child(text, run_id, *, session="thread-1", reason=None, work_summary=None):
    outcome = None
    if reason is not None:
        outcome = runtime.ChildAttemptOutcome(run_id=run_id, failure_reason=reason, exit_code=1)
    return runtime._DelegateChildResult(
        text=text,
        run_id=run_id,
        execution_cwd="/worktrees/child-1",
        session_id=session,
        outcome=outcome,
        work_summary=work_summary,
    )


def invoke(dsl, engine, *, retries, mode="safe"):
    return dsl._run_structured_or_text(
        engine,
        TASK,
        mode=mode,
        model=None,
        effort=None,
        fast=None,
        schema=SCHEMA,
        isolation="worktree",
        passthrough=False,
        timeout=None,
        retries=retries,
        key="stable-agent-key",
    )


def run_with(dsl, engine, results, *, retries, mode="safe"):
    """Drive one structured call through canned children; return (value, calls)."""
    queue = list(results)
    calls = []

    def run(*args, **kwargs):
        calls.append(mock.call(*args, **kwargs))
        return queue.pop(0)

    with (
        mock.patch.object(dsl, "_run_delegate", side_effect=run),
        mock.patch.object(dsl, "_release_structured_retry_worktree"),
        mock.patch.object(dsl.state.cancel_event, "wait", return_value=False),
        mock.patch.object(runtime.random, "uniform", return_value=0.5),
    ):
        value = invoke(dsl, engine, retries=retries, mode=mode)
    return value, calls


def journal(dsl, event_type):
    return [e for e in registry.iter_journal(dsl.state.journal_path) if e["type"] == event_type]


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_missing_resumed_session_relaunches_without_spending_a_retry(dsl, engine):
    value, calls = run_with(
        dsl,
        engine,
        [
            child('{"ok": "wrong"}', "child-1"),
            child(None, "child-2", session=None, reason="session_expired"),
            child('{"ok": true}', "child-3", session=None),
        ],
        # One retry: the resume that found no session must not have used it up.
        retries=1,
    )

    assert value == {"ok": True}
    assert [c.kwargs["resume_session_id"] for c in calls] == [None, "thread-1", None]
    # The relaunch carries the task again (a resumed session held it, a fresh one does not)
    # and still runs in the first attempt's worktree.
    assert TASK in calls[2].args[1]
    assert "Re-emit the StructuredOutput now" not in calls[2].args[1]
    assert calls[2].kwargs["structured_retry_run_id"] == "child-1"
    retries = journal(dsl, "agent_structured_retry")
    assert [e["strategy"] for e in retries] == ["resume", "relaunch"]
    assert "fellBackFrom" not in retries[0]
    fallback = retries[1]
    assert fallback["fellBackFrom"] == "resume"
    assert fallback["sessionId"] == "thread-1"
    assert fallback["childAttemptOutcome"]["failureReason"] == "session_expired"
    assert fallback["childAttemptOutcome"]["failureKind"] == "session_lost"


def test_a_relaunch_that_fails_validation_resumes_its_own_new_session(dsl):
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1"),
            child(None, "child-2", session=None, reason="session_expired"),
            child('{"ok": "still wrong"}', "child-3", session="thread-2"),
            child('{"ok": true}', "child-4", session="thread-2"),
        ],
        retries=2,
    )

    assert value == {"ok": True}
    assert [c.kwargs["resume_session_id"] for c in calls] == [None, "thread-1", None, "thread-2"]


def test_missing_session_after_landed_work_refuses_instead_of_relaunching(dsl):
    # A fresh child would redo the task on top of work that already landed.
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1", work_summary=CHANGED),
            child(None, "child-2", session=None, reason="session_expired", work_summary=CHANGED),
            child('{"ok": true}', "child-3", session=None),
        ],
        retries=2,
    )

    assert value is None
    assert len(calls) == 2
    refused = journal(dsl, "agent_structured_retry_refused")
    assert [e["reason"] for e in refused] == ["work_changed_session_missing"]
    assert refused[0]["workSummary"] == CHANGED
    assert not [e for e in journal(dsl, "agent_structured_retry") if e.get("fellBackFrom")]


def test_work_mode_relaunches_when_every_inspection_verified_a_clean_tree(dsl):
    # The positive case the refusals below are measured against.
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1", work_summary=VERIFIED_CLEAN),
            child(
                None,
                "child-2",
                session=None,
                reason="session_expired",
                work_summary=VERIFIED_CLEAN,
            ),
            child('{"ok": true}', "child-3", session=None, work_summary=VERIFIED_CLEAN),
        ],
        retries=1,
        mode="work",
    )

    assert value == {"ok": True}
    assert [c.kwargs["resume_session_id"] for c in calls] == [None, "thread-1", None]
    assert not journal(dsl, "agent_structured_retry_refused")


@pytest.mark.parametrize(
    "first,resumed",
    [
        # Uncommitted work landed but `git status` failed: zero files, zero commits.
        pytest.param(FILES_UNVERIFIED, VERIFIED_CLEAN, id="prior-attempt-status-failed"),
        pytest.param(VERIFIED_CLEAN, FILES_UNVERIFIED, id="resumed-attempt-status-failed"),
        pytest.param(COMMITS_UNVERIFIED, VERIFIED_CLEAN, id="prior-attempt-commit-count-failed"),
        pytest.param(VERIFIED_CLEAN, COMMITS_UNVERIFIED, id="resumed-attempt-commit-count-failed"),
        # Only one of the two inspections is on record.
        pytest.param(NO_FILE_STATUS_FIELD, VERIFIED_CLEAN, id="prior-summary-predates-status"),
        pytest.param(VERIFIED_CLEAN, NO_FILE_STATUS_FIELD, id="resumed-summary-predates-status"),
        # No summary at all for a work-mode child that owns a tree.
        pytest.param(None, VERIFIED_CLEAN, id="prior-attempt-has-no-summary"),
        pytest.param(None, None, id="neither-attempt-has-a-summary"),
    ],
)
def test_work_mode_refuses_to_relaunch_over_a_tree_it_could_not_verify(dsl, first, resumed):
    # A failed inspection reads as a clean tree, so a relaunch would silently redo
    # the task on top of whatever landed. Refuse, and say the state was unverified.
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1", work_summary=first),
            child(None, "child-2", session=None, reason="session_expired", work_summary=resumed),
            child('{"ok": true}', "child-3", session=None, work_summary=VERIFIED_CLEAN),
        ],
        retries=2,
        mode="work",
    )

    assert value is None
    assert len(calls) == 2
    refused = journal(dsl, "agent_structured_retry_refused")
    assert [e["reason"] for e in refused] == ["work_state_unverified"]
    assert not [e for e in journal(dsl, "agent_structured_retry") if e.get("fellBackFrom")]


def test_landed_work_is_named_as_changed_even_when_another_inspection_failed(dsl):
    # Positive evidence of changes outranks the unverified label.
    value, _calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1", work_summary=CHANGED),
            child(
                None,
                "child-2",
                session=None,
                reason="session_expired",
                work_summary=FILES_UNVERIFIED,
            ),
        ],
        retries=2,
        mode="work",
    )

    assert value is None
    refused = journal(dsl, "agent_structured_retry_refused")
    assert [e["reason"] for e in refused] == ["work_changed_session_missing"]


def test_safe_mode_relaunches_without_any_summary(dsl):
    # Safe children run in temporary workspaces: no summary exists and no tree needs
    # protecting, so the fallback keeps working for read-only structured calls.
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1"),
            child(None, "child-2", session=None, reason="session_expired"),
            child('{"ok": true}', "child-3", session=None),
        ],
        retries=1,
        mode="safe",
    )

    assert value == {"ok": True}
    assert [c.kwargs["resume_session_id"] for c in calls] == [None, "thread-1", None]


def test_safe_mode_still_refuses_a_summary_whose_inspection_failed(dsl):
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1", work_summary=FILES_UNVERIFIED),
            child(None, "child-2", session=None, reason="session_expired"),
            child('{"ok": true}', "child-3", session=None),
        ],
        retries=2,
        mode="safe",
    )

    assert value is None
    assert len(calls) == 2
    refused = journal(dsl, "agent_structured_retry_refused")
    assert [e["reason"] for e in refused] == ["work_state_unverified"]


def test_a_resume_that_fails_for_another_reason_does_not_relaunch(dsl):
    # Only a missing session justifies abandoning the resumed conversation: a
    # timeout still resumes the same session on the next attempt.
    value, calls = run_with(
        dsl,
        "codex",
        [
            child('{"ok": "wrong"}', "child-1"),
            child(None, "child-2", reason="timeout"),
            child('{"ok": true}', "child-3"),
        ],
        retries=2,
    )

    assert value == {"ok": True}
    assert [c.kwargs["resume_session_id"] for c in calls] == [None, "thread-1", "thread-1"]
    assert not [e for e in journal(dsl, "agent_structured_retry") if e.get("fellBackFrom")]


def test_a_missing_session_on_the_first_attempt_is_not_a_resume_fallback(dsl):
    # Attempt 0 resumes nothing, so a session_lost failure there is an ordinary
    # child failure: no retry, no fallback event.
    value, calls = run_with(
        dsl,
        "codex",
        [child(None, "child-1", session=None, reason="session_expired")],
        retries=2,
    )

    assert value is None
    assert len(calls) == 1
    assert not journal(dsl, "agent_structured_retry")


def _payload_of(argv):
    with open(argv[argv.index("--input-json") + 1], encoding="utf-8") as handle:
        return json.load(handle)


@pytest.mark.parametrize(
    "engine,mode,resumable,expected",
    [
        ("codex", "work", False, False),
        ("claude", "work", False, False),
        ("codex", "work", True, True),
        ("claude", "work", True, True),
        # Safe, call and other-engine children never carried the key.
        ("codex", "safe", False, None),
        ("cursor", "work", False, None),
    ],
)
def test_workflow_children_send_an_explicit_resumable_choice(
    dsl, engine, mode, resumable, expected
):
    """A fan-out must not silently retain every child's session and worktree.

    Standalone codex/claude work Runs are resumable by default; workflow children
    keep the per-agent() opt-in, so the payload says False rather than omitting the key.
    """
    seen = []

    def fake_run(argv, **_kwargs):
        seen.append(_payload_of(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"ok": True, "runId": "child", "assistantText": "done"}).encode(),
            b"",
        )

    with mock.patch.object(runtime, "_run_child_command_for_state", side_effect=fake_run):
        dsl._run_delegate(
            engine,
            "do it",
            mode=mode,
            model=None,
            effort=None,
            fast=None,
            isolation=None,
            passthrough=False,
            timeout=None,
            output_schema=None,
            prefer_assistant=True,
            workflow_agent_key="k",
            label="l",
            resumable=resumable,
        )

    assert len(seen) == 1
    if expected is None:
        assert "resumable" not in seen[0]
    else:
        assert seen[0]["resumable"] is expected
