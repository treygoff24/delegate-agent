"""Workspace spec: worktree base ref, recorded env, setup step, and the lease.

Launch-level behavior runs the real CLI against a fake cursor harness; resume,
prune, and registry behavior uses seeded registry records like their neighbors.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry as registry_api
from delegate_agent import wait_cancel_commands as wait_cancel_api
from delegate_agent import workspace_spec
from delegate_agent import worktree_gc as worktree_gc_api
from delegate_agent.workflows import registry as workflow_registry
from delegate_agent.workflows import runtime as workflow_runtime
from tests import test_resume_attachment as resume_tests
from tests import test_wave4_launch_features as wave4_tests
from tests.execution_test_base import ExecutionTestBase
from tests.worktree_mgmt_test_base import WorktreeMgmtTestBase, git

RESULT_EVENT = 'printf \'{"type":"result","result":"Status: completed. ok"}\\n\'\n'
CLI_PATH = Path(__file__).resolve().parents[1] / "bin" / "delegate.py"


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])  # fixed argv, reaped at once
    process.wait()
    return process.pid


class WorkspaceSpecLaunchTests(ExecutionTestBase):
    write_executable = wave4_tests.Wave4LaunchFeatureTests.write_executable
    write_config = wave4_tests.Wave4LaunchFeatureTests.write_config
    config_with_cursor = wave4_tests.Wave4LaunchFeatureTests.config_with_cursor

    def _repo_with_side_branch(self) -> str:
        repo, _ = self._make_git_repo_with_commit()
        path = repo.name
        git("checkout", "-q", "-b", "spine", cwd=path)
        Path(path, "spine.txt").write_text("spine\n", encoding="utf-8")
        git("add", "spine.txt", cwd=path)
        git("commit", "-q", "-m", "spine", cwd=path)
        git("checkout", "-q", "-", cwd=path)
        return path

    def _cli(
        self, repo: str, fake_home: str, agent: Path, *args: str, keep_worktree: bool = False
    ) -> tuple[int, dict, str]:
        config = self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
        if keep_worktree:
            config["worktrees"]["retireWorktreeOnCompletion"] = False
        config_path = self.write_config(config)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config_path)}):
            code = self.delegate.main(
                ["--cwd", repo, "--json", *args], stdout=stdout, stderr=stderr
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def _launch(
        self,
        repo: str,
        fake_home: str,
        agent: Path,
        *args: str,
        keep_worktree: bool = False,
    ) -> tuple[int, dict, str]:
        config = self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
        if keep_worktree:
            config["worktrees"]["retireWorktreeOnCompletion"] = False
        config_path = self.write_config(config)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config_path)}):
            code = self.delegate.main(
                ["--cwd", repo, "--json", "--isolation", "worktree", "cursor", "work", *args],
                stdout=stdout,
                stderr=stderr,
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def _repo_behind_a_recorded_base(self) -> tuple[str, str]:
        """A repo whose HEAD is one commit ahead of the returned base oid.

        `--base <that oid>` is the shape the review found: the lane's creation
        base is an ancestor of the source checkout's HEAD by construction.
        """
        repo, _ = self._make_git_repo_with_commit()
        path = repo.name
        base_oid = git("rev-parse", "HEAD", cwd=path).stdout.strip()
        Path(path, "second.txt").write_text("second\n", encoding="utf-8")
        git("add", "second.txt", cwd=path)
        git("commit", "-q", "-m", "second", cwd=path)
        return path, base_oid

    def _drift_warnings(self, warnings: list[str]) -> list[str]:
        return [
            warning
            for warning in warnings
            if "landed on the source checkout after dispatch" in warning
            or "behind the source branch" in warning
        ]

    def test_a_based_lane_measures_drift_from_dispatch_not_from_its_base(self):
        """`--base <older commit>` must not warn about drift it was cut with.

        The behind-count was measured against the source checkout's HEAD, so a
        lane cut from an ancestor warned on every completion although nothing
        landed after dispatch; the dispatch point is the only honest reference.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            repo, base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--base", base_oid, "do the task"
            )
            self.assertEqual(code, 0, stderr)
            summary = payload["workSummary"]
            self.assertEqual(summary["sourceDrift"]["commits"], 0)
            self.assertEqual(summary["baseCommit"], base_oid)
            # The pre-fix signal was this number, and it is nonzero here.
            self.assertEqual(summary["branchAheadOfSource"]["behind"], 1)
            self.assertEqual(self._drift_warnings(payload.get("warnings") or []), [])

    def test_a_commit_landed_on_the_checkout_after_dispatch_warns_once(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable(
                "agent",
                'git -C "$DELEGATE_SOURCE_ROOT" commit -q --allow-empty '
                "-m 'landed after dispatch'\n" + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--base", base_oid, "do the task"
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["workSummary"]["sourceDrift"]["commits"], 1)
            drift = self._drift_warnings(payload.get("warnings") or [])
            self.assertEqual(len(drift), 1, payload.get("warnings"))
            self.assertIn("1 commit(s)", drift[0])
            self.assertIn(base_oid, drift[0])

    def test_a_based_lane_that_drifts_and_commits_keeps_both_warnings(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable(
                "agent",
                "printf 'lane\\n' > lane.txt\n"
                "git add lane.txt\n"
                "git -c user.name='Delegate Test' -c user.email=delegate-test@example.com "
                "commit -q -m 'lane commit'\n"
                'git -C "$DELEGATE_SOURCE_ROOT" commit -q --allow-empty '
                "-m 'landed after dispatch'\n" + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--base", base_oid, "do the task"
            )
            self.assertEqual(code, 0, stderr)
            warnings = payload.get("warnings") or []
            self.assertEqual(len(self._drift_warnings(warnings)), 1, warnings)
            self.assertTrue(
                any("created commits" in warning for warning in warnings),
                f"the commit warning must survive alongside the drift warning: {warnings}",
            )

    def test_a_based_lane_suggests_its_base_not_the_checkout_branch(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            agent = self.write_executable(
                "agent",
                "printf 'lane\\n' > lane.txt\n"
                "git add lane.txt\n"
                "git -c user.name='Delegate Test' -c user.email=delegate-test@example.com "
                "commit -q -m 'lane commit'\n" + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--base", "spine", "do the task", keep_worktree=True
            )
            self.assertEqual(code, 0, stderr)
            alias = payload["alias"]
            code, shown, err = self._cli(
                repo, fake_home, agent, "worktree", "show", alias, keep_worktree=True
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(shown["worktreeStatus"], "present", shown.get("warnings"))
            commands = shown["suggestedCommands"]
            self.assertIsNone(commands["mergeIntoSource"])
            merged = commands["mergeIntoBaseBranch"] or ""
            self.assertIn("merge --no-ff", merged)
            self.assertIn("spine", merged)
            self.assertIsNotNone(commands["reviewDiffVsCreationBase"])
            self.assertIsNotNone(commands["cherryPickRange"])

    def test_a_lane_that_merged_the_new_source_commits_is_not_warned_about_them(self):
        """The drift count is work the child never saw, not work it lacks.

        An ordinary lane (no `--base`) whose child picks up commits that landed
        after dispatch saw that work; counting the dispatch range alone told the
        reader it never saw it.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
            agent = self.write_executable(
                "agent",
                f"{self._land_source_commits(3)}\ngit merge --ff-only {branch}\n" + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(repo, fake_home, agent, "do the task")
            self.assertEqual(code, 0, stderr)
            summary = payload["workSummary"]
            self.assertEqual(summary["headCommit"], summary["sourceHead"])
            self.assertEqual(summary["sourceDrift"]["commits"], 0)
            self.assertEqual(self._drift_warnings(payload.get("warnings") or []), [])

    def test_a_lane_that_did_not_merge_them_reports_the_count(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            agent = self.write_executable(
                "agent", self._land_source_commits(3) + "\n" + RESULT_EVENT
            )
            code, payload, stderr = self._launch(repo, fake_home, agent, "do the task")
            self.assertEqual(code, 0, stderr)
            summary = payload["workSummary"]
            self.assertNotEqual(summary["headCommit"], summary["sourceHead"])
            self.assertEqual(summary["sourceDrift"]["commits"], 3)
            drift = self._drift_warnings(payload.get("warnings") or [])
            self.assertEqual(len(drift), 1, payload.get("warnings"))
            self.assertIn("3 commit(s)", drift[0])
            self.assertIn("behind the source branch", drift[0])

    @staticmethod
    def _land_source_commits(count: int) -> str:
        """Shell lines for the child: commits that land on the source checkout."""
        lines: list[str] = []
        for index in range(count):
            name = f"landed-{index}.txt"
            lines += [
                f'printf "landed\\n" > "$DELEGATE_SOURCE_ROOT/{name}"',
                f'git -C "$DELEGATE_SOURCE_ROOT" add {name}',
                'git -C "$DELEGATE_SOURCE_ROOT" -c user.name="Delegate Test" '
                "-c user.email=delegate-test@example.com commit -q "
                f'-m "landed after dispatch {index}"',
            ]
        return "\n".join(lines)

    def test_worktree_show_labels_a_based_lane_with_its_base_ref(self):
        """The creation line pairs a ref with the oid that ref names.

        `sourceHeadRef` is the checkout's branch and `sourceHeadOid` is the
        creation base under `--base`, so pairing them read 'created from
        master@<spine oid>' for a lane that never came from master.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--base", "spine", "do the task", keep_worktree=True
            )
            self.assertEqual(code, 0, stderr)
            config = self.write_config(
                self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
            )
            stdout, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config)}):
                code = self.delegate.main(
                    ["--cwd", repo, "worktree", "show", payload["alias"]],
                    stdout=stdout,
                    stderr=err,
                )
            self.assertEqual(code, 0, err.getvalue())
            spine_oid = git("rev-parse", "spine", cwd=repo).stdout.strip()[:7]
            self.assertIn(
                f"created from spine@{spine_oid}; source now at master@", stdout.getvalue()
            )

    def test_setup_failure_output_masks_recorded_env_values(self):
        """`set -x` prints the expanded token, and the tail goes into records."""
        secret = "supersecret-npm-token-1234567890"
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--env",
                f"NPM_TOKEN={secret}",
                "--setup",
                'set -x; printf "%s\\n" "$NPM_TOKEN"; exit 1',
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "workspace_setup_failed")
            self.assertNotIn(secret, json.dumps(payload))
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            state_text = (run_path / "state.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, state_text)
            self.assertIn("***", state_text)

        # A value that straddles the 2,000-character tail boundary used to
        # survive as its own suffix: the tail's first characters are the end of
        # the token and do not match the whole value, so masking the *cut* tail
        # could not see it. The value is placed so the boundary falls inside it.
        padding = workspace_spec.SETUP_TAIL_CHARS - len(secret) // 2
        straddle = len(secret) + padding - workspace_spec.SETUP_TAIL_CHARS
        self.assertTrue(0 < straddle < len(secret), f"not a straddle: {straddle}")
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--env",
                f"NPM_TOKEN={secret}",
                "--setup",
                f'printf "%s" "$NPM_TOKEN"; printf "%*s" {padding} ""; exit 1',
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "workspace_setup_failed")
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            state_text = (run_path / "state.json").read_text(encoding="utf-8")
            self.assertIn("--- setup output (tail) ---", state_text)
            self.assertNotIn(secret, state_text)
            for start in range(len(secret) - 7):
                fragment = secret[start : start + 8]
                self.assertNotIn(
                    fragment,
                    state_text,
                    f"a straddling value left {fragment!r} in the recorded tail",
                )

    def test_setup_output_masks_a_value_inside_the_old_read_window(self):
        """A tail read as a window could start inside an occurrence of a value.

        The suffix of a value does not match the whole value, so masking the
        window could not see it; when masking also shrank the window below the
        recorded tail length, the cut reached back to that window's first
        character and recorded the suffix. A log that repeats the value densely
        has both.
        """
        token = "npm_" + "9f3a2b7c4d8e1f60ae5bc2d8f1e0"
        self.assertEqual(len(token), 32)
        repeats = 400
        setup = (
            f'i=0; while [ "$i" -lt {repeats} ]; do printf "%s\\n" "$NPM_TOKEN"; '
            "i=$((i+1)); done; exit 1"
        )
        line_bytes = len(token) + 1
        # The old read window: `(SETUP_TAIL_CHARS + longest value) * 4` bytes of
        # the log's end, decoded, masked and cut back to SETUP_TAIL_CHARS. This
        # log is longer than that window and placed so the window began inside
        # an occurrence of the value rather than at a line boundary.
        window = (workspace_spec.SETUP_TAIL_CHARS + len(token)) * 4
        self.assertGreater(repeats * line_bytes, window)
        offset = (repeats * line_bytes - window) % line_bytes
        self.assertTrue(
            0 < offset < len(token), f"the old window began at a line boundary ({offset})"
        )
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--env",
                f"NPM_TOKEN={token}",
                "--setup",
                setup,
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "workspace_setup_failed")
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            state_text = (run_path / "state.json").read_text(encoding="utf-8")
            self.assertIn("--- setup output (tail) ---", state_text)
            for where, text in (("state.json", state_text), ("the envelope", json.dumps(payload))):
                self.assertNotIn(token, text, where)
                for start in range(len(token) - 7):
                    fragment = token[start : start + 8]
                    self.assertNotIn(fragment, text, f"{where}: a repeated value left {fragment!r}")

    def test_setup_output_masks_a_value_folded_across_lines(self):
        """A value its own writer breaks across lines has no complete occurrence.

        `printf "%s" "$TOKEN" | fold -w 32` (a wrapped `set -x` trace, a
        multi-line ``curl -v`` header) leaves pieces in the log instead, so
        whole-value masking finds nothing to replace and both halves used to
        reach `state.json` and the error envelope.
        """
        folded = "npm_9f3a2b7c4d8e1f60ae5bc2d8f1e0" + "0123456789abcdefghijklmnopqrstuv"
        self.assertEqual(len(folded), 64)
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--env",
                f"NPM_TOKEN={folded}",
                "--setup",
                'printf "%s" "$NPM_TOKEN" | fold -w 32; exit 1',
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "workspace_setup_failed")
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            state_text = (run_path / "state.json").read_text(encoding="utf-8")
            self.assertIn("--- setup output (tail) ---", state_text)
            for where, text in (("state.json", state_text), ("the envelope", json.dumps(payload))):
                for half in (folded[:32], folded[32:]):
                    self.assertNotIn(half, text, f"{where}: a folded value kept {half!r}")
                for start in range(len(folded) - 7):
                    fragment = folded[start : start + 8]
                    self.assertNotIn(fragment, text, f"{where}: a folded value left {fragment!r}")

    def test_env_file_errors_name_the_line_without_echoing_its_text(self):
        """An env file line may be key material: the error may not repeat it."""
        key_material = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCAgB+7cK9"
        padded_line = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCAgB+7cK9/Q=="
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            env_file = Path(fake_home) / "private.env"
            env_file.write_text(
                'PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n' + key_material + '\n"-----\n',
                encoding="utf-8",
            )
            code, payload, _stderr = self._launch(
                repo, fake_home, agent, "--env-file", str(env_file), "do the task"
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "invalid_workspace_env")
            self.assertIn("line 1", payload["message"])
            self.assertIn("multi-line quoted values are not supported", payload["message"])
            self.assertNotIn(key_material, payload["message"])

            env_file.write_text(
                "API_BASE=http://localhost:8080\n" + padded_line + "\n",
                encoding="utf-8",
            )
            code, payload, _stderr = self._launch(
                repo, fake_home, agent, "--env-file", str(env_file), "do the task"
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "invalid_workspace_env")
            self.assertIn("line 2", payload["message"])
            self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCAgB", payload["message"])

    def test_an_env_token_without_an_equals_sign_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            secret = "sk-live-0123456789abcdef"
            code, payload, _stderr = self._launch(repo, fake_home, agent, "--env", secret, "task")
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "invalid_workspace_env")
            self.assertIn("NAME=VALUE", payload["message"])
            self.assertNotIn(secret, payload["message"])

    def test_kimi_code_home_is_reserved(self):
        """Kimi's home moves the harness's config, credentials, and sessions."""
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo, fake_home, agent, "--env", "KIMI_CODE_HOME=/tmp/kimi-home", "task"
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "invalid_workspace_env")
            self.assertIn("KIMI_CODE_HOME", payload["message"])

    def test_worktree_is_cut_from_base_and_setup_runs_before_the_child_with_env(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            probe = Path(fake_home) / "probe.txt"
            agent = self.write_executable(
                "agent",
                f"[ -d .git ] || [ -f .git ] || exit 0; {{ test -f spine.txt && echo base=spine; echo setup=$(cat setup-marker); "
                f'echo "env=$SPEC_VAR"; }} > "{probe}"\n' + RESULT_EVENT,
            )
            code, payload, stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--base",
                "spine",
                "--env",
                "SPEC_VAR=from-launch",
                "--setup",
                'printf "%s" "$SPEC_VAR" > setup-marker',
                "do the task",
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                probe.read_text(encoding="utf-8").split(),
                ["base=spine", "setup=from-launch", "env=from-launch"],
            )
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
            spine_oid = git("rev-parse", "spine", cwd=repo).stdout.strip()
            spec = manifest["workspaceSpec"]
            self.assertEqual(spec["base"], "spine")
            self.assertEqual(spec["baseOid"], spine_oid)
            self.assertEqual(spec["envKeys"], ["SPEC_VAR"])
            self.assertEqual(spec["setupResult"]["exitCode"], 0)
            self.assertEqual(manifest["creationContext"]["sourceHeadOid"], spine_oid)
            # Values live only in the private env record, never the manifest.
            self.assertNotIn("from-launch", json.dumps(manifest))
            self.assertEqual(workspace_spec.read_run_env(run_path), {"SPEC_VAR": "from-launch"})
            self.assertEqual((run_path / "workspace-env.json").stat().st_mode & 0o777, 0o600)

    def test_a_quote_in_a_cli_env_value_is_literal_data(self):
        """The unclosed-quote refusal belongs to the env *file* reader only.

        A `--env` token is literal: the shell already resolved its quoting, so a
        value with one quote character is data (`DB_PASS="x9k`), and refusing it
        turned an accepted launch into a usage error.
        """
        with tempfile.TemporaryDirectory() as fake_home:
            repo, _base_oid = self._repo_behind_a_recorded_base()
            agent = self.write_executable("agent", RESULT_EVENT)
            code, payload, stderr = self._launch(
                repo, fake_home, agent, "--env", 'DB_PASS="x9k', "do the task"
            )
            self.assertEqual(code, 0, stderr)
            run_path = registry_api.run_directory(Path(repo) / ".delegate", payload["runId"])
            self.assertEqual(workspace_spec.read_run_env(run_path), {"DB_PASS": '"x9k'})
            self.assertEqual(
                workspace_spec.parse_env_assignment('DB_PASS="x9k'), ("DB_PASS", '"x9k')
            )

    def test_setup_failure_is_typed_and_launches_no_child(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            marker = Path(fake_home) / "child-ran"
            agent = self.write_executable("agent", f'touch "{marker}"\n' + RESULT_EVENT)
            code, payload, _stderr = self._launch(
                repo,
                fake_home,
                agent,
                "--setup",
                "echo installing; exit 7",
                "do the task",
            )
            self.assertNotEqual(code, 0)
            self.assertFalse(marker.exists(), "no child may launch after a failed setup")
            self.assertEqual(payload["error"], "workspace_setup_failed")
            self.assertEqual(payload["failureKind"], "workspace_setup")
            self.assertEqual(payload["workspaceSetup"]["exitCode"], 7)
            state = registry_api.load_run_state(Path(repo) / ".delegate", payload["runId"])
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failureReason"], "workspace_setup_failed")
            self.assertEqual(state["failureKind"], "workspace_setup")
            log = Path(payload["workspaceSetup"]["logPath"])
            self.assertIn("installing", log.read_text(encoding="utf-8"))

    def test_spec_flags_are_refused_outside_worktree_work_runs(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo = self._repo_with_side_branch()
            agent = self.write_executable("agent", RESULT_EVENT)
            config = self.write_config(self.config_with_cursor(agent))
            cases = (
                (["cursor", "safe", "--env", "A=1", "x"], "invalid_option_combination"),
                (["cursor", "work", "--base", "spine", "x"], "invalid_option_combination"),
                (["cursor", "work", "--env", "DELEGATE_RUN_ID=x", "x"], "invalid_workspace_env"),
                (["cursor", "work", "--env", "A=1", "--env", "A=2", "x"], "invalid_workspace_env"),
            )
            for argv, error in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    with mock.patch.dict(
                        os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config)}
                    ):
                        code = self.delegate.main(
                            ["--cwd", repo, "--json", *argv], stdout=stdout, stderr=io.StringIO()
                        )
                    self.assertNotEqual(code, 0)
                    self.assertEqual(json.loads(stdout.getvalue())["error"], error)


