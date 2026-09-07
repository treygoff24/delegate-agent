import io
import json
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from delegate_agent import cli_parser, command_help, config, errors, request_models

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*argv: str) -> tuple[int, str, str]:
    from delegate_agent import cli

    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_parsed_command_has_one_payload_field():
    assert {field.name for field in fields(request_models.ParsedCommand)} == {
        "subcommand",
        "global_options",
        "help_topic",
        "payload",
    }
    parsed = cli_parser.parse_cli(["codex", "work", "ship"])
    assert isinstance(parsed.payload, request_models.LaunchOptions)
    assert parsed.payload.prompt_parts == ["ship"]


def test_cli_help_and_version_do_not_import_execution_runtime():
    code = (
        "import sys; sys.path.insert(0, 'src'); import delegate_agent.cli; "
        "assert 'delegate_agent.runner' not in sys.modules; "
        "assert 'delegate_agent.workflows.runtime' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_top_help_is_compact_and_complete():
    _code, output, _error = run_cli("--help")
    assert len(output.encode()) <= 4096
    command_lines = output.split("Commands:\n", 1)[1].split("\n\n", 1)[0]
    names = {name.strip() for line in command_lines.splitlines() for name in line.split(" | ")}
    assert names == {name for name, spec in command_help.COMMAND_SPECS.items() if not spec.internal}


def test_overview_discovers_a_new_command_from_the_registry(monkeypatch):
    spec = command_help.CommandSpec(
        "future-command", "A new command.", ("delegate future-command",)
    )
    monkeypatch.setitem(command_help.COMMAND_SPECS, spec.name, spec)
    assert spec.name in command_help.render_overview_text()


def test_agent_guidance_uses_the_focused_help_renderer():
    code, output, error = run_cli("agent-help")
    assert code == 0, error
    assert output == command_help.render_command_help_text(command_help.COMMAND_SPECS["agent-help"])


def test_followup_options_after_handle_do_not_become_a_live_prompt():
    parsed = cli_parser.parse_cli(
        ["followup", "codex-1", "--dry-run", "--prompt-file", "task.md", "--timeout", "60"]
    )
    assert parsed.payload.dry_run is True
    assert parsed.payload.prompt_file == "task.md"
    assert parsed.payload.timeout == 60
    assert parsed.payload.prompt_parts == []


def test_followup_explicit_literal_prompt_preserves_option_words():
    parsed = cli_parser.parse_cli(["followup", "codex-1", "--", "--dry-run", "literal"])
    assert parsed.payload.dry_run is False
    assert parsed.payload.prompt_parts == ["--dry-run", "literal"]


@pytest.mark.parametrize("args", [("--json", "describe"), ("--json", "describe", "--summary")])
def test_compact_describe_is_bounded_and_complete(args):
    _code, output, error = run_cli(*args)
    assert not error
    assert len(output.encode()) <= 12288
    payload = json.loads(output)
    assert all(
        any(entry.get("command") == name for entry in payload["commands"])
        for name, spec in command_help.COMMAND_SPECS.items()
        if not spec.internal
    )


def test_full_describe_is_explicit():
    code, output, error = run_cli("--json", "describe", "--full")
    assert code == 0, error
    payload = json.loads(output)
    assert payload["summary"] is False
    assert "modeMapping" in payload


def test_agent_help_json_is_a_help_envelope():
    code, output, error = run_cli("--json", "agent-help")
    assert code == 0, error
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["command"] == "agent-help"


def test_unsupported_global_options_are_rejected_from_snapshot():
    parsed_error = None
    with pytest.raises(errors.DelegateError) as caught:
        cli_parser.parse_cli(["--isolation", "worktree", "snapshot", "example"])
    parsed_error = caught.value
    assert getattr(parsed_error, "error", None) == "invalid_option_combination"

    code, _output, error = run_cli("--pass-through", "snapshot", "example")
    assert code != 0
    assert "pass-through" in error


def test_droid_uses_unified_model_option_and_keeps_alias_provenance():
    with pytest.raises(errors.DelegateError) as caught:
        cli_parser.parse_cli(["droid", "reviewer", "safe", "review"])
    assert getattr(caught.value, "error", None) == "invalid_droid_model_syntax"

    parsed = cli_parser.parse_cli(["droid", "safe", "--model", "reviewer", "review"])
    assert parsed.payload.model_alias is None
    assert parsed.payload.model == "reviewer"

    launch_config = config.embedded_default_config()
    launch_config["droid"]["models"] = {"reviewer": "model-id"}
    from delegate_agent import request_build

    request = request_build.request_from_parsed(
        parsed,
        launch_config,
        io.StringIO(),
        workspace=request_models.ResolvedWorkspace(str(ROOT), "git"),
    )
    assert request.model_alias == "reviewer"
    assert request.model == "model-id"


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize(
    "name",
    [
        name
        for name in command_help.COMMAND_SPECS
        if name.split()[0]
        not in {*cli_parser.KNOWN_ENGINES, "dry-run", "run", "resume", "followup"}
    ],
)
def test_nonlaunch_global_advertisement_and_refusals(name, position):
    spec = command_help.COMMAND_SPECS[name]
    allowed = {"--cwd", "--json"}
    if name in {"models", "capabilities", "profiles", "setup"}:
        allowed.add("--auth-profile")
    if name in {"workflow run", "workflow resume"}:
        allowed.add("--notify")
    if name.split()[0] in {"config", "doctor", "promote", "setup", "help"}:
        allowed.remove("--cwd")
    flags = {
        "--pass-through": [],
        "--isolation": ["worktree"],
        "--completion-report": ["markdown"],
        "--no-completion-report": [],
        "--group": ["wave4"],
        "--notify": ["room:example"],
        "--auth-profile": ["example"],
        "--cwd": ["/tmp"],
    }
    advertised = {
        option["flag"] for option in command_help.command_help_payload(spec)["globalOptions"]
    }
    assert advertised == allowed
    local = {option.flag for option in spec.options}
    for flag, values in flags.items():
        if flag in allowed or (position == "after" and flag in local):
            continue
        path = name.split()
        argv = [flag, *values, *path] if position == "before" else [*path, flag, *values]
        with pytest.raises(errors.DelegateError) as caught:
            cli_parser.parse_cli(argv)
        assert caught.value.error == "invalid_option_combination", (argv, caught.value)
        assert flag in str(caught.value)


@pytest.mark.parametrize(
    "command", ["runs", "ps", "wait", "worktree list", "worktree remove", "worktree prune"]
)
def test_local_group_option_remains_effective(command):
    parsed = cli_parser.parse_cli([*command.split(), "--group", "wave4"])
    assert parsed.payload.group == "wave4"


@pytest.mark.parametrize("command", ["wait", "run-output"])
def test_local_completion_report_remains_effective(command):
    parsed = cli_parser.parse_cli([command, "codex-1", "--completion-report"])
    assert parsed.payload.completion_report is True


@pytest.mark.parametrize(
    "args",
    [
        ["snapshot", "codex-1"],
        ["runs"],
        ["ps"],
        ["wait", "codex-1"],
        ["run-output", "codex-1"],
        ["cancel", "codex-1"],
        ["worktree", "list"],
        ["mail", "inbox"],
        ["models"],
        ["capabilities"],
        ["describe"],
        ["profiles"],
        ["personas"],
        ["workflow", "check", "workflow.py"],
    ],
)
def test_supported_global_cwd_and_json_reach_parsed_options(args):
    for prefix in (True, False):
        flags = ["--cwd", "/tmp/example", "--json"]
        parsed = cli_parser.parse_cli(flags + args if prefix else args + flags)
        assert parsed.global_options.cwd == "/tmp/example"
        assert parsed.global_options.json_mode is True


@pytest.mark.parametrize("command", ["models", "capabilities", "profiles", "setup"])
def test_supported_auth_profile_reaches_parsed_options(command):
    parsed = cli_parser.parse_cli(["--auth-profile", "example", command])
    assert parsed.global_options.auth_profile == "example"


@pytest.mark.parametrize("command", ["run", "resume"])
def test_workflow_notify_reaches_command(command):
    parsed = cli_parser.parse_cli(["--notify", "room:example", "workflow", command, "example"])
    assert parsed.payload.notify == "room:example"


def test_completion_report_word_as_cwd_is_not_a_passed_flag():
    parsed = cli_parser.parse_cli(["--cwd", "--completion-report", "snapshot", "codex-1"])
    assert parsed.global_options.cwd == "--completion-report"
