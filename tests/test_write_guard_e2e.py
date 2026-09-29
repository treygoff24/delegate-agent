"""Work write guard and --forbid-commit through the real CLI with fake engines.

The seam-level proof that a child is really refused writes is in
``tests/test_write_guard_live.py``. This file proves the wiring around it: config
and flags reach the launch, the manifest and dry-run record the plan, Codex keeps
its own sandbox (with the right roots) instead of being wrapped, and
``--forbid-commit`` works in place with the post-exit check behind the hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from delegate_agent import run_registry
from tests import assert_compact_temps_contained, proc_harness
from tests.test_write_guard_live import _USABLE, _WHY_NOT, IDENTITY

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "bin" / "delegate.py"

EVENTS = (
    'printf \'{"type":"message","role":"assistant","content":"done"}\\n\'\n'
    'printf \'{"type":"completion","finalText":"done"}\\n\'\n'
)

DESTRUCTIVE_AGENT = (
    "#!/bin/sh\n"
    'rm -rf "$HOME/.ssh/canary" 2>/dev/null; echo "rm_ssh=$?" > "$GUARD_RESULT_DIR/results"\n'
    'echo x > "$GUARD_SIBLING/f" 2>/dev/null; echo "write_sibling=$?" >> "$GUARD_RESULT_DIR/results"\n'
    'echo x > "$PWD/exec-file"; echo "write_exec=$?" >> "$GUARD_RESULT_DIR/results"\n' + EVENTS
)

COMMITTING_AGENT = (
    "#!/bin/sh\n"
    'echo x > "$PWD/lane-file"\n'
    "git add lane-file >/dev/null 2>&1\n"
    'git commit -q -m "lane" >/dev/null 2>&1; echo "commit=$?" > "$GUARD_RESULT_DIR/results"\n'
    'echo "params=$GIT_CONFIG_PARAMETERS" >> "$GUARD_RESULT_DIR/results"\n' + EVENTS
)

HOOK_BYPASS_AGENT = (
    "#!/bin/sh\n"
    'echo x > "$PWD/lane-file"\n'
    "git add lane-file >/dev/null 2>&1\n"
    'git -c core.hooksPath=/dev/null commit -q -m "lane" >/dev/null 2>&1\n' + EVENTS
)

CODEX_LOGGING = (
    "#!/bin/sh\n"
    'printf "%s\\n" "$@" > "$GUARD_RESULT_DIR/codex-argv"\n'
    'cat > "$GUARD_RESULT_DIR/codex-prompt"\n' + EVENTS
)


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *IDENTITY, *args], text=True, capture_output=True, check=True
    ).stdout.strip()


class GuardE2ETestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.home / "Code" / "repo"
        self.sibling = self.home / "Code" / "sibling"
        self.results = self.root / "results"
        self.bin_dir = self.root / "bin"
        for path in (self.workspace, self.sibling, self.results, self.bin_dir, self.home / ".ssh"):
            path.mkdir(parents=True)
        (self.home / ".ssh" / "canary").write_text("precious", encoding="utf-8")
        run_git(self.workspace, "init", "-q", "-b", "main")
        (self.workspace / "seed.txt").write_text("seed", encoding="utf-8")
        run_git(self.workspace, "add", "seed.txt")
        run_git(self.workspace, "commit", "-q", "-m", "seed")
        self.registry_root = run_registry.delegate_root(self.workspace)
        self.config_path = self.registry_root / "config.json"
        self.addCleanup(self.contain_temps)

    def contain_temps(self):
        assert_compact_temps_contained(
            self.registry_root, producers=[proc_harness.reaped_owned_producers(self.workspace)]
        )

    def write_config(self, extra: dict | None = None) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {
            "cursor": {"argvPrefix": ["agent"], "defaultModel": "composer-2.5"},
            "isolation": {
                "writeGuard": {"macosSeatbelt": True, "onUnavailable": "refuse"},
            },
        }
        for key, value in (extra or {}).items():
            config[key] = {**config.get(key, {}), **value} if isinstance(value, dict) else value
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def write_engine(self, name: str, body: str) -> None:
        path = self.bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def env(self, guard: str = "on") -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            HOME=str(self.home),
            PATH=str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            DELEGATE_CONFIG=str(self.config_path),
            DELEGATE_WRITE_GUARD=guard,
            GUARD_RESULT_DIR=str(self.results),
            GUARD_SIBLING=str(self.sibling),
        )
        for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            env.pop(name, None)
        return env

    def cli(self, *args: str, guard: str = "on") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "--cwd", str(self.workspace), *args],
            text=True,
            capture_output=True,
            env=self.env(guard),
            check=False,
        )

    def latest_run(self) -> tuple[dict, dict]:
        index = run_registry.load_index(self.registry_root)
        run_id = next(iter(index["runs"]))
        run_path = run_registry.run_directory(self.registry_root, run_id)
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        state = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
        return manifest, state

    def result_lines(self) -> dict[str, str]:
        text = (self.results / "results").read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@unittest.skipUnless(_USABLE, _WHY_NOT)
class GuardedRunTests(GuardE2ETestCase):
    def test_work_run_is_guarded_and_the_manifest_records_the_plan(self):
        self.write_config()
        self.write_engine("agent", DESTRUCTIVE_AGENT)
        completed = self.cli("cursor", "work", "tidy up")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        lines = self.result_lines()
        self.assertNotEqual(lines["rm_ssh"], "0")
        self.assertNotEqual(lines["write_sibling"], "0")
        self.assertEqual(lines["write_exec"], "0")
        self.assertEqual((self.home / ".ssh/canary").read_text(encoding="utf-8"), "precious")
        self.assertFalse((self.sibling / "f").exists())
        manifest, _state = self.latest_run()
        guard = manifest["writeGuard"]
        self.assertEqual(guard["status"], "enforced")
        self.assertIn(os.path.realpath(self.home / ".ssh"), guard["protected"])
        self.assertIn(os.path.realpath(self.workspace), [w["path"] for w in guard["writable"]])

    def test_writable_flag_reaches_the_launch(self):
        self.write_config()
        self.write_engine("agent", DESTRUCTIVE_AGENT)
        completed = self.cli("cursor", "work", "--writable", str(self.sibling), "tidy up")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(self.result_lines()["write_sibling"], "0")
        self.assertTrue((self.sibling / "f").exists())
        manifest, _state = self.latest_run()
        reasons = {w["path"]: w["reason"] for w in manifest["writeGuard"]["writable"]}
        self.assertEqual(reasons[os.path.realpath(self.sibling)], "--writable")

    def test_the_guard_can_be_switched_off_for_a_run(self):
        self.write_config()
        self.write_engine("agent", DESTRUCTIVE_AGENT)
        completed = self.cli("cursor", "work", "tidy up", guard="off")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(self.result_lines()["rm_ssh"], "0")
        manifest, _state = self.latest_run()
        self.assertNotIn("writeGuard", manifest)

    def test_dry_run_shows_the_plan_without_launching(self):
        self.write_config()
        completed = self.cli("--json", "dry-run", "cursor", "work", "tidy up")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        guard = payload["writeGuard"]
        self.assertEqual(guard["status"], "planned")
        self.assertEqual(guard["backend"], "seatbelt" if sys.platform == "darwin" else "bwrap")
        self.assertIn(os.path.realpath(self.home / ".ssh"), guard["protected"])

    def test_forbid_commit_in_place_refuses_the_commit_and_verifies_zero_commits(self):
        self.write_config()
        self.write_engine("agent", COMMITTING_AGENT)
        before = run_git(self.workspace, "rev-parse", "HEAD")
        completed = self.cli("--isolation", "none", "cursor", "work", "--forbid-commit", "edit")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        lines = self.result_lines()
        self.assertNotEqual(lines["commit"], "0")
        self.assertIn("core.hooksPath", lines["params"])
        self.assertEqual(run_git(self.workspace, "rev-parse", "HEAD"), before)
        _manifest, state = self.latest_run()
        policy = state["commitPolicy"]
        self.assertTrue(policy["verified"])
        self.assertFalse(policy["violated"])
        self.assertEqual(policy["commitsCreatedCount"], 0)

    def test_a_commit_that_gets_around_the_hooks_fails_the_run_afterwards(self):
        self.write_config()
        self.write_engine("agent", HOOK_BYPASS_AGENT)
        completed = self.cli("--isolation", "none", "cursor", "work", "--forbid-commit", "edit")
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        _manifest, state = self.latest_run()
        self.assertEqual(state["error"], "commit_policy_violated")
        self.assertTrue(state["commitPolicy"]["violated"])
        self.assertEqual(state["commitPolicy"]["commitsCreatedCount"], 1)


class ForbidCommitWithoutGuardTests(GuardE2ETestCase):
    """The hooks need no sandbox: they work with the guard off, on any host."""

    def test_hooks_refuse_the_commit_with_the_guard_off(self):
        self.write_config()
        self.write_engine("agent", COMMITTING_AGENT)
        before = run_git(self.workspace, "rev-parse", "HEAD")
        completed = self.cli(
            "--isolation", "none", "cursor", "work", "--forbid-commit", "edit", guard="off"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertNotEqual(self.result_lines()["commit"], "0")
        self.assertEqual(run_git(self.workspace, "rev-parse", "HEAD"), before)

    def test_forbid_commit_with_isolation_none_is_accepted_by_dry_run(self):
        self.write_config()
        completed = self.cli(
            "--isolation", "none", "--json", "dry-run", "cursor", "work", "--forbid-commit", "edit"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["commitPolicy"], {"forbidCommit": True})
        self.assertEqual(payload["effectiveIsolation"], "none")


class CodexNativeSandboxTests(GuardE2ETestCase):
    """Codex work lanes keep Codex's own sandbox and get the roots they need."""

    def config(self) -> None:
        self.write_config(
            {
                "policy": {
                    "profile": "external-sandbox",
                    "harness": {"codex": {"work": {"bypassApprovalsAndSandbox": False}}},
                }
            }
        )

    def test_codex_work_uses_workspace_write_with_the_git_dir_as_its_own_root(self):
        self.config()
        self.write_engine("codex", CODEX_LOGGING)
        completed = self.cli("--isolation", "none", "codex", "work", "edit")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        argv = (self.results / "codex-argv").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        roots = [argv[i + 1] for i, token in enumerate(argv) if token == "--add-dir"]
        # Codex protects .git under a writable root, so the git dir is a root of its
        # own. The registry sits inside the checkout here, so it needs no root.
        self.assertEqual(roots, [os.path.realpath(self.workspace / ".git")])
        manifest, _state = self.latest_run()
        self.assertEqual(manifest["writeGuard"]["backend"], "codex-native-sandbox")
        self.assertIn("--add-dir", manifest["argv"])
        prompt = (self.results / "codex-prompt").read_text(encoding="utf-8")
        self.assertIn("sandboxed by Codex", prompt)

    def test_codex_with_the_bypass_on_gets_no_roots_and_the_bypass_flag(self):
        self.write_config({"policy": {"profile": "external-sandbox"}})
        self.write_engine("codex", CODEX_LOGGING)
        completed = self.cli("--isolation", "none", "codex", "work", "edit")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        argv = (self.results / "codex-argv").read_text(encoding="utf-8").splitlines()
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("--add-dir", argv)

    def test_the_off_switch_leaves_the_codex_argv_alone(self):
        self.config()
        self.write_engine("codex", CODEX_LOGGING)
        completed = self.cli("--isolation", "none", "codex", "work", "edit", guard="off")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        argv = (self.results / "codex-argv").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--add-dir", argv)
        prompt = (self.results / "codex-prompt").read_text(encoding="utf-8")
        self.assertNotIn("sandboxed by Codex", prompt)
        self.assertNotIn("Delegate write guard", prompt)


if __name__ == "__main__":
    unittest.main()