class WorkspaceEnvResumeTests(WorktreeMgmtTestBase):
    _owner = resume_tests.ResumeAttachmentTests._owner
    _source_prompt = resume_tests.ResumeAttachmentTests._source_prompt
    _resume = resume_tests.ResumeAttachmentTests._resume

    def test_resume_reapplies_recorded_env_even_when_the_resuming_shell_differs(self):
        _repo, repo_path = self._make_repo()
        with (
            tempfile.TemporaryDirectory() as fake_home,
            tempfile.TemporaryDirectory() as bin_dir,
        ):
            agent = Path(bin_dir) / "agent"
            agent.write_text(
                '#!/bin/sh\nprintf "%s\\n" "${SPEC_VAR:-unset}" > "$SPEC_PROBE"\n' + RESULT_EVENT,
                encoding="utf-8",
            )
            agent.chmod(0o755)
            probe = Path(fake_home) / "probe.txt"
            owner_id, owner_alias, _worktree, _branch = self._owner(repo_path, fake_home)
            owner_path = registry_api.run_directory(self._registry_root(repo_path), owner_id)
            workspace_spec.write_run_env(owner_path, {"SPEC_VAR": "from-launch"})
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
                    "SPEC_VAR": "from-resuming-shell",
                    "SPEC_PROBE": str(probe),
                },
            ):
                code, payload, stderr = self._resume(repo_path, fake_home, owner_alias, "continue")
            self.assertEqual(code, 0, stderr)
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "from-launch")
            child_path = registry_api.run_directory(
                self._registry_root(repo_path), payload["runId"]
            )
            # The attached run records the env too, so a resume of it replays it.
            self.assertEqual(workspace_spec.read_run_env(child_path), {"SPEC_VAR": "from-launch"})

    def test_resume_replays_env_of_a_run_that_failed_in_setup(self):
        """A setup-failed run is resumable, so its env must already be on disk.

        workspace-env.json was written only once the child launch proceeded, so
        `--env FOO=bar --setup false` left the keys on the manifest and no
        values anywhere: resume re-entered the worktree without FOO.
        """
        _repo, repo_path = self._make_repo()
        with (
            tempfile.TemporaryDirectory() as fake_home,
            tempfile.TemporaryDirectory() as bin_dir,
        ):
            agent = Path(bin_dir) / "agent"
            agent.write_text(
                '#!/bin/sh\nprintf "%s\\n" "${FOO:-unset}" > "$FAKE_PROBE"\n' + RESULT_EVENT,
                encoding="utf-8",
            )
            agent.chmod(0o755)
            probe = Path(fake_home) / "probe.txt"
            path_env = bin_dir + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path_env}):
                code, out, _err = self._run_cli(
                    [
                        "--cwd",
                        repo_path,
                        "--json",
                        "--isolation",
                        "worktree",
                        "cursor",
                        "work",
                        "--env",
                        "FOO=bar",
                        "--setup",
                        "false",
                        "do the task",
                    ],
                    home=fake_home,
                )
            self.assertNotEqual(code, 0, out)
            payload = json.loads(out)
            self.assertEqual(payload["error"], "workspace_setup_failed")
            owner_path = registry_api.run_directory(
                self._registry_root(repo_path), payload["runId"]
            )
            self.assertEqual(workspace_spec.read_run_env(owner_path), {"FOO": "bar"})
            with mock.patch.dict(os.environ, {"PATH": path_env, "FAKE_PROBE": str(probe)}):
                code, payload, stderr = self._resume(
                    repo_path, fake_home, payload["alias"], "continue"
                )
            self.assertEqual(code, 0, stderr)
            # The setup command is creation-only and is not re-run; the recorded
            # env is what the resuming shell may not supply.
            self.assertEqual(probe.read_text(encoding="utf-8").strip(), "bar")


