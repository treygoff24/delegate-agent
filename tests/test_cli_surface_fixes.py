import io
import os
import tempfile
from pathlib import Path

from delegate_agent import cli_parser, errors, request_build, run_registry
from delegate_agent import config as delegate_config
from delegate_agent.command_help import COMMAND_SPECS
from tests.execution_test_base import ExecutionTestBase


class PromptSourceHelpTests(ExecutionTestBase):
    def test_usage_and_option_help_state_one_prompt_source_rule(self):
        for name in ("codex", "cursor"):
            usage = "\n".join(COMMAND_SPECS[name].usage)
            self.assertIn("[--prompt-file PATH | prompt...]", usage)
        prompt_file = next(o for o in COMMAND_SPECS["cursor"].options if o.flag == "--prompt-file")
        self.assertIn("exactly one prompt source", prompt_file.description)


class RegistryGitignoreTests(ExecutionTestBase):
    def test_registry_creation_writes_gitignore_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = run_registry.ensure_registry(Path(tmp), workspace_kind="directory")
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "*\n")
            (root / ".gitignore").write_text("keep\n", encoding="utf-8")
            run_registry.ensure_registry(Path(tmp), workspace_kind="directory")
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "keep\n")


class IncludeDirtySafeTests(ExecutionTestBase):
    def test_safe_include_dirty_is_noop_with_warning(self):
        repo, _ = self._make_git_repo_with_commit()
        parsed = cli_parser.parse_cli(
            ["--cwd", repo.name, "codex", "safe", "--include-dirty", "task"]
        )
        request = request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO()
        )
        self.assertFalse(request.include_dirty)
        self.assertTrue(
            any("--include-dirty is a no-op in safe mode" in w for w in request.warnings)
        )

    def test_work_without_worktree_still_refused(self):
        repo, _ = self._make_git_repo_with_commit()
        parsed = cli_parser.parse_cli(
            ["--cwd", repo.name, "--isolation", "none", "codex", "work", "--include-dirty", "t"]
        )
        with self.assertRaises(errors.DelegateError) as ctx:
            request_build.request_from_parsed(
                parsed, delegate_config.embedded_default_config(), io.StringIO()
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")


class RunsShowTests(ExecutionTestBase):
    def test_runs_show_names_the_readers(self):
        with self.assertRaises(errors.DelegateError) as ctx:
            cli_parser.parse_cli(["runs", "show", "codex-1"])
        message = ctx.exception.message
        self.assertIn("delegate snapshot HANDLE", message)
        self.assertIn("delegate run-output HANDLE", message)
        self.assertIn("completion-report.md", message)


class PromptFileCwdTests(ExecutionTestBase):
    def _request(self, repo, prompt_file):
        parsed = cli_parser.parse_cli(
            ["--cwd", repo, "codex", "safe", "--prompt-file", prompt_file]
        )
        return request_build.request_from_parsed(
            parsed, delegate_config.embedded_default_config(), io.StringIO()
        )

    def _in(self, path):
        old = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, old)

    def test_relative_prompt_file_falls_back_to_cwd_option_with_warning(self):
        repo, _ = self._make_git_repo_with_commit()
        (Path(repo.name) / "p.md").write_text("from repo", encoding="utf-8")
        elsewhere = tempfile.mkdtemp()
        self._in(elsewhere)
        request = self._request(repo.name, "p.md")
        self.assertIn("from repo", request.prompt)
        self.assertTrue(any("relative to --cwd" in w for w in request.warnings))

    def test_shell_cwd_match_wins(self):
        repo, _ = self._make_git_repo_with_commit()
        (Path(repo.name) / "p.md").write_text("from repo", encoding="utf-8")
        elsewhere = tempfile.mkdtemp()
        (Path(elsewhere) / "p.md").write_text("from shell", encoding="utf-8")
        self._in(elsewhere)
        request = self._request(repo.name, "p.md")
        self.assertIn("from shell", request.prompt)
        self.assertFalse(any("relative to --cwd" in w for w in request.warnings))

    def test_missing_everywhere_names_both_absolute_paths(self):
        repo, _ = self._make_git_repo_with_commit()
        elsewhere = os.path.realpath(tempfile.mkdtemp())
        self._in(elsewhere)
        with self.assertRaises(errors.DelegateError) as ctx:
            self._request(repo.name, "nope.md")
        self.assertEqual(ctx.exception.error, "prompt_file_not_found")
        self.assertIn(os.path.join(elsewhere, "nope.md"), ctx.exception.message)
        self.assertIn(os.path.join(repo.name, "nope.md"), ctx.exception.message)
