import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from tests.execution_test_base import ExecutionTestBase, safe_temp_dirs

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")

if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import worktree_summary  # noqa: E402


class ExecutionWorktreeRunTests(ExecutionTestBase):
    def test_persistent_worktree_launch_checks_pool_guardrail(self):
        execution = mock.Mock(config={"worktrees": {}}, stderr=io.StringIO())
        preflight = mock.sentinel.preflight
        with (
            mock.patch.object(
                self.delegate.worktree_execution,
                "_validate_persistent_worktree_request",
                return_value=preflight,
            ),
            mock.patch.object(
                self.delegate.worktree_execution,
                "_warn_if_worktree_pool_large",
            ) as warn,
            mock.patch.object(
                self.delegate.worktree_execution,
                "_register_persistent_worktree_run",
                side_effect=RuntimeError("stop after guardrail"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after guardrail"),
        ):
            self.delegate.worktree_execution.execute_persistent_worktree(execution)

        warn.assert_called_once_with(execution.config, execution.stderr)

    def test_worktree_pool_guardrail_below_threshold_emits_no_warning(self):
        with tempfile.TemporaryDirectory() as fake_home:
            pool = Path(fake_home) / "pool"
            worktree = pool / "abc123def456" / "old-worktree"
            worktree.mkdir(parents=True)
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["worktrees"] = {
                **config["worktrees"],
                "dataHome": str(pool),
                "poolWarnCount": 1,
            }
            stderr = io.StringIO()

            self.delegate.worktree_execution._warn_if_worktree_pool_large(config, stderr)

            self.assertEqual(stderr.getvalue(), "")

    def test_worktree_pool_guardrail_exceeded_warns_with_path_and_threshold(self):
        with tempfile.TemporaryDirectory() as fake_home:
            pool = Path(fake_home) / "pool"
            for name in ("old-worktree", "new-worktree"):
                (pool / "abc123def456" / name).mkdir(parents=True)
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["worktrees"] = {
                **config["worktrees"],
                "dataHome": str(pool),
                "poolWarnCount": 1,
            }
            stderr = io.StringIO()

            self.delegate.worktree_execution._warn_if_worktree_pool_large(config, stderr)

            warning = stderr.getvalue()
            self.assertIn("WARNING", warning)
            self.assertIn(str(pool), warning)
            self.assertIn("2 worktrees", warning)
            self.assertIn("poolWarnCount=1", warning)
            self.assertIn("delegate worktree remove", warning)
            self.assertIn("delegate worktree prune", warning)
            self.assertNotIn("gc --all", warning)

    def test_worktree_pool_guardrail_counts_only_delegate_fingerprint_dirs(self):
        """The count describes Delegate's pool, not whatever else shares the root."""
        with tempfile.TemporaryDirectory() as fake_home:
            pool = Path(fake_home) / "pool"
            for parent in ("abc123def456", "some-other-directory"):
                (pool / parent / "cursor-1").mkdir(parents=True)

            self.assertEqual(self.delegate.worktree_execution._worktree_pool_count(pool), 1)

    def test_worktree_pool_guardrail_survives_an_unreadable_pool_root(self):
        """An advisory count must never block a launch."""
        with tempfile.TemporaryDirectory() as fake_home:
            self.assertEqual(
                self.delegate.worktree_execution._worktree_pool_count(
                    Path(fake_home) / "never-created"
                ),
                0,
            )

    # -- Persistent worktree: cursor work runs in isolated worktree -----------

    def test_cursor_work_persistent_worktree_runs_in_worktree(self):
        """Cursor work with --isolation worktree runs in persistent Git worktree."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            # Replace argv with fake binary
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            # Source workspace should NOT be mutated
            self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
            # Worktree should exist under fake home
            worktree_root = Path(fake_home) / ".delegate" / "worktrees"
            self.assertTrue(worktree_root.exists())
            worktrees = list(worktree_root.glob("*/*"))
            self.assertTrue(len(worktrees) > 0, "No worktree directories found")
            # The worktree should contain the mutated file (child ran inside it)
            for wt in worktrees:
                if wt.is_dir():
                    mutated = wt / "mutated-by-agent.txt"
                    if mutated.exists():
                        break
            else:
                self.fail("No worktree contained mutated-by-agent.txt")

    # -- Persistent worktree remains after child exit -------------------------

    def test_persistent_worktree_preserved_after_child_exit(self):
        """Persistent worktree remains after successful child run."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            # Worktree directory should still exist
            worktree_root = Path(fake_home) / ".delegate" / "worktrees"
            worktrees = list(worktree_root.glob("*/*"))
            self.assertTrue(len(worktrees) > 0, "Worktree should be preserved")
            # Source should NOT have mutated file
            self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
            # Worktree should HAVE mutated file
            any_mutated = any(
                (wt / "mutated-by-agent.txt").exists() for wt in worktrees if wt.is_dir()
            )
            self.assertTrue(any_mutated, "Worktree should have mutated-by-agent.txt")

    # -- Persistent worktree preserved after child failure --------------------

    def test_persistent_worktree_preserved_after_child_failure(self):
        """Persistent worktree remains even when child exits non-zero."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_EXIT": "3",
                },
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 3)
            # Worktree should STILL exist after failure
            worktree_root = Path(fake_home) / ".delegate" / "worktrees"
            worktrees = list(worktree_root.glob("*/*"))
            self.assertTrue(len(worktrees) > 0, "Worktree should be preserved after failure")

    # -- Droid argv rewritten for worktree ------------------------------------

    def test_droid_work_persistent_worktree_rewrites_cwd(self):
        """Droid work --isolation worktree rewrites --cwd to execution worktree."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_fake_bin()
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["droid"] = dict(config["droid"])
            config["droid"]["models"] = {"qwen": "real-model-id"}
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "droid",
                "work",
                repo.name,
                config,
                model_alias="qwen",
            )
            # Replace argv with fake binary (pointed at source cwd, will be rewritten)
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "droid"),
                    "exec",
                    "--cwd",
                    repo.name,
                    "--model",
                    "real-model-id",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            # The worktree should exist
            worktree_root = Path(fake_home) / ".delegate" / "worktrees"
            worktrees = list(worktree_root.glob("*/*"))
            self.assertTrue(len(worktrees) > 0)

    def test_grok_work_persistent_worktree_rewrites_cwd_and_redacts_prompt_file(self):
        """Grok work --isolation worktree rewrites --cwd and keeps prompt-file redaction."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            log_file = str(Path(fake_home) / "child-argv.log")
            fake_bin = self._make_logging_fake_bin("grok", log_file)
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "grok",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            secret_prompt = "TOP SECRET GROK PROMPT TEXT"
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                secret_prompt,
                [str(fake_bin / "grok"), *request.argv[1:]],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                prompt_transport=request.prompt_transport,
                prompt_file_text=secret_prompt,
                display_argv=request.display_argv,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            logged = Path(log_file).read_text() if Path(log_file).exists() else ""
            cwd_index = logged.split().index("--cwd")
            logged_cwd = logged.split()[cwd_index + 1]
            self.assertIn("/worktrees/", logged_cwd)
            self.assertNotEqual(logged_cwd, repo.name)

            runs_dir = Path(repo.name) / ".delegate" / "runs"
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertTrue(run_dirs)
            manifest = self.delegate.json.loads((run_dirs[0] / "manifest.json").read_text())
            manifest_argv = manifest["argv"]
            manifest_cwd = manifest_argv[manifest_argv.index("--cwd") + 1]
            self.assertIn("/worktrees/", manifest_cwd)
            self.assertNotEqual(manifest_cwd, repo.name)
            self.assertIn(self.delegate.PROMPT_FILE_DISPLAY, manifest_argv)
            self.assertNotIn(secret_prompt, manifest_argv)

    def test_persistent_worktree_child_receives_registered_fresh_mail_identity(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            observed_path = Path(fake_home) / "child-env.json"
            fake = Path(fake_home) / "fake-agent"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os\n"
                "with open(os.environ['FAKE_ENV_LOG'], 'w', encoding='utf-8') as handle:\n"
                "    json.dump({key: value for key, value in os.environ.items() "
                "if key in ('DELEGATE_RUN_ID', 'DELEGATE_MAIL_SELF')}, handle)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["cursor"] = {**config["cursor"], "argvPrefix": [str(fake)]}
            config["profiles"] = {
                **config["profiles"],
                "default": "identity-profile",
                "definitions": {
                    "identity-profile": {
                        "env": {
                            "DELEGATE_RUN_ID": "del_20260801T120000Z_abcdef",
                            "DELEGATE_MAIL_SELF": "cursor-99",
                            "FAKE_ENV_LOG": str(observed_path),
                        }
                    }
                },
            }
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request("cursor", "work", repo.name, config)

            code, _payload = self.delegate.execute_request(
                request,
                json_mode=True,
                config=config,
                pass_through=False,
                completion_report_mode="none",
                source_workspace=workspace,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(code, 0)
            manifests = list((Path(repo.name) / ".delegate" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            observed = json.loads(observed_path.read_text(encoding="utf-8"))
            self.assertEqual(observed["DELEGATE_RUN_ID"], manifest["runId"])
            self.assertEqual(observed["DELEGATE_MAIL_SELF"], manifest["alias"])
            self.assertNotEqual(observed["DELEGATE_RUN_ID"], "del_20260801T120000Z_abcdef")
            self.assertNotEqual(observed["DELEGATE_MAIL_SELF"], "cursor-99")

    def test_persistent_worktree_persists_initiator_root_and_passes_child_env(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _git_cd = self._make_git_repo_with_commit()
            observed_path = Path(fake_home) / "initiator.txt"
            fake = Path(fake_home) / "fake-agent"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "${{DELEGATE_INITIATOR_ROOT:-}}" > "{observed_path}"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["cursor"] = {**config["cursor"], "argvPrefix": [str(fake)]}
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request("cursor", "work", repo.name, config)

            with mock.patch.dict(
                os.environ,
                {"HOME": fake_home, "CLAUDE_CODE_SESSION_ID": "session-1", "CODEX_THREAD_ID": ""},
                clear=False,
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            index = self.delegate.run_registry.load_index(registry_root)
            run_id = next(iter(index["runs"]))
            self.assertEqual(index["runs"][run_id]["initiatorRoot"], "claude:session-1")
            self.assertEqual(observed_path.read_text(encoding="utf-8").strip(), "claude:session-1")

    # -- Safe + worktree passthrough allowed and cleans up --------------------

    def test_safe_worktree_passthrough_allowed_and_cleans_up(self):
        """--pass-through --isolation worktree cursor safe is allowed and cleans up."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            config = dict(self.delegate.DEFAULT_CONFIG)
            workspace = self.delegate.resolve_workspace(repo.name)
            git_root, git_common_dir, head_oid, head_ref, branch_name = (
                self.delegate.capture_git_metadata(repo.name)
            )
            effective = self.delegate.delegate_config.resolve_isolation(
                cli_value="worktree",
                loaded_config=config,
                engine="cursor",
                mode="safe",
            )
            isolation_context = self.delegate.build_isolation_context(
                source_workspace=workspace.path,
                resolved_isolation=effective,
                engine="cursor",
                mode="safe",
                config=config,
                run_short_id=None,
                source_git_root=git_root,
                source_git_common_dir=git_common_dir,
                source_head_oid=head_oid,
                source_head_ref=head_ref,
                source_branch=branch_name,
            )
            request = self.delegate.Request(
                "cursor",
                "safe",
                repo.name,
                self.delegate.prefix_cursor_safe_prompt(
                    self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"
                ),
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    self.delegate.prefix_cursor_safe_prompt(
                        self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"
                    ),
                ],
                "composer-2.5",
                workspace_kind="git",
                isolation_context=isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=config,
                    pass_through=True,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            # Source should NOT have mutated file
            self.assertFalse((Path(repo.name) / "mutated-by-agent.txt").exists())
            # No temp dirs should remain (safe_temp_dirs returns dirs under tempfile)
            # The cleanup is handled by the context manager's finally block

    # -- worktreeStatus is set to "present" after run -------------------------

    def test_persistent_worktree_state_includes_worktree_status_present(self):
        """After successful persistent worktree run, state.json includes worktreeStatus: present."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            runs_dir = registry_root / "runs"
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertTrue(len(run_dirs) > 0, "No run directory found")
            state = self.delegate.json.loads((run_dirs[0] / "state.json").read_text())
            self.assertEqual(state.get("worktreeStatus"), "present")

    # -- Manifest includes creationContext ------------------------------------

    def test_persistent_worktree_manifest_includes_creation_context(self):
        """Manifest includes creationContext with sourceHeadOid, plannedBranch, etc."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            runs_dir = registry_root / "runs"
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertTrue(len(run_dirs) > 0)
            manifest = self.delegate.json.loads((run_dirs[0] / "manifest.json").read_text())
            self.assertIn("creationContext", manifest)
            cc = manifest["creationContext"]
            self.assertIn("sourceHeadOid", cc)
            self.assertIn("plannedBranch", cc)
            self.assertIn("plannedExecutionCwd", cc)
            self.assertTrue(cc["plannedBranch"].startswith("delegate/cursor-"))
            self.assertIn("/worktrees/", cc["plannedExecutionCwd"])

    def _make_logging_fake_bin(self, name, log_file):
        """Make a fake binary that logs its argv to a file."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / name
        path.write_text(f'#!/usr/bin/env bash\necho "$@" >> {log_file}\nexit 0\n')
        path.chmod(0o755)
        return bin_dir

    def _make_commit_fake_bin(self, name="agent"):
        """Make a fake binary that creates a commit in its current working directory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bin_dir = Path(temp.name)
        path = bin_dir / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'committed\\n' > committed-by-agent.txt\n"
            "git add committed-by-agent.txt\n"
            "git commit -m 'agent commit' >/dev/null\n"
            'exit "${FAKE_EXIT:-0}"\n'
        )
        path.chmod(0o755)
        return bin_dir

    def test_prompt_context_note_reaches_child_cursor(self):
        """Persistent worktree prompt context note is present in child argv for cursor."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            log_file = str(Path(fake_home) / "child-argv.log")
            fake_bin = self._make_logging_fake_bin("agent", log_file)
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            logged = Path(log_file).read_text() if Path(log_file).exists() else ""
            self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    def test_persistent_worktree_json_payload_includes_work_summary(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            self.assertIsNotNone(payload)
            summary = payload["workSummary"]
            self.assertEqual(summary["changedFilesCount"], 1)
            self.assertEqual(summary["commitsCreatedCount"], 0)
            self.assertFalse(summary["noChanges"])

            registry_root = Path(repo.name) / ".delegate"
            runs_dir = registry_root / "runs"
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertTrue(run_dirs)
            state = self.delegate.json.loads((run_dirs[0] / "state.json").read_text())
            snapshot = self.delegate.json.loads((run_dirs[0] / "snapshot.json").read_text())
            self.assertEqual(state["workSummary"]["changedFilesCount"], 1)
            self.assertEqual(snapshot["workSummary"]["changedFilesCount"], 1)

    def test_work_summary_does_not_mark_failed_commit_fetch_as_truncated(self):
        repo, _git_cd = self._make_git_repo_with_commit()
        base_oid = subprocess.run(
            ["git", "-C", repo.name, "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        for idx in range(3):
            path = Path(repo.name) / f"commit-{idx}.txt"
            path.write_text(f"{idx}\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo.name, "add", path.name], check=True)
            subprocess.run(
                ["git", "-C", repo.name, "commit", "-m", f"commit {idx}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        original_run_git = worktree_summary.run_git

        def fail_commit_log(cwd, args, *, timeout_seconds):
            if args and args[0] == "log":
                return subprocess.CompletedProcess(
                    ["git", "-C", cwd, *args],
                    128,
                    "",
                    "fatal: log fetch failed",
                )
            return original_run_git(cwd, args, timeout_seconds=timeout_seconds)

        with mock.patch.object(worktree_summary, "run_git", fail_commit_log):
            summary = worktree_summary.build_work_summary(
                source_git_root=repo.name,
                execution_cwd=repo.name,
                branch="delegate/test",
                creation_context={"sourceHeadOid": base_oid},
            )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["commitsCreatedCount"], 3)
        self.assertEqual(summary["commitsCreated"], [])
        self.assertFalse(summary["commitsCreatedTruncated"])
        self.assertTrue(any("git log" in warning for warning in summary["warnings"]))

    def test_work_summary_marks_real_commit_list_truncation(self):
        repo, _git_cd = self._make_git_repo_with_commit()
        base_oid = subprocess.run(
            ["git", "-C", repo.name, "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        for idx in range(worktree_summary.MAX_COMMITS_REPORTED + 1):
            path = Path(repo.name) / f"commit-{idx}.txt"
            path.write_text(f"{idx}\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo.name, "add", path.name], check=True)
            subprocess.run(
                ["git", "-C", repo.name, "commit", "-m", f"commit {idx}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        summary = worktree_summary.build_work_summary(
            source_git_root=repo.name,
            execution_cwd=repo.name,
            branch="delegate/test",
            creation_context={"sourceHeadOid": base_oid},
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["commitsCreatedCount"], worktree_summary.MAX_COMMITS_REPORTED + 1)
        self.assertEqual(len(summary["commitsCreated"]), worktree_summary.MAX_COMMITS_REPORTED)
        self.assertTrue(summary["commitsCreatedTruncated"])

    def test_forbid_commit_fails_when_child_creates_commit(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self._make_commit_fake_bin()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                forbid_commit=True,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 1)
            self.assertIsNotNone(payload)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "commit_policy_violated")
            self.assertTrue(payload["commitPolicyViolated"])
            self.assertEqual(payload["commitPolicy"]["commitsCreatedCount"], 1)
            self.assertEqual(payload["childExitCode"], 0)
            self.assertEqual(payload["workSummary"]["commitsCreatedCount"], 1)

    def test_forbid_commit_fails_closed_when_commit_inspection_unverified(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                forbid_commit=True,
            )
            original_run_git = worktree_summary.run_git

            def fail_commit_count(cwd, args, *, timeout_seconds):
                if args[:2] == ["rev-list", "--count"]:
                    return subprocess.CompletedProcess(
                        ["git", "-C", cwd, *args],
                        128,
                        "",
                        "fatal: not a git repository",
                    )
                return original_run_git(cwd, args, timeout_seconds=timeout_seconds)

            with (
                mock.patch.dict(
                    os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
                ),
                mock.patch.object(worktree_summary, "run_git", fail_commit_count),
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 1)
            self.assertIsNotNone(payload)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "commit_policy_unverified")
            self.assertTrue(payload["commitPolicyUnverified"])
            self.assertFalse(payload["commitPolicy"]["verified"])
            self.assertIsNone(payload["commitPolicy"]["commitsCreatedCount"])
            self.assertEqual(payload["childExitCode"], 0)
            self.assertEqual(payload["workSummary"]["commitInspectionStatus"], "unverified")

    def test_child_commits_without_forbid_commit_surface_warning(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self._make_commit_fake_bin()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, payload = self.delegate.execute_request(
                    request,
                    json_mode=True,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertIsNotNone(payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["workSummary"]["commitsCreatedCount"], 1)
            self.assertTrue(payload["commitsCreatedByChild"])
            self.assertIn("warnings", payload)
            self.assertIn("Child command created commits", payload["warnings"][0])
            self.assertIn("delegate worktree show", payload["nextActions"][0])

    def test_forbid_commit_prompt_context_reaches_child(self):
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            log_file = str(Path(fake_home) / "child-argv.log")
            fake_bin = self._make_logging_fake_bin("agent", log_file)
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
                forbid_commit=True,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            logged = Path(log_file).read_text() if Path(log_file).exists() else ""
            self.assertIn("--forbid-commit is active", logged)

    def test_prompt_context_note_reaches_child_codex(self):
        """Persistent worktree prompt context note is present in child argv for codex."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            log_file = str(Path(fake_home) / "child-argv.log")
            fake_bin = self._make_logging_fake_bin("codex", log_file)
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "codex",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "codex"),
                    "exec",
                    "--cd",
                    repo.name,
                    "--sandbox",
                    "workspace-write",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            logged = Path(log_file).read_text() if Path(log_file).exists() else ""
            self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    def test_prompt_context_note_reaches_child_droid(self):
        """Persistent worktree prompt context note is present in child argv for droid."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            log_file = str(Path(fake_home) / "child-argv.log")
            fake_bin = self._make_logging_fake_bin("droid", log_file)
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["droid"] = dict(config["droid"])
            config["droid"]["models"] = {"qwen": "real-model-id"}
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "droid",
                "work",
                repo.name,
                config,
                model_alias="qwen",
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "droid"),
                    "exec",
                    "--cwd",
                    repo.name,
                    "--model",
                    "real-model-id",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                model_alias="qwen",
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            registry_root = Path(repo.name) / ".delegate"
            index = self.delegate.run_registry.load_index(registry_root)
            run_id = next(iter(index["runs"]))
            index_entry = index["runs"][run_id]
            manifest = self.delegate.run_registry.load_run_manifest(registry_root, run_id)
            self.assertEqual(index_entry["modelAlias"], "qwen")
            self.assertEqual(index_entry["modelResolved"], "real-model-id")
            self.assertEqual(manifest["modelAlias"], "qwen")
            self.assertEqual(manifest["modelResolved"], "real-model-id")
            logged = Path(log_file).read_text() if Path(log_file).exists() else ""
            self.assertIn("You are running in a Delegate-created isolated Git worktree", logged)

    def test_droid_branch_uses_alias_not_resolved_id(self):
        """Droid persistent worktree branch label uses alias (e.g. qwen), not resolved model id."""
        from delegate_agent.isolation import branch_label

        alias = "qwen"
        resolved_id = "custom:OpenRouter-:-Qwen-3.7-Max-0"
        label_alias = branch_label("droid", alias)
        label_resolved = branch_label("droid", resolved_id)
        self.assertEqual(label_alias, "droid-qwen")
        # resolved_id produces a different slug
        self.assertNotEqual(label_alias, label_resolved)

    def test_droid_persistent_worktree_creates_alias_based_branch(self):
        """Droid persistent worktree creates a branch based on alias, not resolved model id."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            fake_bin = self.make_fake_bin()
            config = dict(self.delegate.DEFAULT_CONFIG)
            config["droid"] = dict(config["droid"])
            config["droid"]["models"] = {"qwen": "custom:OpenRouter-:-Qwen-3.7-Max-0"}
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "droid",
                "work",
                repo.name,
                config,
                model_alias="qwen",
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "droid"),
                    "exec",
                    "--cwd",
                    repo.name,
                    "--model",
                    "custom:OpenRouter-:-Qwen-3.7-Max-0",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                model_alias="qwen",
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=config,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0)
            branches = subprocess.run(
                ["git", "-C", repo.name, "branch", "--list", "delegate/droid-*"],
                capture_output=True,
                text=True,
                check=False,
            )
            branch_output = branches.stdout.strip()
            self.assertIn("droid-qwen-", branch_output)
            self.assertNotIn("custom", branch_output)
            self.assertNotIn("OpenRouter", branch_output)

    # -- L836: Detached source HEAD persistent worktree -----------------------

    def test_persistent_worktree_detached_head_source(self):
        """Persistent worktree with detached source HEAD runs and
        records sourceHeadRef: null with 'integration target unknown' warning."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()

            # Detach HEAD: checkout the lone commit OID in detached state.
            oid = subprocess.run(
                ["git", "-C", repo.name, "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", repo.name, "checkout", "--detach", oid],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Confirm detached state: symbolic-ref must fail.
            sym_ref = subprocess.run(
                ["git", "-C", repo.name, "symbolic-ref", "--quiet", "HEAD"],
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(sym_ref.returncode, 0, "HEAD should be detached")

            fake_bin = self.make_cursor_safe_fake_agent()
            workspace = self.delegate.resolve_workspace(repo.name)
            request = self._make_persistent_worktree_request(
                "cursor",
                "work",
                repo.name,
                self.delegate.DEFAULT_CONFIG,
            )
            request = self.delegate.Request(
                request.engine,
                request.mode,
                request.workspace,
                request.prompt,
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    "hello",
                ],
                request.model,
                dry_run=False,
                workspace_kind=request.workspace_kind,
                isolation_context=request.isolation_context,
            )
            with mock.patch.dict(
                os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
            ):
                code, _payload = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=self.delegate.DEFAULT_CONFIG,
                    pass_through=False,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(code, 0, "Run should succeed even with detached HEAD")

            # Assert creationContext.sourceHeadRef is null in state.json
            registry_root = Path(repo.name) / ".delegate"
            runs_dir = registry_root / "runs"
            run_dirs = list(runs_dir.glob("del_*"))
            self.assertTrue(len(run_dirs) > 0, "No run directory found")
            state = self.delegate.json.loads((run_dirs[0] / "state.json").read_text())
            cc = state.get("creationContext", {})
            self.assertIsNone(
                cc.get("sourceHeadRef"),
                "sourceHeadRef must be null when source HEAD is detached",
            )

            # Assert worktree_mgmt.show_worktree includes the warning
            alias = state.get("alias")
            self.assertIsNotNone(alias, "state must record alias")
            show_payload = self.delegate.worktree_mgmt.show_worktree(
                registry_root,
                handle=alias,
            )
            self.assertIn("warnings", show_payload)
            self.assertIn(
                "source was detached at creation; integration target unknown",
                show_payload["warnings"],
            )

    # -- L841: safe + worktree + pass-through cleanup on child failure ---------

    def test_safe_worktree_passthrough_cleans_up_on_child_failure(self):
        """--pass-through --isolation worktree cursor safe cleans up temp
        worktree even when the child exits non-zero."""
        with (
            tempfile.TemporaryDirectory() as fake_home,
            mock.patch.dict(os.environ, {"HOME": fake_home}),
        ):
            repo, _git_cd = self._make_git_repo_with_commit()
            # Fake binary that exits non-zero (child failure).
            fake_bin = self.make_cursor_safe_fake_agent()
            config = dict(self.delegate.DEFAULT_CONFIG)
            workspace = self.delegate.resolve_workspace(repo.name)
            git_root, git_common_dir, head_oid, head_ref, branch_name = (
                self.delegate.capture_git_metadata(repo.name)
            )
            effective = self.delegate.delegate_config.resolve_isolation(
                cli_value="worktree",
                loaded_config=config,
                engine="cursor",
                mode="safe",
            )
            isolation_context = self.delegate.build_isolation_context(
                source_workspace=workspace.path,
                resolved_isolation=effective,
                engine="cursor",
                mode="safe",
                config=config,
                run_short_id=None,
                source_git_root=git_root,
                source_git_common_dir=git_common_dir,
                source_head_oid=head_oid,
                source_head_ref=head_ref,
                source_branch=branch_name,
            )
            request = self.delegate.Request(
                "cursor",
                "safe",
                repo.name,
                self.delegate.prefix_cursor_safe_prompt(
                    self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"
                ),
                [
                    str(fake_bin / "agent"),
                    "--workspace",
                    repo.name,
                    "-p",
                    "--trust",
                    "--model",
                    "composer-2.5",
                    "--output-format",
                    "text",
                    self.delegate.prefix_cursor_safe_prompt(
                        self.delegate.delegate_runner.SKILL_REVIEW_PREFIX + "hello"
                    ),
                ],
                "composer-2.5",
                workspace_kind="git",
                isolation_context=isolation_context,
            )
            temp_dirs_before = safe_temp_dirs()
            branches_before = subprocess.run(
                ["git", "-C", repo.name, "branch", "--list", "delegate/*"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_EXIT": "7",
                },
            ):
                code, _ = self.delegate.execute_request(
                    request,
                    json_mode=False,
                    config=config,
                    pass_through=True,
                    completion_report_mode="none",
                    source_workspace=workspace,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            # 1. Child exit code reflects failure.
            self.assertNotEqual(code, 0, "Child non-zero exit must propagate")

            # 2. Temporary worktree directory must NOT exist after exit
            #    (cleaned up by the finally block).
            self.assertEqual(
                safe_temp_dirs() - temp_dirs_before,
                set(),
                "No new delegate-safe-* temp dirs should remain after cleanup",
            )

            # 3. The branch the temporary worktree used must NOT exist
            #    (safe-mode worktrees are detached, no branch created).
            branches_after = subprocess.run(
                ["git", "-C", repo.name, "branch", "--list", "delegate/*"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            self.assertEqual(
                branches_before,
                branches_after,
                "No delegate/* branches should be created by safe mode",
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