class SetupWindowControlTests(ExecutionTestBase):
    """Setup runs unbounded, so cancel and termination must reach it.

    Setup publishes no pid, so `delegate cancel` refused the record
    (missing_pid) and a terminated or interrupted launcher left the setup
    session running inside a worktree the seal path may later reap.
    """

    write_executable = wave4_tests.Wave4LaunchFeatureTests.write_executable
    write_config = wave4_tests.Wave4LaunchFeatureTests.write_config
    config_with_cursor = wave4_tests.Wave4LaunchFeatureTests.config_with_cursor

    def setUp(self):
        super().setUp()
        self._pgids: list[int] = []

    def _spawn_launcher(
        self, repo: str, fake_home: str, agent: Path, setup: str
    ) -> subprocess.Popen[bytes]:
        config = self.write_config(
            self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
        )
        env = {**os.environ, "HOME": fake_home, "DELEGATE_CONFIG": str(config)}
        process = subprocess.Popen(
            [
                sys.executable,
                str(CLI_PATH),
                "--cwd",
                repo,
                "--json",
                "--isolation",
                "worktree",
                "cursor",
                "work",
                "--setup",
                setup,
                "do the task",
            ],
            cwd=repo,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_launcher, process)
        return process

    def _stop_launcher(self, process: subprocess.Popen[bytes]) -> None:
        """Take the launcher and its setup group down, whatever the test did.

        SIGTERM first so the launcher kills its own setup group on the way out;
        SIGKILL only if it does not exit, and the recorded group is signalled
        directly so no setup process can outlive the test.
        """
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for pgid in self._pgids:
            if self._group_alive(pgid):
                with contextlib.suppress(OSError):
                    os.killpg(pgid, signal.SIGKILL)

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        return wait_cancel_api._signal_target_alive(pgid, process_group=True)

    def _await_group_gone(self, pgid: int, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._group_alive(pgid):
                return
            time.sleep(0.1)
        self.fail(f"setup process group {pgid} is still running")

    def _await_setup_pgid(self, repo: str) -> tuple[str, int]:
        """The first run in this repo that published its setup group."""
        root = Path(repo) / ".delegate"
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            index = registry_api.load_index(root)
            for run_id, _entry in registry_api.index_run_entries(index):
                state = registry_api.load_run_state_or_none(root, run_id)
                if isinstance(state, dict) and isinstance(state.get("setupPgid"), int):
                    pgid = state["setupPgid"]
                    self._pgids.append(pgid)
                    return run_id, pgid
            time.sleep(0.1)
        self.fail("the launcher never published a setupPgid")

    def test_cancel_stops_a_setup_that_is_still_running(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            marker = Path(fake_home) / "child-ran"
            agent = self.write_executable("agent", f'touch "{marker}"\n' + RESULT_EVENT)
            launcher = self._spawn_launcher(repo, fake_home, agent, "sleep 300")
            run_id, pgid = self._await_setup_pgid(repo)
            code, out, err = self._run_cancel(repo, fake_home, agent, run_id)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["runs"][0]["status"], "cancelled")
            self._await_group_gone(pgid)
            self.assertEqual(launcher.wait(timeout=30), 1)
            state = registry_api.load_run_state_or_none(Path(repo) / ".delegate", run_id)
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(state["failureReason"], "cancelled_by_user")
            self.assertNotIn("setupPgid", state)
            self.assertFalse(marker.exists(), "cancel during setup must not launch the child")

    def test_a_terminated_launcher_stops_its_setup_group(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            agent = self.write_executable("agent", RESULT_EVENT)
            launcher = self._spawn_launcher(repo, fake_home, agent, "sleep 300")
            run_id, pgid = self._await_setup_pgid(repo)
            launcher.send_signal(signal.SIGTERM)
            # The launcher dies from SIGTERM (default disposition is restored
            # after the setup group is down) instead of orphaning it.
            self.assertEqual(launcher.wait(timeout=30), -signal.SIGTERM)
            self._await_group_gone(pgid)
            state = registry_api.load_run_state_or_none(Path(repo) / ".delegate", run_id)
            self.assertEqual(state["status"], "creating_isolation")
            self.assertNotIn("setupPgid", state)

    def test_an_interrupted_launcher_stops_its_setup_group(self):
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            agent = self.write_executable("agent", RESULT_EVENT)
            launcher = self._spawn_launcher(repo, fake_home, agent, "sleep 300")
            _run_id, pgid = self._await_setup_pgid(repo)
            launcher.send_signal(signal.SIGINT)
            launcher.wait(timeout=30)
            self._await_group_gone(pgid)

    def test_a_setup_grandchild_that_ignores_term_is_killed(self):
        """The group leader is not the whole setup: `--setup 'npm ci && npm run
        build'` leads with `/bin/sh`, which dies at once on TERM while npm keeps
        writing into the worktree the launcher is leaving behind."""
        with tempfile.TemporaryDirectory() as fake_home:
            repo_dir, _ = self._make_git_repo_with_commit()
            repo = repo_dir.name
            agent = self.write_executable("agent", RESULT_EVENT)
            launcher = self._spawn_launcher(
                repo, fake_home, agent, "sh -c 'trap \"\" TERM; sleep 30' & wait"
            )
            _run_id, pgid = self._await_setup_pgid(repo)
            launcher.send_signal(signal.SIGTERM)
            self.assertEqual(launcher.wait(timeout=30), -signal.SIGTERM)
            # The grandchild ignores TERM, so only the escalation after the
            # grace window can take it down.
            self._await_group_gone(pgid, timeout=workspace_spec.SETUP_KILL_GRACE_SECONDS + 5.0)

    def _run_cancel(
        self, repo: str, fake_home: str, agent: Path, target: str
    ) -> tuple[int, str, str]:
        config = self.write_config(
            self.config_with_cursor(agent, data_home=str(Path(fake_home) / "worktrees"))
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": fake_home, "DELEGATE_CONFIG": str(config)}):
            code = self.delegate.main(
                ["--cwd", repo, "--json", "cancel", target], stdout=stdout, stderr=stderr
            )
        return code, stdout.getvalue(), stderr.getvalue()


class SetupProcessUnitTests(unittest.TestCase):
    """`run_setup`'s own contract: the SIGTERM windows it must not swallow."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name

    def _run_setup(self, command: str, **kwargs: object) -> workspace_spec.SetupResult:
        return workspace_spec.run_setup(
            command,
            cwd=self.cwd,
            env=dict(os.environ),
            log_path=Path(self.cwd) / workspace_spec.SETUP_LOG_FILE,
            timeout=30.0,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_short_env_values_receive_whole_value_masking_only(self):
        for length in range(1, 11):
            with self.subTest(length=length):
                value = "ABCDEFGHIJ"[:length]
                env = {"TOKEN": value, "EMPTY": ""}
                text = f"prefix {value} suffix"
                whole_expected = text if length < 4 else "prefix *** suffix"
                fragment_expected = text if length < 8 else "prefix *** suffix"
                self.assertEqual(workspace_spec.mask_recorded_env_values(text, env), whole_expected)
                self.assertEqual(
                    workspace_spec.mask_recorded_env_fragments(text, env), fragment_expected
                )
                result = self._run_setup(f"printf '%s' {shlex.quote(text)}", mask_values=env)
                self.assertEqual(result.output_tail, whole_expected)
                if length > 8:
                    fragment = f"prefix {value[:8]} suffix"
                    self.assertEqual(
                        workspace_spec.mask_recorded_env_values(fragment, env), fragment
                    )
                    result = self._run_setup(
                        f"printf '%s' {shlex.quote(fragment)}", mask_values=env
                    )
                    self.assertEqual(result.output_tail, "prefix *** suffix")

    def test_a_sigterm_during_the_setup_pgid_clear_is_not_swallowed(self):
        """The clear waits on the registry lock, the launcher's longest window.

        `SetupInterrupted` used to raise inside the cleanup's `suppress(Exception)`
        there, so the interruption vanished, `run_setup` returned the setup's
        zero exit, and the launcher went on to launch the child.
        """
        published: list[int | None] = []

        def publish(pgid: int | None) -> None:
            published.append(pgid)
            if pgid is None:
                os.kill(os.getpid(), signal.SIGTERM)

        with self.assertRaises(workspace_spec.SetupInterrupted) as caught:
            self._run_setup("true", publish_pgid=publish)

        self.assertEqual(caught.exception.signum, int(signal.SIGTERM))
        self.assertEqual(len(published), 2, published)
        self.assertIsNone(published[-1], "the group is unrecorded before the signal lands")

    def test_a_sigterm_before_the_setup_process_is_bound_still_stops_it(self):
        """The fork inside `Popen` is the other window: nothing held `process`.

        The handler fired between the fork and the assignment, so the launcher
        died under a setup group it never learned the pid of and orphaned it.
        """
        real_popen = workspace_spec.subprocess.Popen
        groups: list[int] = []

        def popen_then_sigterm(*args: object, **kwargs: object) -> object:
            process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            groups.append(process.pid)
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        with (
            mock.patch.object(workspace_spec.subprocess, "Popen", side_effect=popen_then_sigterm),
            self.assertRaises(workspace_spec.SetupInterrupted),
        ):
            self._run_setup("sleep 30")

        self.assertEqual(len(groups), 1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if workspace_spec._group_liveness(groups[0]) is False:
                break
            time.sleep(0.05)
        self.assertIs(
            workspace_spec._group_liveness(groups[0]),
            False,
            "the setup group outlived the launcher that started it",
        )

    def test_a_second_sigterm_does_not_abort_the_final_kill(self):
        """After the clear's recorded TERM, the kill that takes the group down runs.

        That escalation waits out the same grace `delegate cancel` does, and a
        second TERM landing in it used to raise from inside the wait: the group
        had just been TERMed, the SIGKILL that would have ended a group
        ignoring that TERM never ran, and setup outlived the launcher that was
        told to stop.
        """
        groups: list[int] = []
        real_kill_group = workspace_spec._kill_group
        real_sleep = time.sleep
        repeated_terms: list[int] = []

        def publish(pgid: int | None) -> None:
            if pgid is None:
                # The clear, with the setup's own group still alive behind it:
                # this TERM is what leads to the final kill.
                os.kill(os.getpid(), signal.SIGTERM)
                return
            groups.append(pgid)

        def kill_group_then_a_second_sigterm(process: subprocess.Popen[bytes]) -> None:
            def sleep_then_sigterm(seconds: float) -> None:
                if not repeated_terms:
                    repeated_terms.append(process.pid)
                    os.kill(os.getpid(), signal.SIGTERM)
                real_sleep(seconds)

            # Inject during the grace, after the group has received its TERM.
            with mock.patch.object(workspace_spec.time, "sleep", side_effect=sleep_then_sigterm):
                real_kill_group(process)

        def kill_group_now() -> None:
            for pgid in groups:
                with contextlib.suppress(OSError):
                    os.killpg(pgid, signal.SIGKILL)

        self.addCleanup(kill_group_now)
        with (
            mock.patch.object(
                workspace_spec, "_kill_group", side_effect=kill_group_then_a_second_sigterm
            ),
            self.assertRaises(workspace_spec.SetupInterrupted),
        ):
            # The leader exits at once; the grandchild it leaves behind
            # ignores TERM, so only the escalation can take the group down.
            self._run_setup(
                "sh -c 'trap \"\" TERM; touch ready; exec sleep 30' & "
                "while [ ! -e ready ]; do sleep 0.01; done; exit 1",
                publish_pgid=publish,
            )

        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(repeated_terms, groups)
        deadline = time.monotonic() + workspace_spec.SETUP_KILL_GRACE_SECONDS + 5.0
        while time.monotonic() < deadline:
            if workspace_spec._group_liveness(groups[0]) is False:
                break
            time.sleep(0.05)
        self.assertIs(
            workspace_spec._group_liveness(groups[0]),
            False,
            "a repeated SIGTERM left the group it had already TERMed running",
        )


class WorktreeLeaseTests(WorktreeMgmtTestBase):
    def _leased_tree(self, repo_path: str, fake_home: str, alias: str, **state: object) -> str:
        branch = f"delegate/{alias}"
        wt_path = str(Path(fake_home) / "wt" / alias)
        old = (datetime.now(UTC) - timedelta(days=3)).strftime(registry_api.UTC_TIMESTAMP_FORMAT)
        run_id, _ = self._seed_persistent_run(
            repo_path, alias=alias, branch=branch, execution_cwd=wt_path, last_activity_at=old
        )
        self._create_worktree_at(repo_path, branch, wt_path)
        root = self._registry_root(repo_path)
        state_path = registry_api.run_directory(root, run_id) / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        dead = _dead_pid()
        payload.update({"status": "running", "pid": dead, "pgid": dead, **state})
        registry_api.write_json_atomic(state_path, payload)
        return wt_path

    def test_live_launcher_lease_survives_auto_prune_and_prune_merged_with_dead_pid(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            wt_path = self._leased_tree(
                repo_path, fake_home, "cursor-leased", launcherPid=os.getpid()
            )
            global_config = Path(fake_home) / ".delegate" / "config.json"
            global_config.parent.mkdir(parents=True, exist_ok=True)
            global_config.write_text(
                json.dumps(
                    {"worktrees": {"autoPrune": {"enabled": True, "mergedOlderThanDays": 1}}}
                ),
                encoding="utf-8",
            )
            code, out, err = self._run_cli(
                ["--cwd", repo_path, "--json", "worktree", "list"], home=fake_home
            )
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["summary"]["autoPruneMode"], "attempted")
            self.assertTrue(Path(wt_path).exists(), "auto-prune reaped a leased worktree")

            code, out, err = self._run_cli(
                ["--cwd", repo_path, "--json", "worktree", "prune", "--merged"], home=fake_home
            )
            payload = json.loads(out)
            self.assertTrue(Path(wt_path).exists(), "prune --merged reaped a leased worktree")
            reasons = {entry["alias"]: entry["reason"] for entry in payload["skipped"]}
            self.assertEqual(reasons["cursor-leased"], "worktree_leased")

    def test_lease_ends_with_a_dead_launcher_or_a_terminal_record(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            self._leased_tree(repo_path, fake_home, "cursor-gone", launcherPid=_dead_pid())
            self._leased_tree(
                repo_path, fake_home, "cursor-done", status="succeeded", launcherPid=os.getpid()
            )
            result = worktree_gc_api.prune_worktrees(
                self._registry_root(repo_path), merged=True, dry_run=True
            )
            planned = {entry["alias"] for entry in result["planned"]}
            self.assertEqual(planned, {"cursor-gone", "cursor-done"})

    def test_a_run_whose_setup_is_still_running_keeps_its_worktree(self):
        """`creating_isolation` with a live launcher is leased, like any run.

        The setup window holds the worktree before any child pid exists, so no
        pid-based heuristic can see it: pruning must leave it alone until the
        record turns terminal or its launcher is verifiably gone.
        """
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            wt_path = self._leased_tree(
                repo_path,
                fake_home,
                "cursor-setup",
                status="creating_isolation",
                launcherPid=os.getpid(),
            )
            root = self._registry_root(repo_path)
            for path in sorted((root / "runs").glob("*/state.json")):
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("alias") != "cursor-setup":
                    continue
                # The setup window's own shape: the launcher is alive, the
                # setup group is recorded, and no child pid is.
                payload.pop("pid", None)
                payload.pop("pgid", None)
                payload["setupPgid"] = os.getpid()
                registry_api.write_json_atomic(path, payload)
            result = worktree_gc_api.prune_worktrees(root, merged=True, dry_run=True)
            self.assertEqual({entry["alias"] for entry in result["planned"]}, set())
            reasons = {entry["alias"]: entry["reason"] for entry in result["skipped"]}
            self.assertEqual(reasons["cursor-setup"], "worktree_leased")
            self.assertTrue(Path(wt_path).exists())


class LinkedWorktreeRegistryTests(WorktreeMgmtTestBase):
    def test_run_launched_from_a_linked_worktree_is_listed_from_the_parent_repo(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            linked = str(Path(fake_home) / "lane")
            git("worktree", "add", "-q", "-b", "lane", linked, "HEAD", cwd=repo_path)
            parent_run, _ = self._seed_plain_run(repo_path)
            linked_run, _ = self._seed_plain_run(linked)

            code, out, err = self._run_cli(["--cwd", repo_path, "--json", "runs"], home=fake_home)
            self.assertEqual(code, 0, err)
            runs = {item["runId"]: item for item in json.loads(out)["runs"]}
            self.assertIn(parent_run, runs)
            self.assertIn(linked_run, runs)
            self.assertEqual(runs[linked_run]["registryWorkspace"], linked)
            self.assertNotIn("registryWorkspace", runs[parent_run])

            # The linked worktree's own listing stays its own.
            code, out, _err = self._run_cli(["--cwd", linked, "--json", "runs"], home=fake_home)
            self.assertEqual([item["runId"] for item in json.loads(out)["runs"]], [linked_run])

    def test_a_lane_sharing_the_main_registry_lists_each_run_once(self):
        """One Registry is one row per run, however many worktrees reach it.

        A lane's `.delegate` can be a symlink to the main Registry or a copy of
        it. The symlinked root resolved to the same Registry (and the record
        reader refuses a symlinked registry path component, so it contributed
        nothing); the copy is a readable second root holding the same rows, and
        without the shared-root skip and the per-run de-duplication every run
        appeared twice in the parent's listing.
        """
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            linked = str(Path(fake_home) / "lane")
            git("worktree", "add", "-q", "-b", "lane", linked, "HEAD", cwd=repo_path)
            parent_run, _ = self._seed_plain_run(repo_path)
            os.symlink(
                self._registry_root(repo_path), Path(linked) / ".delegate", target_is_directory=True
            )
            code, out, err = self._run_cli(["--cwd", repo_path, "--json", "runs"], home=fake_home)
            self.assertEqual(code, 0, err)
            self.assertEqual([item["runId"] for item in json.loads(out)["runs"]], [parent_run])

            # A copy of the registry is a distinct root holding the same rows.
            copy = Path(linked) / ".delegate"
            copy.unlink()
            shutil.copytree(self._registry_root(repo_path), copy, symlinks=True)
            code, out, err = self._run_cli(["--cwd", repo_path, "--json", "runs"], home=fake_home)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual([item["runId"] for item in payload["runs"]], [parent_run])
            # The rows are de-duplicated, so the totals must be too: the copy's
            # rows are the same run, and an uncorrected footer read "1 of 2".
            self.assertEqual(payload["total"], 1)

            # 25 runs per root under `--limit 10`: the copy contributes the
            # same run ids, so the page holds 10 rows and the footer must read
            # "10 of 25". Summing the roots' pre-limit counts and subtracting
            # only the duplicates that made it into the page read "10 of 40".
            for number in range(24):
                self._seed_plain_run(repo_path, last_activity_at=f"2026-05-20T12:{number:02d}:00Z")
            shutil.rmtree(copy)
            shutil.copytree(self._registry_root(repo_path), copy, symlinks=True)
            code, out, err = self._run_cli(
                ["--cwd", repo_path, "--json", "runs", "--limit", "10"], home=fake_home
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["total"], 25)
            self.assertEqual(len(payload["runs"]), 10)
            self.assertTrue(payload["truncated"])
            self.assertEqual(payload["runs"][0]["runId"], parent_run)

            code, out, err = self._run_cli(
                ["--cwd", repo_path, "runs", "--limit", "10"], home=fake_home
            )
            self.assertEqual(code, 0, err)
            self.assertIn("showing 10 of 25 runs", out)


class WorkflowWorkspaceSpecTests(WorktreeMgmtTestBase):
    def test_capability_is_advertised(self):
        self.assertEqual(workflow_runtime.WORKFLOW_CAPABILITIES["workspaceSpec"], 1)

    def test_agent_spec_requires_worktree_work_lane_and_valid_values(self):
        spec = workflow_runtime._agent_workspace_spec(
            base="main", env={"A": "1"}, setup="make", mode="work", isolation="worktree"
        )
        self.assertEqual(spec, {"base": "main", "env": {"A": "1"}, "setup": "make"})
        for kwargs in (
            {"mode": "safe", "isolation": "worktree"},
            {"mode": "work", "isolation": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                workflow_runtime._agent_workspace_spec(
                    base=None, env={"A": "1"}, setup=None, **kwargs
                )
        with self.assertRaises(ValueError):
            workflow_runtime._agent_workspace_spec(
                base=None, env={"TMPDIR": "/x"}, setup=None, mode="work", isolation="worktree"
            )

    def test_workflow_launch_env_is_recorded_and_resume_refuses_new_env(self):
        _repo, repo_path = self._make_repo()
        with tempfile.TemporaryDirectory() as fake_home:
            script = Path(fake_home) / "wf.py"
            script.write_text('meta = {"name": "noop"}\nreturn {"ok": True}\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"DELEGATE_WORKFLOW_NO_DAEMON": "1"}):
                code, out, err = self._run_cli(
                    [
                        "--cwd",
                        repo_path,
                        "--json",
                        "workflow",
                        "run",
                        str(script),
                        "--dry-run",
                        "--env",
                        "SPEC_VAR=from-launch",
                    ],
                    home=fake_home,
                )
            self.assertEqual(code, 0, err)
            wf_root = Path(repo_path) / ".delegate" / "workflows" / json.loads(out)["wfId"]
            self.assertEqual(workspace_spec.read_run_env(wf_root), {"SPEC_VAR": "from-launch"})

            code, out, _err = self._run_cli(
                [
                    "--cwd",
                    repo_path,
                    "--json",
                    "workflow",
                    "resume",
                    json.loads(out)["wfId"],
                    "--env",
                    "SPEC_VAR=other",
                ],
                home=fake_home,
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out)["error"], "invalid_option_combination")

    def _workflow_state(
        self,
        workspace: Path,
        *,
        launch_env: dict[str, str] | None = None,
        cli_argv: list[str] | None = None,
        wf_id: str = "wf_333333333333",
    ) -> workflow_runtime.WorkflowState:
        root = workspace / ".delegate" / "workflows" / wf_id
        registry_api.ensure_private_dir(root)
        script = root / workflow_registry.SCRIPT_FILE
        script.write_text("return True\n", encoding="utf-8")
        workflow_registry.write_json(
            root / workflow_registry.STATUS_FILE,
            {
                "wfId": wf_id,
                "status": "created",
                "budget": {"total": None, "spent": 0, "remaining": None},
            },
        )
        return workflow_runtime.WorkflowState(
            wf_id=wf_id,
            workspace=workspace,
            root=root,
            script_path=script,
            config={},
            cli_argv=cli_argv or ["delegate"],
            args=None,
            budget=workflow_runtime.Budget(None),
            launch_env=dict(launch_env or {}),
        )

    def _fake_child_delegate(self, workspace: Path) -> tuple[Path, Path, Path, Path, Path]:
        """A child Delegate stand-in: records its argv's input JSON and its env.

        Returns the fake, the last call's input payload, a log of every call's
        payload (one JSON object per line), and the two env probes.
        """
        captured = workspace / "captured-input.json"
        payload_log = workspace / "captured-inputs.jsonl"
        spec_probe = workspace / "spec-probe.txt"
        git_probe = workspace / "git-probe.txt"
        fake = workspace / "fake-delegate"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--input-json" ]; then\n'
            '    cp "$2" ' + shlex.quote(str(captured)) + "\n"
            '    cat "$2" >> ' + shlex.quote(str(payload_log)) + "\n"
            '    printf "\\n" >> ' + shlex.quote(str(payload_log)) + "\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            f'printf "%s\\n" "${{SPEC_VAR:-unset}}" > {shlex.quote(str(spec_probe))}\n'
            f'printf "%s\\n" "${{GIT_DIR:-unset}}" > {shlex.quote(str(git_probe))}\n'
            'printf \'{"ok": true, "runId": "del_20260924T000000Z_aaaaaa", '
            '"assistantText": "done"}\\n\'\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake, captured, payload_log, spec_probe, git_probe

    def _agent_keys(self, root: Path) -> list[str]:
        return [
            event["workflowAgentKey"]
            for event in workflow_registry.iter_journal(root / workflow_registry.JOURNAL_FILE)
            if event.get("type") == "agent_started"
            and isinstance(event.get("workflowAgentKey"), str)
        ]

    def test_a_worktree_lane_receives_the_launch_env_as_run_input(self):
        """`workflow run --env` reaches the lane as data, never as process env.

        Merging it into the child Delegate process's environment moved that
        process's own config, data-home, and git resolution in every lane mode
        (`--env GIT_DIR=/elsewhere/.git` made child Delegates work against
        another repository). It belongs under `agent(env=)`, where it goes
        through the per-run layer's validation and reaches exactly the children
        per-run `--env` reaches.
        """
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            launch_env = {"GIT_DIR": "/elsewhere/.git", "SPEC_VAR": "from-launch"}
            workspace_spec.write_run_env(
                workspace / ".delegate" / "workflows" / "wf_333333333333", launch_env
            )
            fake, captured, payload_log, spec_probe, git_probe = self._fake_child_delegate(
                workspace
            )
            state = self._workflow_state(workspace, launch_env=launch_env, cli_argv=[str(fake)])
            dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})
            self.assertEqual(dsl.agent("do the task", mode="work", isolation="worktree"), "done")
            self.assertEqual(
                dsl.agent(
                    "do it again",
                    mode="work",
                    isolation="worktree",
                    env={"SPEC_VAR": "from-call"},
                ),
                "done",
            )
            # The child Delegate's own environment is the supervisor's, and the
            # launch env is not in it.
            self.assertEqual(spec_probe.read_text(encoding="utf-8").strip(), "unset")
            self.assertEqual(git_probe.read_text(encoding="utf-8").strip(), "unset")
            payload = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["env"], {"GIT_DIR": "/elsewhere/.git", "SPEC_VAR": "from-call"}
            )
            # Both calls carry it: the first has no env= of its own, and the
            # launch env is a default that stands alone.
            first, second = [
                json.loads(line)
                for line in payload_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(first["env"], launch_env)
            self.assertEqual(second["env"], {"GIT_DIR": "/elsewhere/.git", "SPEC_VAR": "from-call"})

    def test_the_supervisor_reads_the_recorded_launch_env_for_agent_calls(self):
        """`run_supervisor` is what puts the recorded env on the state.

        The lane tests build `WorkflowState(launch_env=...)` themselves, which
        skips the `read_run_env(root)` wiring between `workflow run --env` and an
        `agent()` call: a launch env on disk that never reached the state would
        pass all of them.
        """
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            wf_id = "wf_444444444444"
            root = workspace / ".delegate" / "workflows" / wf_id
            registry_api.ensure_private_dir(root)
            (root / workflow_registry.SCRIPT_FILE).write_text(
                'meta = {"name": "env-lane"}\n'
                'return {"ok": agent("do the task", mode="work", isolation="worktree") == "done"}\n',
                encoding="utf-8",
            )
            workflow_registry.write_json(
                root / workflow_registry.STATUS_FILE,
                {
                    "wfId": wf_id,
                    "status": "created",
                    "workflowKeyVersion": workflow_runtime.WORKFLOW_KEY_VERSION,
                    "budget": {"total": None, "spent": 0, "remaining": None},
                },
            )
            launch_env = {"SPEC_VAR": "from-launch", "GIT_DIR": "/elsewhere/.git"}
            workspace_spec.write_run_env(root, launch_env)
            fake, captured, _payload_log, _spec_probe, _git_probe = self._fake_child_delegate(
                workspace
            )
            with mock.patch.dict(os.environ, {"DELEGATE_WORKFLOW_NO_DAEMON": "1"}):
                code = workflow_runtime.run_supervisor(
                    workspace=workspace,
                    wf_id=wf_id,
                    cli_argv=[str(fake)],
                    config={},
                )
            self.assertEqual(code, 0)
            payload = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual(payload["env"], launch_env)

    def test_a_lane_that_cannot_take_env_keeps_its_spec_less_key(self):
        """The launch env is a default, not a demand: it cannot fail a call.

        A call whose mode/isolation cannot carry workspace env receives nothing
        and keeps the key it would have had in a workflow with no launch env.
        """
        keys: list[str] = []
        captured_payloads: list[dict] = []
        for launch_env in ({}, {"SPEC_VAR": "from-launch"}):
            with tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                fake, captured, _payload_log, spec_probe, _git_probe = self._fake_child_delegate(
                    workspace
                )
                state = self._workflow_state(workspace, launch_env=launch_env, cli_argv=[str(fake)])
                dsl = workflow_runtime.WorkflowDsl(state, {"defaults": {"engine": "codex"}})
                self.assertEqual(dsl.agent("judge it", mode="safe"), "done")
                self.assertEqual(spec_probe.read_text(encoding="utf-8").strip(), "unset")
                payload = json.loads(captured.read_text(encoding="utf-8"))
                captured_payloads.append(payload)
                keys.extend(self._agent_keys(state.root))
        self.assertNotIn("env", captured_payloads[0])
        self.assertNotIn("env", captured_payloads[1])
        self.assertEqual(len(keys), 2, keys)
        self.assertEqual(keys[0], keys[1])
