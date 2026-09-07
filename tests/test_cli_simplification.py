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
    assert all(
        name in output for name, spec in command_help.COMMAND_SPECS.items() if not spec.internal
    )


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
