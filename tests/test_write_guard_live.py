"""Work write guard: a real child, really refused.

Each test launches a fake engine through the production launch seam
(``runner._launch_tracked_process``) with the guard on and a throwaway HOME laid
out like the estate. The engine runs destructive commands (``rm -rf`` of a
credential canary, a write into a sibling checkout, moving its own checkout away)
and legitimate ones (writing its checkout, a commit, TMPDIR, caches, its engine
home); the test reads back what each command returned and what survived.

The guard is configured with ``onUnavailable: refuse`` so a missing backend fails
the test loudly instead of letting an unguarded child pass it. macOS runs the
Seatbelt backend (opt-in there); Linux runs bubblewrap. A host where the sandbox
cannot run (no ``bwrap``, disabled user namespaces, or a process already inside a
Seatbelt sandbox) skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from delegate_agent import runner, write_guard
from delegate_agent.write_guard import WriteGuardSettings

IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def _sandbox_usable() -> tuple[bool, str]:
    if sys.platform == "darwin":
        tool = shutil.which("sandbox-exec")
        if tool is None:
            return False, "sandbox-exec is not installed"
        probe = subprocess.run(
            [tool, "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True,
            check=False,
        )
        return probe.returncode == 0, "sandbox-exec cannot run here (already sandboxed?)"
    if sys.platform.startswith("linux"):
        tool = shutil.which("bwrap")
        if tool is None:
            return False, "bwrap is not installed"
        probe = subprocess.run(
            [tool, "--unshare-user", "--dev-bind", "/", "/", "/bin/true"],
            capture_output=True,
            check=False,
        )
        return probe.returncode == 0, "bwrap cannot create a user namespace here"
    return False, f"no write guard backend on {sys.platform}"


_USABLE, _WHY_NOT = _sandbox_usable()

ENGINE_SCRIPT = r"""#!/bin/sh
R="$GUARD_RESULT_DIR/results"
: > "$R"
note() { echo "$1=$2" >> "$R"; }

# --- things a confused lane must not be able to do ---
rm -rf "$HOME/.ssh/canary" 2>/dev/null; note rm_ssh_canary $?
rm -rf "$HOME/.gnupg/canary" 2>/dev/null; note rm_gnupg_canary $?
rm -rf "$HOME/.config/gh/canary" 2>/dev/null; note rm_gh_canary $?
rm -rf "$HOME/.ai-profiles/accounts/claude/personal/canary" 2>/dev/null; note rm_sibling_profile_canary $?
rm -rf "$HOME/.delegate/src/canary" 2>/dev/null; note rm_delegate_src_canary $?
echo tampered > "$HOME/.delegate/config.json" 2>/dev/null; note write_delegate_config $?
echo x > "$GUARD_SIBLING/f" 2>/dev/null; note write_sibling_checkout $?
rm -rf "$GUARD_SIBLING/canary" 2>/dev/null; note rm_sibling_canary $?
rm -rf "$HOME/.ssh" 2>/dev/null; note rm_ssh_dir $?
mv "$HOME/.ssh" "$TMPDIR/ssh-moved" 2>/dev/null; note mv_ssh_dir $?
mv "$PWD" "$TMPDIR/checkout-moved" 2>/dev/null; note mv_exec_root $?

