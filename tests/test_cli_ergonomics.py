"""CLI ergonomics and errors that carry their rule (dlg-qd1).

Each case is a command an agent actually typed. It either got a bare
"does not support option" or no compact view existed. The tests pin the
new behavior: a compact count view, and errors that name the rule and the
command to use instead.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from delegate_agent import config as delegate_config
from delegate_agent import request_build, request_models, run_registry
from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase


class RunsSummaryTests(CommandTestBase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="delegate-runs-summary-")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def _run(self, harness: str, status: str, group: str | None = None) -> None:
        metadata = {"mode": "work"}
        if group is not None:
            metadata["group"] = group
        run_id, _alias = run_registry.register_run(
            self.registry_root, harness=harness, metadata=metadata
        )
        state = {"status": status, "lastActivityAt": "2026-09-24T12:00:00Z"}
        if status == "running":
            state["pid"] = os.getpid()
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, run_id) / run_registry.STATE_FILE,
            state,
        )

    def test_summary_counts_every_match_by_status_harness_and_group(self):
        self._run("codex", "running", group="wave")
        self._run("codex", "succeeded", group="wave")
        self._run("cursor", "failed", group="wave")
        self._run("cursor", "succeeded")

        code, stdout, stderr = self.run_main(
            ["--json", "--cwd", str(self.workspace), "runs", "--summary"]
        )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema"], "delegate.runs.summary.v1")
        self.assertNotIn("runs", payload)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["byStatus"], {"succeeded": 2, "failed": 1, "running": 1})
        self.assertEqual(payload["byHarness"], {"codex": 2, "cursor": 2})
        self.assertEqual(payload["byGroup"], {"wave": 3, "(none)": 1})

        code, stdout, _stderr = self.run_main(
            ["--cwd", str(self.workspace), "runs", "--group", "wave", "--summary"]
        )
        self.assertEqual(code, 0)
        self.assertIn("total: 3", stdout)
        self.assertIn("harness: codex 2, cursor 1", stdout)

    def test_summary_deduplicates_copied_registry_in_every_bucket(self):
        self._run("codex", "running", group="wave")
        self._run("codex", "succeeded", group="wave")
        self._run("cursor", "failed", group="wave")
        self._run("cursor", "succeeded")
        linked = self.workspace / "linked"
        copied = linked / ".delegate"
        shutil.copytree(self.registry_root, copied)

        with mock.patch(
            "delegate_agent.inspection_commands.linked_registry_roots",
            return_value=[(str(linked), copied)],
        ):
            code, stdout, stderr = self.run_main(
                ["--json", "--cwd", str(self.workspace), "runs", "--summary"]
            )
            self.assertEqual(code, 0, stderr)
            payload = json.loads(stdout)
            code, stdout, stderr = self.run_main(
                ["--json", "--cwd", str(self.workspace), "runs", "--limit", "1"]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["total"], json.loads(stdout)["total"])
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["byStatus"], {"succeeded": 2, "failed": 1, "running": 1})
        self.assertEqual(payload["byHarness"], {"codex": 2, "cursor": 2})
        self.assertEqual(payload["byGroup"], {"wave": 3, "(none)": 1})

        # Conflicting copies still use the earlier root's bucket values.
        for run_id in run_registry.load_index(copied)["runs"]:
            run_registry.write_json_atomic(
                run_registry.run_directory(copied, run_id) / run_registry.STATE_FILE,
                {"status": "cancelled", "lastActivityAt": "2026-09-24T13:00:00Z"},
            )
        with mock.patch(
            "delegate_agent.inspection_commands.linked_registry_roots",
            return_value=[(str(linked), copied)],
        ):
            code, stdout, stderr = self.run_main(
                ["--json", "--cwd", str(self.workspace), "runs", "--summary"]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout), payload)

    def test_empty_summary_uses_listing_warning_in_json_and_text(self):
        for flags in ([], ["--running"], ["--active"], ["--stale"], ["--group", "missing"]):
            with self.subTest(flags=flags):
                base = ["--cwd", str(self.workspace), "runs", *flags]
                code, listing, stderr = self.run_main(["--json", *base])
                self.assertEqual(code, 0, stderr)
                code, summary, stderr = self.run_main(["--json", *base, "--summary"])
                self.assertEqual(code, 0, stderr)
                warnings = json.loads(summary)["warnings"]
                self.assertEqual(warnings, json.loads(listing)["warnings"])
                self.assertIn("use --cwd PATH", warnings[0])
                code, summary, stderr = self.run_main([*base, "--summary"])
                self.assertEqual(code, 0, stderr)
                self.assertIn(f"warning: {warnings[0]}", summary)

    def test_status_filtered_summary_uses_listing_warning(self):
        self._run("codex", "succeeded")
        for flag in ("--running", "--active", "--stale"):
            with self.subTest(flag=flag):
                base = ["--json", "--cwd", str(self.workspace), "runs", flag]
                code, listing, stderr = self.run_main(base)
                self.assertEqual(code, 0, stderr)
                code, summary, stderr = self.run_main([*base, "--summary"])
                self.assertEqual(code, 0, stderr)
                warnings = json.loads(summary)["warnings"]
                self.assertEqual(warnings, json.loads(listing)["warnings"])
                self.assertIn(f"Drop {flag}", warnings[0])

    def test_summary_refuses_row_shaping_options(self):
        for option in (["--limit", "3"], ["--structural"]):
            with self.subTest(option=option[0]):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(["runs", "--summary", *option])
                self.assertEqual(caught.exception.error, "invalid_option_combination")
                self.assertIn("counts with no rows", caught.exception.message)


class OneRunRequestHintTests(CommandTestBase):
    def test_runs_and_ps_point_handle_options_at_snapshot_and_run_output(self):
        for argv in (
            ["ps", "--id", "codex-3"],
            ["runs", "--alias=codex-3"],
            ["runs", "codex-3"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(argv)
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("delegate snapshot HANDLE", caught.exception.message)
                self.assertIn("delegate run-output HANDLE", caught.exception.message)

    def test_other_unknown_runs_options_keep_the_plain_message(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["runs", "--verbose"])
        self.assertEqual(caught.exception.message, "runs does not support option: --verbose")

    def test_show_and_status_subcommands_suggest_the_per_run_commands(self):
        for word in ("show", "status", "logs"):
            with self.subTest(word=word):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli([word, "codex-3"])
                self.assertEqual(caught.exception.error, "unknown_subcommand")
                self.assertIn("delegate run-output HANDLE", caught.exception.message)


class MailBodyOptionTests(CommandTestBase):
    def test_body_option_names_the_positional_rule(self):
        for option in ("--body", "--message=hi", "-m"):
            with self.subTest(option=option):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(["mail", "send", "--to", "coordinator", option, "hi"])
                message = caught.exception.message
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("the body is the positional BODY, --file FILE, or '-'", message)
                self.assertIn("delegate mail send --to coordinator", message)


class FollowupInheritedRouteTests(CommandTestBase):
    def test_route_options_say_the_route_is_inherited_and_point_at_resume(self):
        for argv in (
            ["followup", "--model", "gpt-5.5", "codex-1", "go on"],
            ["followup", "--progress", "codex-1", "go on"],
            ["followup", "--reasoning-effort=high", "codex-1", "go on"],
        ):
            with self.subTest(option=argv[1]):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(argv)
                message = caught.exception.message
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn("inherits the source run's route", message)
                option = argv[1].split("=", 1)[0]
                self.assertIn(f"delegate resume {option} ... HANDLE", message)

    def test_non_route_resume_only_options_get_the_same_message(self):
        # dlg-qd1: --include-dirty, --persona, and --continuity-mode are not
        # routes, so the refusal must read as "resume owns this" for them too.
        for argv in (
            ["followup", "--include-dirty", "codex-1", "go on"],
            ["followup", "--persona", "reviewer", "codex-1", "go on"],
            ["followup", "--continuity-mode", "pinned", "codex-1", "go on"],
        ):
            with self.subTest(option=argv[1]):
                with self.assertRaises(DelegateError) as caught:
                    parse_cli(argv)
                message = caught.exception.message
                self.assertEqual(caught.exception.error, "unknown_option")
                self.assertIn(f"followup has no {argv[1]} option", message)
                self.assertIn(f"{argv[1]} belongs to resume", message)
                self.assertIn(f"delegate resume {argv[1]} ... HANDLE", message)

    def test_non_route_unknown_option_keeps_the_plain_message(self):
        with self.assertRaises(DelegateError) as caught:
            parse_cli(["followup", "--bogus", "codex-1", "go on"])
        self.assertEqual(caught.exception.error, "unknown_option")
        self.assertEqual(caught.exception.message, "followup does not support option: --bogus.")
        self.assertNotIn("inherits", caught.exception.message)


class LaunchNoticeTests(CommandTestBase):
    """Notices that change what a child runs against print when it launches."""

    def _repo(self) -> str:
        temp = tempfile.TemporaryDirectory(prefix="delegate-launch-notice-")
        self.addCleanup(temp.cleanup)
        repo = str(Path(temp.name).resolve())
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
        )
        return repo

    def _launch(self, argv: list[str], config: dict, *, dry_run: bool = False):
        repo = self._repo()
        if dry_run:
            argv = ["dry-run", *argv]
        parsed = parse_cli(argv)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self._config_env, clear=False):
            request = request_build.request_from_parsed(
                parsed,
                config,
                io.StringIO(),
                stderr,
                workspace=request_models.ResolvedWorkspace(repo, "git"),
            )
        return request, stderr.getvalue()

    def test_forbid_commit_implied_worktree_note_prints_at_launch(self):
        config = delegate_config.embedded_default_config()
        _request, stderr = self._launch(["codex", "work", "--forbid-commit", "fix it"], config)
        self.assertIn("note: --forbid-commit implies --isolation worktree", stderr)

        _request, stderr = self._launch(
            ["codex", "work", "--forbid-commit", "fix it"], config, dry_run=True
        )
        self.assertNotIn("implies --isolation worktree", stderr)

    def test_missing_codex_profile_overlay_warns_at_launch(self):
        codex_home = tempfile.TemporaryDirectory(prefix="delegate-codex-home-")
        self.addCleanup(codex_home.cleanup)
        config = delegate_config.embedded_default_config()
        config["codex"]["profile"] = "fast-lane"
        with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home.name}, clear=False):
            request, stderr = self._launch(["codex", "safe", "review it"], config)
        (warning,) = [w for w in request.warnings if w.startswith("codex.profile ")]
        self.assertIn("fast-lane.config.toml", warning)
        self.assertIn(f"delegate: warning: {warning}", stderr)

        (Path(codex_home.name) / "fast-lane.config.toml").write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_HOME": codex_home.name}, clear=False):
            request, stderr = self._launch(["codex", "safe", "review it"], config)
        self.assertFalse(any(w.startswith("codex.profile ") for w in request.warnings))
        self.assertNotIn("codex.profile", stderr)
