"""Failed output is evidence, not a task result; retry only workflow transients."""

import json
import subprocess
from unittest import mock

import pytest

from delegate_agent.workflows import registry, runtime
from tests import test_provider_outcomes as provider_helpers
from tests import test_workflow_retry_outcomes as helpers


@pytest.fixture
def dsl():
    case = helpers.ChildAttemptOutcomeTests()
    case.setUp()
    try:
        yield case._dsl()
    finally:
        case.doCleanups()


def invoke(dsl, schema, retries=2):
    return dsl._run_structured_or_text(
        "omp",
        "test",
        mode="safe",
        model=None,
        effort=None,
        fast=None,
        schema=schema,
        isolation="none",
        passthrough=False,
        timeout=None,
        retries=retries,
        key="failure-policy",
    )


SCHEMA = {"type": "object", "required": ["ok"]}
TERMINAL = (
    "provider_refusal",
    "provider_max_turns",
    "usage_limit",
    "auth_failed",
    "provider_error",
    "provider_cancelled",
    "harness_cancelled",
    "nonzero_exit",
    "output_cap",
    "output_limit_exceeded",
    "structured",
    "future_failure",
)
TRANSIENT = ("timeout", "call_timeout", "agent_timeout", "stall", "stalled")


@pytest.mark.parametrize("reason", TERMINAL + TRANSIENT)
@pytest.mark.parametrize("schema", [None, SCHEMA])
def test_failed_partial_output_never_settles_and_policy_is_bounded(dsl, reason, schema):
    child = runtime._child_result_from_payload(
        {"ok": False, "runId": "failed-child", "failureReason": reason},
        text='{"ok": true}',
    )
    with (
        mock.patch.object(dsl, "_run_delegate", return_value=child) as run,
        mock.patch.object(dsl, "_release_structured_retry_worktree"),
        mock.patch.object(dsl.state.cancel_event, "wait", return_value=False) as wait,
        mock.patch.object(runtime.random, "uniform", return_value=0.5),
    ):
        assert invoke(dsl, schema) is None
    expected = 3 if reason in TRANSIENT else 1
    assert run.call_count == expected
    assert wait.call_count == expected - 1
    assert wait.call_args_list == [mock.call(0.875 * 2**i) for i in range(expected - 1)]
    assert child.text == '{"ok": true}'
    events = list(registry.iter_journal(dsl.state.journal_path))
    failures = [e for e in events if e["type"] == "agent_attempt_failed"]
    assert len(failures) == expected
    assert all(e["childAttemptOutcome"]["failureReason"] == reason for e in failures)
    if schema is not None:
        exhausted = [e for e in events if e["type"] == "agent_structured_exhausted"]
        assert exhausted[0]["attempts"] == expected
        assert exhausted[0]["lastParsedCandidate"] is None


@pytest.mark.parametrize("schema", [None, SCHEMA])
def test_zero_retry_failed_partial_output_is_not_a_result(dsl, schema):
    child = runtime._child_result_from_payload(
        {"ok": False, "failureReason": "provider_error"},
        text='{"ok": true}',
    )
    with mock.patch.object(dsl, "_run_delegate", return_value=child):
        assert invoke(dsl, schema, retries=0) is None


def test_failed_child_report_cannot_be_salvaged(dsl):
    report = dsl.state.workspace / "report.md"
    report.write_text('```json\n{"ok": true}\n```\n')
    child = runtime._DelegateChildResult(
        text="partial",
        run_id=None,
        execution_cwd=None,
        session_id=None,
        outcome=runtime.ChildAttemptOutcome(None, "provider_refusal"),
        completion_report_source="child",
        completion_report_path=str(report),
    )
    with mock.patch.object(dsl, "_run_delegate", return_value=child):
        assert invoke(dsl, SCHEMA) is None


@pytest.mark.parametrize("fraction", [0.0, 0.5, 1.0])
def test_retry_backoff_grows_jitters_and_caps(fraction):
    with mock.patch.object(runtime.random, "uniform", return_value=fraction):
        delays = [runtime._workflow_retry_delay(i) for i in range(10)]
    assert delays == [min(30.0, 2.0**i) * (0.75 + fraction * 0.25) for i in range(10)]