# --- things a lane legitimately does ---
echo x > "$PWD/exec-file"; note write_exec_root $?
mkdir -p "$PWD/nested/dir" && echo x > "$PWD/nested/dir/f"; note write_exec_root_nested $?
echo x > "$TMPDIR/tmp-file"; note write_tmpdir $?
mkdir -p "$HOME/.cache/tool" && echo x > "$HOME/.cache/tool/f"; note write_home_cache $?
echo x > "$GUARD_ENGINE_HOME/state"; note write_engine_home $?
echo x > "$HOME/.delegate/worktrees/run-1/f"; note write_delegate_worktrees $?
echo x > "$GUARD_RUN_SCRATCH/f"; note write_run_scratch $?
git add exec-file >/dev/null 2>&1 && git commit -q -m "lane commit" >/dev/null 2>&1; note git_commit $?
[ -n "$GUARD_EXTRA" ] && { echo x > "$GUARD_EXTRA/f" 2>/dev/null; note write_extra $?; }
exit 0
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *IDENTITY, *args],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@unittest.skipUnless(_USABLE, _WHY_NOT)
class LiveWriteGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.results = self.root / "results"
        self.results.mkdir()
        self.canaries: list[Path] = []
        for rel in (
            ".ssh",
            ".gnupg",
            ".config/gh",
            ".ai-profiles/accounts/claude/work/work-d",
            ".ai-profiles/accounts/claude/personal",
            ".delegate/src",
            ".delegate/releases",
            ".delegate/bin",
            ".delegate/worktrees/run-1",
            ".delegate/registry/runs/run-1/scratch",
            "Code/repo",
            "Code/sibling",
        ):
            (self.home / rel).mkdir(parents=True)
        for rel in (
            ".ssh/canary",
            ".gnupg/canary",
            ".config/gh/canary",
            ".ai-profiles/accounts/claude/personal/canary",
            ".delegate/src/canary",
            "Code/sibling/canary",
        ):
            path = self.home / rel
            path.write_text("precious", encoding="utf-8")
            self.canaries.append(path)
        self.config_file = self.home / ".delegate" / "config.json"
        self.config_file.write_text("{}", encoding="utf-8")
        self.repo = self.home / "Code" / "repo"
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        git(self.repo, "add", "seed.txt")
        git(self.repo, "commit", "-q", "-m", "seed")
        self.script = self.root / "engine.sh"
        self.script.write_text(ENGINE_SCRIPT, encoding="utf-8")
        self.script.chmod(0o755)
        self.engine_home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.scratch = self.home / ".delegate/registry/runs/run-1/scratch"
        self.registry = self.home / ".delegate/registry"

    def settings(self, **kwargs) -> WriteGuardSettings:
        kwargs.setdefault("on_unavailable", write_guard.ON_UNAVAILABLE_REFUSE)
        kwargs.setdefault("macos_seatbelt", True)
        return WriteGuardSettings(**kwargs)

    def run_engine(self, cwd: Path, *, settings=None, extra_env=None, engine="claude"):
        env = {
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "CLAUDE_CONFIG_DIR": str(self.engine_home),
            "GUARD_RESULT_DIR": str(self.results),
            "GUARD_SIBLING": str(self.home / "Code" / "sibling"),
            "GUARD_ENGINE_HOME": str(self.engine_home),
            "GUARD_RUN_SCRATCH": str(self.scratch),
            "GUARD_EXTRA": "",
            **(extra_env or {}),
        }
        record: dict = {}
        process = runner._launch_tracked_process(
            [str(self.script)],
            str(cwd),
            stdin_text=None,
            env_overrides=env,
            engine=engine,
            run_path=self.root,
            guard_settings=settings or self.settings(),
            guard_record=record,
            guard_registry_root=self.registry,
            guard_run_roots=(write_guard.Reopen(str(self.scratch), "run scratch"),),
        )
        process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0)
        self.record = record
        return self.read_results()

    def read_results(self) -> dict[str, int]:
        lines = (self.results / "results").read_text(encoding="utf-8").splitlines()
        return {key: int(value) for key, value in (line.split("=") for line in lines)}

    DENIED = (
        "rm_ssh_canary",
        "rm_gnupg_canary",
        "rm_gh_canary",
        "rm_sibling_profile_canary",
        "rm_delegate_src_canary",
        "write_delegate_config",
        "write_sibling_checkout",
        "rm_sibling_canary",
        "rm_ssh_dir",
        "mv_ssh_dir",
        "mv_exec_root",
    )
    ALLOWED = (
        "write_exec_root",
        "write_exec_root_nested",
        "write_tmpdir",
        "write_home_cache",
        "write_engine_home",
        "write_delegate_worktrees",
        "write_run_scratch",
        "git_commit",
    )

    def assert_guarded(self, results: dict[str, int]) -> None:
        self.assertEqual(self.record["status"], "enforced", self.record)
        for key in self.DENIED:
            with self.subTest(denied=key):
                self.assertNotEqual(results[key], 0, f"{key} unexpectedly succeeded")
        for key in self.ALLOWED:
            with self.subTest(allowed=key):
                self.assertEqual(results[key], 0, f"{key} was refused: {results}")
        for canary in self.canaries:
            with self.subTest(canary=str(canary.relative_to(self.home))):
                self.assertEqual(canary.read_text(encoding="utf-8"), "precious")
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), "{}")
        self.assertFalse((self.home / "Code/sibling/f").exists())
        self.assertTrue(self.repo.is_dir(), "the lane moved its own checkout away")
        self.assertTrue((self.home / ".ssh").is_dir())

    def test_in_place_lane_cannot_touch_irreplaceable_paths_but_works_normally(self):
        before = git(self.repo, "rev-parse", "HEAD")
        results = self.run_engine(self.repo)
        self.assert_guarded(results)
        # The commit really landed, so the exec root and its git dir were writable.
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual((self.tmpdir / "tmp-file").read_text(encoding="utf-8").strip(), "x")

    def test_worktree_lane_can_commit_although_its_source_checkout_is_protected(self):
        worktree = self.home / ".delegate/worktrees/wt"
        git(self.repo, "worktree", "add", "-q", "-b", "lane", str(worktree))
        before = git(worktree, "rev-parse", "HEAD")
        results = self.run_engine(worktree)
        self.assert_guarded(results)
        self.assertNotEqual(git(worktree, "rev-parse", "HEAD"), before)
        # The source checkout's own files stay out of reach even from the worktree.
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_registry_and_scratch_inside_a_protected_checkout_stay_writable(self):
        # A workspace registry lives at <checkout>/.delegate, which the code root
        # protects; the run's scratch under it is still where the lane writes.
        self.registry = self.home / "Code/repo/.delegate"
        self.scratch = self.registry / "runs/run-1/scratch"
        self.scratch.mkdir(parents=True)
        worktree = self.home / ".delegate/worktrees/wt"
        git(self.repo, "worktree", "add", "-q", "-b", "lane", str(worktree))
        results = self.run_engine(worktree)
        self.assert_guarded(results)
        self.assertTrue((self.scratch / "f").exists())
        reasons = {entry["path"]: entry["reason"] for entry in self.record["writable"]}
        self.assertEqual(reasons[os.path.realpath(self.registry)], "run registry")

    def test_sibling_profile_is_protected_but_the_selected_engine_home_is_writable(self):
        results = self.run_engine(self.repo)
        self.assertNotEqual(results["rm_sibling_profile_canary"], 0)
        self.assertEqual(results["write_engine_home"], 0)
        self.assertTrue((self.engine_home / "state").exists())

    def test_any_engine_home_variable_pointing_into_the_profiles_tree_stays_writable(self):
        # An engine the guard has no built-in home variable for (droid here) still
        # works when its profile selects a directory under ~/.ai-profiles.
        profile = self.home / ".ai-profiles/accounts/droid/work"
        profile.mkdir(parents=True)
        results = self.run_engine(
            self.repo,
            engine="droid",
            extra_env={
                "FACTORY_PROFILE_DIR": str(profile),
                "GUARD_ENGINE_HOME": str(profile),
            },
        )
        self.assertEqual(results["write_engine_home"], 0)
        self.assertTrue((profile / "state").exists())
        self.assertNotEqual(results["rm_sibling_profile_canary"], 0)

    def test_env_var_naming_a_credential_store_does_not_lift_its_protection(self):
        results = self.run_engine(
            self.repo,
            engine="droid",
            extra_env={"GNUPGHOME": str(self.home / ".gnupg")},
        )
        self.assertNotEqual(results["rm_gnupg_canary"], 0)
        self.assertTrue((self.home / ".gnupg/canary").exists())

    def test_writable_reopens_one_protected_path_for_this_run(self):
        results = self.run_engine(
            self.repo,
            settings=self.settings(run_writable=(os.path.realpath(self.home / "Code/sibling"),)),
        )
        self.assertEqual(results["write_sibling_checkout"], 0)
        self.assertTrue((self.home / "Code/sibling/f").exists())
        # Everything else stays protected.
        self.assertNotEqual(results["rm_ssh_canary"], 0)
        self.assertTrue((self.home / ".ssh/canary").exists())

    def test_a_reopen_can_target_a_protected_directory_inside_a_protected_tree(self):
        extra = self.home / ".config/gh"
        results = self.run_engine(
            self.repo,
            settings=self.settings(run_writable=(os.path.realpath(extra),)),
            extra_env={"GUARD_EXTRA": str(extra)},
        )
        self.assertEqual(results["write_extra"], 0)
        self.assertEqual(results["rm_gh_canary"], 0)
        self.assertNotEqual(results["rm_ssh_canary"], 0)

    def test_removed_default_is_not_protected(self):
        results = self.run_engine(self.repo, settings=self.settings(remove=("~/.gnupg",)))
        self.assertEqual(results["rm_gnupg_canary"], 0)
        self.assertFalse((self.home / ".gnupg/canary").exists())
        self.assertNotEqual(results["rm_ssh_canary"], 0)

    def test_added_path_is_protected(self):
        vault = self.home / "vault"
        vault.mkdir()
        (vault / "canary").write_text("precious", encoding="utf-8")
        script = self.root / "engine-vault.sh"
        script.write_text(
            '#!/bin/sh\nrm -rf "$HOME/vault/canary" 2>/dev/null; echo "rm_vault=$?" > '
            '"$GUARD_RESULT_DIR/results"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.script = script
        results = self.run_engine(self.repo, settings=self.settings(add=("~/vault",)))
        self.assertNotEqual(results["rm_vault"], 0)
        self.assertTrue((vault / "canary").exists())

    def test_record_lists_the_plan_that_was_enforced(self):
        self.run_engine(self.repo)
        self.assertEqual(
            self.record["backend"], "bwrap" if sys.platform != "darwin" else "seatbelt"
        )
        self.assertIn(os.path.realpath(self.home / ".ssh"), self.record["protected"])
        reasons = {entry["path"]: entry["reason"] for entry in self.record["writable"]}
        self.assertEqual(reasons[os.path.realpath(self.repo)], "execution root")
        self.assertEqual(reasons[os.path.realpath(self.engine_home)], "engine home")
        # A re-open inside an unprotected directory changes nothing, so it is not listed.
        self.assertNotIn(os.path.realpath(self.scratch), reasons)


if __name__ == "__main__":
    unittest.main()