def test_cancellation_during_backoff_does_not_launch_again(dsl):
    child = runtime._child_result_from_payload(
        {"ok": False, "failureReason": "timeout"},
        text=None,
    )
    with (
        mock.patch.object(dsl, "_run_delegate", return_value=child) as run,
        mock.patch.object(dsl.state.cancel_event, "wait", return_value=True),
        pytest.raises(runtime.SupervisorWatchdogExit),
    ):
        invoke(dsl, SCHEMA)
    assert run.call_count == 1


@pytest.mark.parametrize("reason", TERMINAL[:5])
def test_child_envelope_failure_name_reaches_journal(dsl, reason):
    envelope = {
        "ok": False,
        "runId": "child",
        "failureReason": reason,
        "assistantText": '{"ok": true}',
    }
    completed = subprocess.CompletedProcess([], 1, json.dumps(envelope).encode(), b"")
    with (
        mock.patch.object(runtime, "_run_child_command_for_state", return_value=completed),
        mock.patch.object(dsl, "_release_structured_retry_worktree"),
    ):
        assert invoke(dsl, SCHEMA) is None
    failures = [
        e
        for e in registry.iter_journal(dsl.state.journal_path)
        if e["type"] == "agent_attempt_failed"
    ]
    assert failures[0]["childAttemptOutcome"]["failureReason"] == reason


@pytest.mark.parametrize("schema", [None, SCHEMA])
@pytest.mark.parametrize("reason", ["provider_error", "timeout"])
def test_followup_obeys_same_failure_policy(dsl, schema, reason):
    envelope = {
        "ok": False,
        "runId": "child",
        "failureReason": reason,
        "assistantText": '{"ok": true}',
    }
    completed = subprocess.CompletedProcess([], 0, json.dumps(envelope).encode(), b"")
    with (
        mock.patch.object(runtime, "_run_child_command_for_state", return_value=completed) as run,
        mock.patch.object(dsl.state.cancel_event, "wait", return_value=False) as wait,
    ):
        result = dsl._run_followup_structured_or_text(
            runtime.CompletedChild("prior", "omp", True),
            "test",
            schema=schema,
            timeout=None,
            retries=1,
            key="followup",
        )
    assert result is None
    assert run.call_count == (2 if reason == "timeout" else 1)
    assert wait.call_count == (1 if reason == "timeout" else 0)
    failures = [
        e
        for e in registry.iter_journal(dsl.state.journal_path)
        if e["type"] == "agent_attempt_failed"
    ]
    assert failures[0]["childAttemptOutcome"]["failureReason"] == reason


@pytest.mark.parametrize(
    "engine,reason,detail",
    [
        ("omp", "provider_error", "provider rejected request"),
        ("omp", "usage_limit", "usage limit reached"),
        ("omp", "auth_failed", "401 unauthorized"),
        ("claude", "provider_refusal", "safety_refusal"),
        ("claude", "provider_max_turns", "error_max_turns"),
    ],
)
def test_stub_child_failure_record_and_partial_output_survive_workflow(dsl, engine, reason, detail):
    partial = '{"ok": true}'
    events = (
        [provider_helpers.turn("error", text=partial, errorMessage=detail)]
        if engine == "omp"
        else [
            {"type": "result", "subtype": detail, "result": partial},
        ]
    )
    code, payload, state = provider_helpers.ProviderOutcomeTests()._tracked(
        engine,
        events,
        str(dsl.state.workspace),
    )
    assert code == 1
    assert state["status"] == "failed"
    assert state["failureReason"] == reason
    assert state["assistantText"] == partial
    assert payload["assistantText"] == partial
    completed = subprocess.CompletedProcess([], code, json.dumps(payload).encode(), b"")
    with mock.patch.object(runtime, "_run_child_command_for_state", return_value=completed):
        assert invoke(dsl, SCHEMA) is None
    failures = [
        e
        for e in registry.iter_journal(dsl.state.journal_path)
        if e["type"] == "agent_attempt_failed"
    ]
    assert failures[0]["childAttemptOutcome"]["failureReason"] == reason
    assert failures[0]["childAttemptOutcome"]["runId"] == payload["runId"]
    root = provider_helpers.run_registry.ensure_registry(
        dsl.state.workspace, workspace_kind="directory"
    )
    after = provider_helpers.run_registry.load_run_state(root, payload["runId"])
    assert after == state
