"""Work write guard: plan, settings, backends' argv, fallbacks, and --forbid-commit hooks.

The live confinement proofs (a real child refused writes) live in
``tests/test_write_guard_live.py``; this file covers everything that needs no
sandbox to run.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from delegate_agent import cli_parser as parser_api
from delegate_agent import config as delegate_config
from delegate_agent import errors as error_types
from delegate_agent import request_build, runner, sandbox_bwrap, seatbelt, write_guard
from delegate_agent import write_guard_launch as launch
from delegate_agent.write_guard import GuardFacts, Reopen, WriteGuardSettings
from tests.execution_test_base import load_delegate

GIT_IDENTITY = ("-c", "user.name=Delegate Test", "-c", "user.email=delegate-test@example.com")


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


class HomeTestCase(unittest.TestCase):
    """A throwaway HOME laid out like the estate: credentials, profiles, the runtime, code."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        for rel in (
            ".ssh",
            ".gnupg",
            ".ai-profiles/accounts/claude/work/work-d",
            ".ai-profiles/accounts/claude/personal",
            ".config/gh",
            ".delegate/src",
            ".delegate/releases",
            ".delegate/bin",
            ".delegate/worktrees/run-1",
            ".delegate/registry",
            ".cache",
            "Code/repo",
            "Code/sibling",
        ):
            (self.home / rel).mkdir(parents=True)
        (self.home / ".delegate" / "config.json").write_text("{}", encoding="utf-8")
        (self.home / ".delegate" / "config.local.json").write_text("{}", encoding="utf-8")
        # A real profile home always holds its identity file.
        for rel in (
            ".ai-profiles/accounts/claude/work/work-d/.claude.json",
            ".ai-profiles/accounts/claude/personal/.claude.json",
        ):
            (self.home / rel).write_text("{}", encoding="utf-8")

    def real(self, rel: str) -> str:
        return os.path.realpath(self.home / rel)

    def plan(self, settings=None, **facts):
        facts.setdefault("exec_root", str(self.home / "Code" / "repo"))
        return write_guard.plan_guard(
            settings or WriteGuardSettings(),
            GuardFacts(home=str(self.home), **facts),
            backend=write_guard.BACKEND_BWRAP,
        )


class PlanTests(HomeTestCase):
    def test_protects_the_named_paths_and_leaves_run_state_writable(self):
        plan = self.plan()
        for rel in (
            ".ssh",
            ".gnupg",
            ".ai-profiles",
            ".config/gh",
            ".delegate/src",
            ".delegate/releases",
            ".delegate/bin",
            ".delegate/config.json",
            ".delegate/config.local.json",
            "Code",
        ):
            with self.subTest(path=rel):
                self.assertIn(self.real(rel), plan.protected)
        for rel in (".delegate/worktrees", ".delegate/registry", ".cache", "."):
            with self.subTest(path=rel):
                self.assertNotIn(self.real(rel), plan.protected)

    def test_paths_that_do_not_exist_are_dropped(self):
        plan = self.plan()
        self.assertNotIn(self.real(".aws"), plan.protected)
        self.assertTrue(all(os.path.exists(path) for path in plan.protected))

    def test_parents_come_before_children_and_the_exec_root_is_reopened(self):
        mounts = self.plan().mounts()
        code = ("ro", self.real("Code"))
        repo = ("rw", self.real("Code/repo"))
        self.assertIn(code, mounts)
        self.assertIn(repo, mounts)
        self.assertLess(mounts.index(code), mounts.index(repo))
        self.assertNotIn(("rw", self.real("Code/sibling")), mounts)

    def test_git_common_dir_of_a_worktree_is_reopened_under_the_protected_source(self):
        (self.home / "Code/repo/.git").mkdir()
        plan = self.plan(
            exec_root=str(self.home / ".delegate/worktrees/run-1"),
            git_common_dir=str(self.home / "Code/repo/.git"),
        )
        mounts = plan.mounts()
        self.assertIn(("rw", self.real("Code/repo/.git")), mounts)
        self.assertLess(
            mounts.index(("ro", self.real("Code"))),
            mounts.index(("rw", self.real("Code/repo/.git"))),
        )
        # The worktree itself is outside every protected path, so it needs no mount.
        self.assertNotIn(self.real(".delegate/worktrees/run-1"), [path for _, path in mounts])

    def test_engine_home_is_reopened_inside_ai_profiles_and_siblings_stay_protected(self):
        engine_home = str(self.home / ".ai-profiles/accounts/claude/work/work-d")
        plan = self.plan(home_candidates=(Reopen(engine_home, "engine home"),))
        mounts = plan.mounts()
        self.assertIn(("rw", self.real(".ai-profiles/accounts/claude/work/work-d")), mounts)
        self.assertNotIn(("rw", self.real(".ai-profiles/accounts/claude/personal")), mounts)
        self.assertIn(("ro", self.real(".ai-profiles")), mounts)
        self.assertEqual(plan.refused, ())

    def test_a_candidate_that_contains_another_profile_home_is_refused(self):
        # An environment variable naming a parent of several profiles would otherwise make
        # every sibling profile writable through one reopen.
        for rel, reason in (
            (".ai-profiles", "itself a protected path"),
            (".ai-profiles/accounts", "not itself a profile home"),
            (".ai-profiles/accounts/claude", "not itself a profile home"),
            (".ai-profiles/accounts/claude/work", "not itself a profile home"),
        ):
            with self.subTest(candidate=rel):
                plan = self.plan(home_candidates=(Reopen(str(self.home / rel), "engine home"),))
                self.assertNotIn(("rw", self.real(rel)), plan.mounts())
                self.assertNotIn(
                    ("rw", self.real(".ai-profiles/accounts/claude/personal")), plan.mounts()
                )
                self.assertIn(("ro", self.real(".ai-profiles")), plan.mounts())
                self.assertEqual([entry.path for entry in plan.refused], [self.real(rel)])
                self.assertIn(reason, plan.refused[0].reason)
                self.assertIn("engine home not reopened", plan.refused[0].reason)
                self.assertEqual(plan.payload()["refused"][0]["path"], self.real(rel))

    def test_a_parent_whose_profiles_carry_no_identity_file_is_still_refused(self):
        # The scan below a parent cannot see a profile that has no identity file yet (a fresh
        # login, a different engine's layout). The parent is refused for lacking one itself.
        for rel in ("work/work-d/.claude.json", "personal/.claude.json"):
            (self.home / ".ai-profiles/accounts/claude" / rel).unlink()
        parent = ".ai-profiles/accounts/claude"
        plan = self.plan(home_candidates=(Reopen(str(self.home / parent), "engine home"),))
        self.assertNotIn(("rw", self.real(parent)), plan.mounts())
        self.assertEqual([entry.path for entry in plan.refused], [self.real(parent)])
        self.assertIn("not itself a profile home", plan.refused[0].reason)

    def test_a_marked_candidate_below_a_credential_directory_is_refused(self):
        # Only the protected path itself was refused before; a child of ~/.ssh is not a profile.
        (self.home / ".ssh/keys").mkdir()
        plan = self.plan(home_candidates=(Reopen(str(self.home / ".ssh/keys"), "engine home"),))
        self.assertNotIn(("rw", self.real(".ssh/keys")), plan.mounts())
        self.assertIn("not itself a profile home", plan.refused[0].reason)

    def test_a_profile_home_holding_another_profile_is_refused(self):
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        nested = home / "backup/old-profile"
        nested.mkdir(parents=True)
        (nested / ".credentials.json").write_text("{}", encoding="utf-8")
        plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertNotIn(("rw", str(home)), plan.mounts())
        self.assertIn("another profile home", plan.refused[0].reason)
        self.assertIn(str(nested), plan.refused[0].reason)

    def test_a_profile_home_holding_another_three_levels_down_is_refused_but_not_four(self):
        # The scan looks three levels below the reopened home (docs/security-model.md).
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        near = home / "a/b/near-home"
        near.mkdir(parents=True)
        (near / ".claude.json").write_text("{}", encoding="utf-8")
        plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertNotIn(("rw", str(home)), plan.mounts())
        self.assertIn("another profile home", plan.refused[0].reason)
        self.assertIn(str(near), plan.refused[0].reason)

        (near / ".claude.json").unlink()
        far = home / "x/y/z/far-home"
        far.mkdir(parents=True)
        (far / ".claude.json").write_text("{}", encoding="utf-8")
        plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertEqual(plan.refused, ())
        self.assertIn(("rw", str(home)), plan.mounts())

    def test_a_profile_home_whose_scan_cannot_finish_is_refused(self):
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        for index in range(4):
            (home / f"d{index}").mkdir()
        with mock.patch.object(write_guard, "_HOME_SCAN_MAX_DIRS", 3):
            plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertNotIn(("rw", str(home)), plan.mounts())
        self.assertIn("could not be proven", plan.refused[0].reason)
        self.assertIn("more than 3 directories", plan.refused[0].reason)

    def test_a_profile_home_with_an_unlistable_directory_is_refused(self):
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        locked = home / "locked"
        locked.mkdir()
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(locked):
                raise PermissionError(13, "Permission denied", str(locked))
            return real_scandir(path)

        with mock.patch.object(write_guard.os, "scandir", side_effect=scandir):
            plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertNotIn(("rw", str(home)), plan.mounts())
        self.assertIn("could not be proven", plan.refused[0].reason)
        self.assertIn(str(locked), plan.refused[0].reason)

    def test_a_symlink_inside_a_profile_home_does_not_block_it(self):
        # A write through the link lands on its target, which a reopen by real path does not
        # cover; the outer read-only mount still holds the sibling.
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        (home / "sibling-link").symlink_to(self.home / ".ai-profiles/accounts/claude/personal")
        plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertIn(("rw", self.real(".ai-profiles/accounts/claude/work/work-d")), plan.mounts())
        self.assertEqual(plan.refused, ())

    def test_a_leaf_profile_home_full_of_content_is_still_reopened(self):
        # Skills, projects and plugins are content, not sibling profiles: an identity-looking
        # file inside them must not make the real home unwritable.
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        for rel in (
            "skills/s/.credentials.json",
            "projects/p/.claude.json",
            "plugins/x/y/.claude.json",
        ):
            (home / rel).parent.mkdir(parents=True)
            (home / rel).write_text("{}", encoding="utf-8")
        (home / "todos/t/auth.json").parent.mkdir(parents=True)
        (home / "todos/t/auth.json").write_text("{}", encoding="utf-8")
        (home / "ide/x").mkdir(parents=True)
        (home / "ide/x/auth.json").write_text("{}", encoding="utf-8")  # no config.toml beside it
        plan = self.plan(home_candidates=(Reopen(str(home), "engine home"),))
        self.assertIn(("rw", self.real(".ai-profiles/accounts/claude/work/work-d")), plan.mounts())
        self.assertEqual(plan.refused, ())

    def test_a_codex_style_home_pair_marks_a_profile_home_both_ways(self):
        codex = self.home / ".ai-profiles/accounts/codex/work"
        nested = codex / "archive/personal"
        for directory in (codex, nested):
            directory.mkdir(parents=True)
            (directory / "auth.json").write_text("{}", encoding="utf-8")
            (directory / "config.toml").write_text("", encoding="utf-8")
        plan = self.plan(home_candidates=(Reopen(str(codex), "engine home"),))
        self.assertEqual(len(plan.refused), 1)
        self.assertIn(str(nested), plan.refused[0].reason)
        (nested / "auth.json").unlink()
        plan = self.plan(home_candidates=(Reopen(str(codex), "engine home"),))
        self.assertEqual(plan.refused, ())
        self.assertIn(("rw", str(codex)), plan.mounts())

    def test_a_candidate_outside_every_protected_path_is_harmless(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        plan = self.plan(home_candidates=(Reopen(str(elsewhere), "engine home"),))
        self.assertEqual(plan.refused, ())
        self.assertNotIn(("ro", self.real("elsewhere")), plan.mounts())

    def test_a_protected_path_nested_in_an_accepted_engine_home_stays_protected(self):
        home = self.home / ".ai-profiles/accounts/claude/work/work-d"
        (home / "secrets").mkdir()
        plan = self.plan(
            WriteGuardSettings(add=(str(home / "secrets"),)),
            home_candidates=(Reopen(str(home), "engine home"),),
        )
        mounts = plan.mounts()
        self.assertEqual(plan.refused, ())
        self.assertLess(
            mounts.index(("rw", self.real(".ai-profiles/accounts/claude/work/work-d"))),
            mounts.index(("ro", self.real(".ai-profiles/accounts/claude/work/work-d/secrets"))),
        )

    def test_only_the_engines_own_home_variable_is_a_candidate(self):
        profile = self.home / ".ai-profiles/accounts/claude/work/work-d"
        env = {
            "CLAUDE_CONFIG_DIR": str(profile),
            "CODEX_HOME": str(self.home / ".ai-profiles/accounts/claude/personal"),
            "GNUPGHOME": str(self.home / ".gnupg"),
            "PROFILES_ROOT": str(self.home / ".ai-profiles"),
            "TOOL_STATE_DIR": str(self.home / ".ai-profiles/accounts/claude"),
        }
        candidates = launch.engine_home_candidates("claude", env, str(self.home))
        self.assertEqual([(c.path, c.reason) for c in candidates], [(str(profile), "engine home")])
        # No variable Delegate knows for this engine: nothing, however many name a profile.
        self.assertEqual(launch.engine_home_candidates("droid", env, str(self.home)), ())
        facts = launch._facts(
            argv=["droid"],
            cwd=str(self.home / "Code/repo"),
            env={**env, "HOME": str(self.home)},
            engine="droid",
            registry_root=None,
            run_roots=(),
            common_dir=None,
        )
        self.assertEqual(facts.home_candidates, ())
        self.assertEqual(facts.run_roots, ())
        self.assertEqual(facts.launcher_roots, ())

    def test_remove_drops_a_default_and_add_protects_an_extra_path(self):
        (self.home / "vault").mkdir()
        plan = self.plan(
            WriteGuardSettings(remove=("~/.gnupg",), add=("~/vault",)),
        )
        self.assertNotIn(self.real(".gnupg"), plan.protected)
        self.assertIn(self.real("vault"), plan.protected)
        self.assertIn(self.real(".ssh"), plan.protected)

    def test_config_writable_and_run_writable_lift_a_protection(self):
        plan = self.plan(
            WriteGuardSettings(writable=("~/.ssh",), run_writable=(self.real("Code/sibling"),))
        )
        self.assertNotIn(self.real(".ssh"), plan.protected)
        mounts = plan.mounts()
        self.assertIn(("rw", self.real("Code/sibling")), mounts)
        reasons = {entry.path: entry.reason for entry in plan.writable}
        self.assertEqual(reasons[self.real("Code/sibling")], "--writable")
        self.assertEqual(reasons[self.real(".ssh")], "isolation.writeGuard.writable")

    def test_a_reopen_that_is_already_writable_needs_no_mount(self):
        plan = self.plan(run_roots=(Reopen(str(self.home / ".cache"), "cache"),))
        self.assertNotIn(("rw", self.real(".cache")), plan.mounts())

    def test_a_null_code_root_leaves_checkouts_unprotected(self):
        plan = self.plan(WriteGuardSettings(code_root=None))
        self.assertNotIn(self.real("Code"), plan.protected)

    def test_payload_names_backend_protected_and_reopens_with_reasons(self):
        payload = self.plan().payload()
        self.assertEqual(payload["backend"], "bwrap")
        self.assertIn(self.real(".ssh"), payload["protected"])
        self.assertIn(
            {"path": self.real("Code/repo"), "reason": "execution root"}, payload["writable"]
        )

    def test_pin_makes_an_unprotected_exec_root_its_own_mount(self):
        # A worktree lane's checkout sits outside every protected path, so without a pin
        # it is not a mount point and the lane could rename it away.
        worktree = self.real(".delegate/worktrees/run-1")
        plan = self.plan(exec_root=worktree)
        self.assertNotIn(("rw", worktree), plan.mounts())
        pinned = plan.mounts(pin=[worktree])
        self.assertIn(("rw", worktree), pinned)
        self.assertEqual(pinned.count(("rw", worktree)), 1)

    def test_pin_comes_after_its_protected_parents_and_keeps_an_existing_mode(self):
        repo = self.real("Code/repo")
        plan = self.plan()
        self.assertEqual(plan.mounts(pin=[repo]), plan.mounts())
        self.assertLess(
            plan.mounts(pin=[repo]).index(("ro", self.real("Code"))),
            plan.mounts(pin=[repo]).index(("rw", repo)),
        )
        # A pinned path that is itself protected keeps its protection.
        ssh = self.real(".ssh")
        self.assertIn(("ro", ssh), plan.mounts(pin=[ssh]))
        self.assertNotIn(("rw", ssh), plan.mounts(pin=[ssh]))

    def test_pinning_the_filesystem_root_is_ignored(self):
        plan = self.plan()
        self.assertEqual(plan.mounts(pin=["/", ""]), plan.mounts())


class SettingsTests(unittest.TestCase):
    def test_defaults_are_on_and_warn(self):
        settings = write_guard.settings_from_config({}, env={})
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.on_unavailable, "warn")
        self.assertFalse(settings.macos_seatbelt)
        self.assertEqual(settings.code_root, "~/Code")

    def test_config_section_is_read(self):
        config = {
            "isolation": {
                "writeGuard": {
                    "enabled": False,
                    "onUnavailable": "refuse",
                    "macosSeatbelt": True,
                    "codeRoot": "/srv/code",
                    "add": ["~/vault"],
                    "remove": ["~/.gnupg"],
                    "writable": ["~/.cache/tool"],
                }
            }
        }
        settings = write_guard.settings_from_config(config, env={})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.on_unavailable, "refuse")
        self.assertTrue(settings.macos_seatbelt)
        self.assertEqual(settings.code_root, "/srv/code")
        self.assertEqual(settings.add, ("~/vault",))
        self.assertEqual(settings.remove, ("~/.gnupg",))
        self.assertEqual(settings.writable, ("~/.cache/tool",))

    def test_environment_override_wins_over_config_both_ways(self):
        on = {"isolation": {"writeGuard": {"enabled": True}}}
        off = {"isolation": {"writeGuard": {"enabled": False}}}
        self.assertFalse(
            write_guard.settings_from_config(on, env={write_guard.ENV_OVERRIDE: "off"}).enabled
        )
        self.assertTrue(
            write_guard.settings_from_config(off, env={write_guard.ENV_OVERRIDE: "1"}).enabled
        )

    def test_validation_names_the_bad_key(self):
        bad = {
            "enabled": "yes",
            "onUnavailable": "shrug",
            "macosSeatbelt": 1,
            "codeRoot": "relative/path",
            "add": "~/x",
            "remove": ["relative"],
            "unknownKey": True,
        }
        for key, value in bad.items():
            with self.subTest(key=key):
                message = write_guard.validate_config_section({key: value})
                self.assertIsNotNone(message)
                self.assertIn(key, message)
        self.assertIsNone(write_guard.validate_config_section(None))
        self.assertIsNone(
            write_guard.validate_config_section({"codeRoot": None, "add": ["~/vault", "/abs"]})
        )

    def test_config_validation_rejects_a_bad_section(self):
        config = delegate_config.embedded_default_config()
        config["isolation"] = {**config["isolation"], "writeGuard": {"onUnavailable": "shrug"}}
        with self.assertRaises(delegate_config.ConfigError) as ctx:
            delegate_config.validate_config(config)
        self.assertEqual(ctx.exception.error, "invalid_isolation_config")
        self.assertIn("onUnavailable", ctx.exception.message)


class BackendArgvTests(unittest.TestCase):
    def test_bwrap_argv_binds_root_writable_then_each_mount_in_order(self):
        argv = sandbox_bwrap.build_work_guard_argv(
            mounts=[("ro", "/home/u/Code"), ("rw", "/home/u/Code/repo")],
            engine_argv=["engine", "--flag"],
            cwd="/home/u/Code/repo",
            bwrap_path="/usr/bin/bwrap",
        )
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        root = argv.index("--dev-bind")
        self.assertEqual(argv[root : root + 3], ["--dev-bind", "/", "/"])
        ro = argv.index("--ro-bind")
        rw = argv.index("--bind")
        self.assertLess(root, ro)
        self.assertLess(ro, rw)
        self.assertEqual(argv[ro : ro + 3], ["--ro-bind", "/home/u/Code", "/home/u/Code"])
        self.assertEqual(argv[rw : rw + 3], ["--bind", "/home/u/Code/repo", "/home/u/Code/repo"])
        self.assertEqual(argv[-5:], ["--chdir", "/home/u/Code/repo", "--", "engine", "--flag"][-5:])
        self.assertIn("--die-with-parent", argv)
        self.assertNotIn("--tmpfs", argv)

    def test_bwrap_argv_refuses_a_relative_mount_path(self):
        with self.assertRaises(error_types.DelegateError):
            sandbox_bwrap.build_work_guard_argv(
                mounts=[("ro", "relative")], engine_argv=["e"], cwd="/w"
            )

    def test_seatbelt_profile_allows_by_default_then_denies_and_reallows_in_order(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            (base / "code" / "repo").mkdir(parents=True)
            profile = seatbelt.build_work_guard_profile(
                [("ro", str(base / "code")), ("rw", str(base / "code" / "repo"))],
                no_unlink=[str(base / "code" / "repo")],
            )
        lines = profile.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(allow default)"])
        deny = next(i for i, line in enumerate(lines) if line.startswith("(deny file-write*"))
        allow = next(i for i, line in enumerate(lines) if line.startswith("(allow file-write*"))
        unlink = next(i for i, line in enumerate(lines) if "file-write-unlink" in line)
        self.assertLess(deny, allow)
        self.assertLess(allow, unlink)
        self.assertIn("(literal ", lines[unlink])

    def test_seatbelt_wrap_prefixes_sandbox_exec(self):
        self.assertEqual(
            seatbelt.work_guard_argv("(version 1)", ["engine", "x"]),
            ["sandbox-exec", "-p", "(version 1)", "engine", "x"],
        )


class FallbackTests(HomeTestCase):
    def apply(self, settings, *, platform, which, argv=None, engine="cursor"):
        env = {"HOME": str(self.home)}
        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(shutil, "which", return_value=which),
        ):
            return launch.apply_write_guard(
                settings,
                argv=argv or ["engine"],
                cwd=str(self.home / "Code" / "repo"),
                env=env,
                engine=engine,
            )

    def test_missing_bwrap_warns_and_runs_unguarded_by_default(self):
        result = self.apply(WriteGuardSettings(), platform="linux", which=None)
        self.assertEqual(result.argv, ["engine"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("bubblewrap", result.record["reason"])
        self.assertIn("not guarded", result.warning)

    def test_missing_bwrap_refuses_when_configured(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.apply(WriteGuardSettings(on_unavailable="refuse"), platform="linux", which=None)
        self.assertEqual(ctx.exception.error, "write_guard_unavailable")
        self.assertIn("onUnavailable", ctx.exception.message)

    def test_a_failed_bwrap_preflight_counts_as_unavailable(self):
        failure = error_types.DelegateError("bwrap_preflight_failed", "user namespaces disabled")
        with mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=failure):
            result = self.apply(WriteGuardSettings(), platform="linux", which="/usr/bin/bwrap")
        self.assertEqual(result.argv, ["engine"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("user namespaces", result.record["reason"])
        self.assertIsNotNone(result.warning)

    def apply_with_seatbelt_probe(self, settings, *, returncode, stderr=b""):
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            if argv and argv[0] == "sandbox-exec":
                return subprocess.CompletedProcess(argv, returncode, b"", stderr)
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(launch.subprocess, "run", side_effect=run):
            return self.apply(
                settings, platform="darwin", which="/usr/bin/sandbox-exec", argv=["engine", "x"]
            )

    def test_seatbelt_wraps_when_the_probe_passes(self):
        result = self.apply_with_seatbelt_probe(
            WriteGuardSettings(macos_seatbelt=True), returncode=0
        )
        self.assertEqual(result.argv[:2], ["sandbox-exec", "-p"])
        self.assertEqual(result.argv[-2:], ["engine", "x"])
        self.assertEqual(result.record["status"], "enforced")
        self.assertEqual(result.record["backend"], "seatbelt")
        self.assertIsNone(result.warning)

    def test_a_failed_seatbelt_probe_counts_as_unavailable(self):
        # A nested sandbox_apply fails inside an already sandboxed process; the run
        # must not be wrapped in a profile that cannot start.
        result = self.apply_with_seatbelt_probe(
            WriteGuardSettings(macos_seatbelt=True),
            returncode=71,
            stderr=b"sandbox_apply: Operation not permitted\n",
        )
        self.assertEqual(result.argv, ["engine", "x"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertIn("preflight failed", result.record["reason"])
        self.assertIn("Operation not permitted", result.record["reason"])
        self.assertIn("not guarded", result.warning)

    def test_a_failed_seatbelt_probe_refuses_when_configured(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.apply_with_seatbelt_probe(
                WriteGuardSettings(macos_seatbelt=True, on_unavailable="refuse"), returncode=71
            )
        self.assertEqual(ctx.exception.error, "write_guard_unavailable")

    def test_macos_seatbelt_off_is_off_not_unavailable(self):
        # Opt-in on the Mac: nothing is missing, so no warning and no refusal.
        for on_unavailable in ("warn", "refuse"):
            with self.subTest(on_unavailable=on_unavailable):
                result = self.apply(
                    WriteGuardSettings(on_unavailable=on_unavailable),
                    platform="darwin",
                    which="/usr/bin/sandbox-exec",
                )
                self.assertEqual(result.argv, ["engine"])
                self.assertEqual(result.record["status"], "off")
                self.assertIsNone(result.warning)

    def test_codex_with_its_own_sandbox_is_never_wrapped(self):
        argv = ["codex", "exec", "--sandbox", "workspace-write", "task"]
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                result = self.apply(
                    WriteGuardSettings(macos_seatbelt=True),
                    platform=platform,
                    which="/usr/bin/tool",
                    argv=argv,
                    engine="codex",
                )
                self.assertEqual(result.argv, argv)
                self.assertEqual(result.record["backend"], "codex-native-sandbox")

    def test_codex_bypass_is_wrapped_like_any_other_engine_on_linux(self):
        argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "task"]
        self.assertFalse(write_guard.codex_native_sandbox_on(argv))
        with mock.patch.object(sandbox_bwrap, "preflight_plan"):
            result = self.apply(
                WriteGuardSettings(),
                platform="linux",
                which="/usr/bin/bwrap",
                argv=argv,
                engine="codex",
            )
        self.assertEqual(result.record["backend"], "bwrap")
        self.assertEqual(result.argv[0], "/usr/bin/bwrap")
        self.assertEqual(result.argv[-len(argv) :], argv)


class EngineHomeLaunchTests(HomeTestCase):
    """What the launch seam reopens under ~/.ai-profiles, seen through the bwrap argv."""

    def apply(self, env_extra, *, engine="claude", argv=("engine",)):
        env = {"HOME": str(self.home), **env_extra}
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
            mock.patch.object(sandbox_bwrap, "preflight_plan"),
        ):
            return launch.apply_write_guard(
                WriteGuardSettings(),
                argv=list(argv),
                cwd=str(self.home / "Code" / "repo"),
                env=env,
                engine=engine,
            )

    @staticmethod
    def rw_binds(result):
        argv = result.argv
        return [argv[i + 1] for i, token in enumerate(argv) if token == "--bind"]

    def test_the_selected_engine_home_is_reopened_and_an_ambient_parent_is_not(self):
        result = self.apply(
            {
                "CLAUDE_CONFIG_DIR": str(self.home / ".ai-profiles/accounts/claude/work/work-d"),
                "TOOL_STATE_DIR": str(self.home / ".ai-profiles/accounts/claude"),
                "PROFILES_ROOT": str(self.home / ".ai-profiles"),
                # A sibling leaf profile: it passes every check a candidate must pass, so
                # only "not the engine's own variable" keeps it closed.
                "OTHER_PROFILE_HOME": str(self.home / ".ai-profiles/accounts/claude/personal"),
            }
        )
        binds = self.rw_binds(result)
        self.assertIn(self.real(".ai-profiles/accounts/claude/work/work-d"), binds)
        for rel in (
            ".ai-profiles",
            ".ai-profiles/accounts/claude",
            ".ai-profiles/accounts/claude/personal",
        ):
            self.assertNotIn(self.real(rel), binds)
        self.assertNotIn("refused", result.record)
        self.assertIsNone(result.warning)

    def test_a_refused_engine_home_is_reported_with_the_way_to_open_it(self):
        parent = self.real(".ai-profiles/accounts/claude")
        result = self.apply({"CLAUDE_CONFIG_DIR": parent})
        self.assertNotIn(parent, self.rw_binds(result))
        self.assertEqual([entry["path"] for entry in result.record["refused"]], [parent])
        self.assertIn("not itself a profile home", result.record["refused"][0]["reason"])
        self.assertIn(parent, result.warning)
        self.assertIn("isolation.writeGuard.writable", result.warning)
        self.assertEqual(result.record["status"], "enforced")

    def test_an_engine_with_no_home_variable_reopens_nothing_from_the_environment(self):
        result = self.apply(
            {"FACTORY_PROFILE_DIR": str(self.home / ".ai-profiles/accounts/claude/personal")},
            engine="droid",
        )
        self.assertNotIn(self.real(".ai-profiles/accounts/claude/personal"), self.rw_binds(result))


class EstateLauncherTests(HomeTestCase):
    """A lane started through an estate launcher gets the profiles root reopened.

    Live 2026-09-28: estate-claude under the Seatbelt guard died before Claude
    started, on chmod of ~/.ai-profiles/personas/work/claude-plugins, because the
    launcher picks an account, refreshes its token, and writes plugin, session and
    lock state under the profiles root on every launch.
    """

    apply = EngineHomeLaunchTests.apply
    rw_binds = staticmethod(EngineHomeLaunchTests.rw_binds)

    @staticmethod
    def ro_binds(result):
        argv = result.argv
        return [argv[i + 1] for i, token in enumerate(argv) if token == "--ro-bind"]

    def test_an_estate_launcher_lane_can_write_the_profiles_root(self):
        for argv0 in ("estate-claude", "/home/agent/.local/bin/estate-codex"):
            with self.subTest(argv0=argv0):
                result = self.apply({}, argv=(argv0, "-p"))
                profiles = self.real(".ai-profiles")
                self.assertNotIn(profiles, self.ro_binds(result))
                self.assertNotIn(profiles, result.record["protected"])
                self.assertIn(
                    {"path": profiles, "reason": write_guard.ESTATE_LAUNCHER_REASON},
                    result.record["writable"],
                )
                # Everything else stays protected.
                self.assertIn(self.real(".ssh"), self.ro_binds(result))
                self.assertIn(self.real("Code"), self.ro_binds(result))

    def test_a_lane_started_any_other_way_keeps_the_profiles_root_protected(self):
        for argv0 in ("devin", "/usr/local/bin/claude", "my-estate-claude"):
            with self.subTest(argv0=argv0):
                result = self.apply({}, argv=(argv0,))
                self.assertIn(self.real(".ai-profiles"), self.ro_binds(result))
                self.assertNotIn(self.real(".ai-profiles"), self.rw_binds(result))

    def test_the_launchers_own_root_override_is_followed(self):
        other = self.home / "profiles-elsewhere"
        other.mkdir()
        for var in ("ESTATE_AI_PROFILES_ROOT", "AI_PROFILES_ROOT"):
            with self.subTest(var=var):
                roots = write_guard.estate_launcher_roots(
                    ["estate-omp"], {var: str(other)}, str(self.home)
                )
                self.assertEqual([r.path for r in roots], [str(other)])
        roots = write_guard.estate_launcher_roots(
            ["estate-omp"],
            {"ESTATE_AI_PROFILES_ROOT": str(other), "AI_PROFILES_ROOT": "/nope"},
            str(self.home),
        )
        self.assertEqual([r.path for r in roots], [str(other)])
        self.assertEqual(write_guard.estate_launcher_roots([], {}, str(self.home)), ())

    def test_dry_run_preview_shows_the_reopen(self):
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
        ):
            payload = launch.preview_payload(
                WriteGuardSettings(),
                engine="claude",
                argv=["estate-claude", "-p"],
                exec_root=str(self.home / "Code" / "repo"),
                registry_root=None,
                home=str(self.home),
            )
        self.assertIn(
            {"path": self.real(".ai-profiles"), "reason": write_guard.ESTATE_LAUNCHER_REASON},
            payload["writable"],
        )
        self.assertNotIn(self.real(".ai-profiles"), payload["protected"])

    def test_a_codex_native_sandbox_never_gets_the_profiles_root(self):
        facts = GuardFacts(
            home=str(self.home),
            exec_root=str(self.home / "Code" / "repo"),
            launcher_roots=write_guard.estate_launcher_roots(["estate-codex"], {}, str(self.home)),
        )
        roots = write_guard.native_writable_roots(WriteGuardSettings(), facts)
        self.assertNotIn(self.real(".ai-profiles"), [r.path for r in roots])


class BwrapLaunchShapeTests(HomeTestCase):
    """The bwrap argv the launch seam builds, and what it does when a path will not bind."""

    def apply(self, settings=None, *, cwd=None, preflight=None, env_extra=None, engine="cursor"):
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
            mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=preflight),
        ):
            return launch.apply_write_guard(
                settings or WriteGuardSettings(),
                argv=["engine"],
                cwd=cwd or str(self.home / "Code" / "repo"),
                env={"HOME": str(self.home), **(env_extra or {})},
                engine=engine,
            )

    @staticmethod
    def binds(result, flag):
        argv = result.argv
        return [argv[i + 1] for i, token in enumerate(argv) if token == flag]

    def refusing(self, *bad_rel):
        """A preflight that fails for any argv that names one of the given paths."""
        bad = [self.real(rel) for rel in bad_rel]

        def preflight(argv, **_kwargs):
            if any(path in argv for path in bad):
                raise error_types.DelegateError(
                    "bwrap_launch_failed", "bwrap preflight failed: Can't bind mount"
                )

        return preflight

    def test_a_worktree_exec_root_outside_every_protected_path_is_pinned(self):
        worktree = self.real(".delegate/worktrees/run-1")
        result = self.apply(cwd=worktree)
        self.assertIn(worktree, self.binds(result, "--bind"))

    def test_one_unbindable_path_is_dropped_and_the_rest_stay_guarded(self):
        result = self.apply(preflight=self.refusing(".gnupg"))
        gnupg = self.real(".gnupg")
        self.assertNotIn(gnupg, self.binds(result, "--ro-bind"))
        self.assertIn(self.real(".ssh"), self.binds(result, "--ro-bind"))
        self.assertIn(self.real(".ai-profiles"), self.binds(result, "--ro-bind"))
        self.assertEqual(result.argv[0], "/usr/bin/bwrap")
        # A protected path is open this run, so the record must not claim "enforced".
        self.assertEqual(result.record["status"], "partial")
        self.assertTrue(result.warning.startswith("work write guard is PARTIAL"), result.warning)
        self.assertEqual(
            [(e["path"], e["mode"]) for e in result.record["unbound"]], [(gnupg, "ro")]
        )
        self.assertIn("Can't bind mount", result.record["unbound"][0]["reason"])
        self.assertNotIn(gnupg, result.record["protected"])
        self.assertIn(self.real(".ssh"), result.record["protected"])
        self.assertIn(gnupg, result.warning)
        self.assertIn("left unprotected", result.warning)

    def test_an_unbindable_reopen_is_dropped_and_reported_as_not_reopened(self):
        rel = ".ai-profiles/accounts/claude/work/work-d"
        result = self.apply(
            preflight=self.refusing(rel),
            env_extra={"CLAUDE_CONFIG_DIR": str(self.home / rel)},
            engine="claude",
        )
        self.assertNotIn(self.real(rel), self.binds(result, "--bind"))
        self.assertEqual(result.record["unbound"][0]["mode"], "rw")
        self.assertNotIn(self.real(rel), [e["path"] for e in result.record["writable"]])
        self.assertIn("not reopened", result.warning)
        # Every protected path is still bound; only a reopen failed, so nothing is exposed.
        self.assertEqual(result.record["status"], "enforced")
        self.assertNotIn("PARTIAL", result.warning)

    def test_refuse_still_refuses_when_one_path_will_not_bind(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.apply(
                WriteGuardSettings(on_unavailable="refuse"), preflight=self.refusing(".gnupg")
            )
        self.assertEqual(ctx.exception.error, "write_guard_unavailable")

    def test_a_boundary_that_cannot_run_at_all_is_still_unavailable(self):
        def preflight(argv, **_kwargs):
            raise error_types.DelegateError("bwrap_launch_failed", "user namespaces disabled")

        result = self.apply(preflight=preflight)
        self.assertEqual(result.argv, ["engine"])
        self.assertEqual(result.record["status"], "unavailable")
        self.assertNotIn("unbound", result.record)

    def test_a_plan_that_still_fails_after_dropping_is_unavailable(self):
        gnupg = self.real(".gnupg")

        def preflight(argv, **_kwargs):
            if argv[-1] == "/bin/true":  # the baseline and the one-mount probes
                if gnupg in argv:
                    raise error_types.DelegateError("bwrap_launch_failed", "gnupg alone fails")
                return
            raise error_types.DelegateError("bwrap_launch_failed", "the full plan fails")

        result = self.apply(preflight=preflight)
        self.assertEqual(result.record["status"], "unavailable")
        self.assertEqual(result.argv, ["engine"])

    def test_unbindable_mounts_names_each_failing_mount_only(self):
        def preflight(argv, **_kwargs):
            if "/a" in argv:
                raise error_types.DelegateError("bwrap_launch_failed", "nope: /a")

        with mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=preflight):
            failed = sandbox_bwrap.unbindable_mounts(
                [("ro", "/a"), ("rw", "/b"), ("ro", "/c")], bwrap_path="bwrap"
            )
        self.assertEqual(failed, [("ro", "/a", "nope: /a")])

    def test_unbindable_mounts_reports_nothing_when_the_empty_plan_fails_too(self):
        failure = error_types.DelegateError("bwrap_launch_failed", "no user namespaces")
        with mock.patch.object(sandbox_bwrap, "preflight_plan", side_effect=failure):
            self.assertEqual(
                sandbox_bwrap.unbindable_mounts([("ro", "/a")], bwrap_path="bwrap"), []
            )


class LaunchSeamGuardTests(HomeTestCase):
    def test_native_codex_record_survives_the_launch_and_the_child_is_not_wrapped(self):
        # Seatbelt is switched on so a wrongly wrapped Codex would show: the record
        # captured with the argv's --add-dir roots must not be replaced at launch.
        repo = self.home / "Code" / "repo"
        out = self.home / "argv.txt"
        script = self.home / "codex"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$0" > "{out}"\n', encoding="utf-8")
        script.chmod(0o755)
        record = {"backend": "codex-native-sandbox", "status": "recorded-earlier"}
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            process = runner._launch_tracked_process(
                [str(script), "exec", "--sandbox", "workspace-write", "task"],
                str(repo),
                stdin_text=None,
                engine="codex",
                run_path=self.home / ".delegate" / "registry",
                guard_settings=WriteGuardSettings(macos_seatbelt=True),
                guard_record=record,
            )
            process.communicate(timeout=30)
        self.assertEqual(record, {"backend": "codex-native-sandbox", "status": "recorded-earlier"})
        self.assertEqual(out.read_text(encoding="utf-8").strip(), str(script))
        self.assertEqual(process.args[0], str(script))


class CodexNativeRootsTests(HomeTestCase):
    def test_sandbox_detection_reads_the_argv(self):
        self.assertTrue(
            write_guard.codex_native_sandbox_on(["codex", "exec", "--sandbox", "workspace-write"])
        )
        self.assertFalse(
            write_guard.codex_native_sandbox_on(["codex", "exec", "--sandbox", "read-only"])
        )
        self.assertFalse(
            write_guard.codex_native_sandbox_on(
                ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
            )
        )

    def test_roots_include_git_common_dir_even_inside_the_exec_root(self):
        (self.home / "Code/repo/.git").mkdir()
        (self.home / ".cache/tool").mkdir()
        facts = GuardFacts(
            home=str(self.home),
            exec_root=str(self.home / "Code/repo"),
            git_common_dir=str(self.home / "Code/repo/.git"),
            registry_root=str(self.home / ".delegate/registry"),
            run_roots=(Reopen(str(self.home / "Code/repo/scratch"), "inside"),),
        )
        roots = {
            r.path: r.reason for r in write_guard.native_writable_roots(WriteGuardSettings(), facts)
        }
        # Codex protects .git under a writable root unless it is its own root.
        self.assertEqual(roots[self.real("Code/repo/.git")], "git common dir")
        self.assertEqual(roots[self.real(".delegate/registry")], "run registry")
        self.assertEqual(roots[self.real(".cache")], "home cache")
        self.assertNotIn(self.real("Code/repo"), roots)

    def test_add_dir_roots_go_right_after_exec_and_survive_resume(self):
        roots = (Reopen("/a", "x"), Reopen("/b", "y"))
        fresh = launch.codex_argv_with_work_roots(
            ["codex", "-c", "k=v", "exec", "--cd", "/w", "--sandbox", "workspace-write", "task"],
            roots,
        )
        self.assertEqual(
            fresh[:8], ["codex", "-c", "k=v", "exec", "--add-dir", "/a", "--add-dir", "/b"]
        )
        resume = launch.codex_argv_with_work_roots(
            ["codex", "exec", "--sandbox", "workspace-write", "resume", "sid"], roots
        )
        self.assertLess(resume.index("--add-dir"), resume.index("resume"))

    def test_add_dir_is_not_added_when_codex_bypasses_its_sandbox(self):
        argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "task"]
        self.assertEqual(launch.codex_argv_with_work_roots(argv, (Reopen("/a", "x"),)), argv)

    def test_native_launch_is_none_without_the_sandbox(self):
        self.assertIsNone(
            launch.codex_native_launch(
                WriteGuardSettings(),
                argv=["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
                cwd=str(self.home),
                registry_root=None,
            )
        )


class PreviewTests(HomeTestCase):
    def test_preview_lists_the_plan_for_the_predicted_backend(self):
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
        ):
            payload = launch.preview_payload(
                WriteGuardSettings(),
                engine="cursor",
                argv=["agent"],
                exec_root=str(self.home / "Code/repo"),
                registry_root=None,
                home=str(self.home),
            )
        self.assertEqual(payload["backend"], "bwrap")
        self.assertEqual(payload["status"], "planned")
        self.assertIn(self.real(".ssh"), payload["protected"])

    def test_preview_says_why_there_is_no_guard(self):
        with mock.patch.object(sys, "platform", "darwin"):
            payload = launch.preview_payload(
                WriteGuardSettings(),
                engine="cursor",
                argv=["agent"],
                exec_root=str(self.home),
                registry_root=None,
                home=str(self.home),
            )
        self.assertIsNone(payload["backend"])
        self.assertEqual(payload["status"], "off")
        self.assertIn("macosSeatbelt", payload["reason"])


class PromptNoteTests(unittest.TestCase):
    def test_note_names_the_protected_paths_for_a_protect_list_backend(self):
        note = write_guard.prompt_note(WriteGuardSettings(), write_guard.BACKEND_BWRAP, home="/h")
        self.assertIn("SSH", note)
        self.assertIn("/h/Code", note)
        self.assertIn("read-only or permission error", note)

    def test_note_for_codex_native_says_where_it_may_write(self):
        note = write_guard.prompt_note(WriteGuardSettings(), write_guard.BACKEND_CODEX_NATIVE)
        self.assertIn("Codex", note)
        self.assertIn("git directory", note)

    def test_no_backend_means_no_note(self):
        self.assertIsNone(write_guard.prompt_note(WriteGuardSettings(), None))


class RequestWiringTests(HomeTestCase):
    """The guard settings, --writable and the note travel from the CLI into the request."""

    def setUp(self):
        super().setUp()
        self.delegate = load_delegate()
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), write_guard.ENV_OVERRIDE: "on"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.home / "Code" / "repo"
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

    def build(self, *args, config=None, dry_run=True):
        command = ["--cwd", str(self.repo), "--json"]
        if dry_run:
            command.append("dry-run")
        parsed = parser_api.parse_cli([*command, *args])
        return request_build.request_from_parsed(
            parsed, config or delegate_config.embedded_default_config(), io.StringIO("")
        )

    def with_backend(self):
        return (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
        )

    def test_work_request_carries_settings_and_the_prompt_note(self):
        patches = self.with_backend()
        with patches[0], patches[1]:
            request = self.build("cursor", "work", "fix it")
        self.assertIsNotNone(request.write_guard)
        self.assertTrue(request.write_guard.enabled)
        self.assertIn("Delegate write guard", request.prompt)

    def test_safe_and_call_requests_carry_no_guard(self):
        patches = self.with_backend()
        with patches[0], patches[1]:
            safe = self.build("cursor", "safe", "look")
        self.assertIsNone(safe.write_guard)
        self.assertNotIn("Delegate write guard", safe.prompt)
        with patches[0], patches[1]:
            # Call mode takes no --cwd, so it cannot go through self.build.
            parsed = parser_api.parse_cli(["--json", "dry-run", "codex", "call", "summarize"])
            call = request_build.request_from_parsed(
                parsed, delegate_config.embedded_default_config(), io.StringIO("")
            )
        self.assertIsNone(call.write_guard)
        self.assertNotIn("Delegate write guard", call.prompt)

    def test_disabled_guard_adds_no_note(self):
        with mock.patch.dict(os.environ, {write_guard.ENV_OVERRIDE: "off"}):
            request = self.build("cursor", "work", "fix it")
        self.assertFalse(request.write_guard.enabled)
        self.assertNotIn("Delegate write guard", request.prompt)

    def test_codex_note_says_native_sandbox_when_the_bypass_is_off(self):
        config = delegate_config.embedded_default_config()
        with mock.patch.object(sys, "platform", "darwin"):
            request = self.build("codex", "work", "fix it", config=config)
        self.assertIn("sandboxed by Codex", request.prompt)

    def test_writable_flag_resolves_into_run_writable(self):
        extra = self.home / "Code" / "sibling"
        request = self.build("cursor", "work", "--writable", str(extra), "fix it")
        self.assertEqual(request.write_guard.run_writable, (os.path.realpath(extra),))

    def test_writable_flag_resolves_relative_paths_against_the_cwd(self):
        request = self.build("cursor", "work", "--writable", "../sibling", "fix it")
        self.assertEqual(
            request.write_guard.run_writable, (os.path.realpath(self.home / "Code/sibling"),)
        )

    def test_writable_flag_repeats(self):
        request = self.build(
            "cursor",
            "work",
            "--writable",
            str(self.home / ".ssh"),
            "--writable",
            str(self.home / ".gnupg"),
            "fix it",
        )
        self.assertEqual(len(request.write_guard.run_writable), 2)

    def test_writable_path_must_exist(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            self.build("cursor", "work", "--writable", str(self.home / "missing"), "fix it")
        self.assertEqual(ctx.exception.error, "invalid_writable_path")

    def test_writable_needs_a_value(self):
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "work", "--writable"])
        self.assertEqual(ctx.exception.error, "missing_writable_path")
        with self.assertRaises(error_types.DelegateError) as ctx:
            parser_api.parse_cli(["cursor", "work", "--writable", "", "fix it"])
        self.assertEqual(ctx.exception.error, "missing_writable_path")

    def test_writable_is_refused_outside_work_mode_and_with_pass_through(self):
        for location, args in (
            (["--cwd", str(self.repo)], ("cursor", "safe", "--writable", str(self.home), "x")),
            ([], ("codex", "call", "--writable", str(self.home), "x")),
        ):
            with self.subTest(args=args), self.assertRaises(error_types.DelegateError) as ctx:
                parsed = parser_api.parse_cli([*location, "--json", "dry-run", *args])
                request_build.request_from_parsed(
                    parsed, delegate_config.embedded_default_config(), io.StringIO("")
                )
            self.assertEqual(ctx.exception.error, "invalid_option_combination")
            self.assertIn("work mode", ctx.exception.message)
        with self.assertRaises(error_types.DelegateError) as ctx:
            parsed = parser_api.parse_cli(
                [
                    "--cwd",
                    str(self.repo),
                    "--pass-through",
                    "cursor",
                    "work",
                    "--writable",
                    str(self.home),
                    "x",
                ]
            )
            request_build.request_from_parsed(
                parsed, delegate_config.embedded_default_config(), io.StringIO("")
            )
        self.assertEqual(ctx.exception.error, "invalid_option_combination")
        self.assertIn("pass-through", ctx.exception.message)

    def test_dry_run_payload_shows_the_guard_plan(self):
        (self.home / ".ssh" / "id").write_text("k", encoding="utf-8")
        patches = self.with_backend()
        with patches[0], patches[1]:
            request = self.build("cursor", "work", "--writable", str(self.home / ".gnupg"), "x")
            payload = self.delegate.dry_run_payload(request)
        guard = payload["writeGuard"]
        self.assertEqual(guard["backend"], "bwrap")
        self.assertEqual(guard["status"], "planned")
        self.assertIn(self.real(".ssh"), guard["protected"])
        self.assertNotIn(self.real(".gnupg"), guard["protected"])
        self.assertIn({"path": self.real(".gnupg"), "reason": "--writable"}, guard["writable"])

    def test_safe_dry_run_payload_has_no_guard_section(self):
        request = self.build("cursor", "safe", "look")
        self.assertNotIn("writeGuard", self.delegate.dry_run_payload(request))


class ForbidCommitHookTests(unittest.TestCase):
    """--forbid-commit is refused by git itself, in every isolation mode."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        run_git(self.repo, "add", "seed.txt")
        run_git(self.repo, "commit", "-q", "-m", "seed")
        self.hooks = write_guard.install_forbid_commit_hooks(self.root / "hooks")

    def env(self, base=None):
        env = {k: v for k, v in (base or os.environ).items() if not k.startswith("GIT_CONFIG")}
        env.update(write_guard.forbid_commit_env(str(self.hooks), env))
        return env

    def commit(self, *args, env=None):
        (self.repo / "change.txt").write_text(str(os.urandom(4)), encoding="utf-8")
        run_git(self.repo, "add", "change.txt", env=env)
        return run_git(self.repo, "commit", "-q", "-m", "child", *args, env=env, check=False)

    def head(self):
        return run_git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_every_commit_path_has_its_own_refusing_hook(self):
        # Names are spelled out on purpose: iterating the module constant would pass
        # with any hook silently missing from it. Git runs pre-merge-commit only for
        # a merge that creates a commit, and the other three also fire on that path,
        # so only running each hook itself proves each one refuses.
        for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "pre-merge-commit"):
            with self.subTest(hook=name):
                hook = self.hooks / name
                self.assertTrue(os.access(hook, os.X_OK))
                result = subprocess.run(
                    [str(hook)], capture_output=True, text=True, check=False, cwd=self.repo
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--forbid-commit", result.stderr)

    def test_commit_is_refused_with_the_reason(self):
        before = self.head()
        result = self.commit(env=self.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--forbid-commit", result.stderr)
        self.assertEqual(self.head(), before)

    def test_no_verify_does_not_get_around_it(self):
        before = self.head()
        result = self.commit("--no-verify", env=self.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_merge_commit_is_refused(self):
        run_git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "side.txt").write_text("s", encoding="utf-8")
        run_git(self.repo, "add", "side.txt")
        run_git(self.repo, "commit", "-q", "-m", "side")
        run_git(self.repo, "checkout", "-q", "main")
        (self.repo / "main.txt").write_text("m", encoding="utf-8")
        run_git(self.repo, "add", "main.txt")
        run_git(self.repo, "commit", "-q", "-m", "main")
        before = self.head()
        result = run_git(
            self.repo, "merge", "--no-ff", "-m", "merge", "side", env=self.env(), check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)

    def test_without_the_env_the_same_commit_succeeds(self):
        before = self.head()
        result = self.commit(env=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.head(), before)

    @staticmethod
    def codex_default_filter(env):
        # Codex's default shell_environment_policy drops every variable whose name
        # contains KEY, SECRET or TOKEN (case-insensitive) before a tool call runs.
        # The patterns are in the Codex 0.157.1 binary; a live probe confirmed them.
        return {
            name: value
            for name, value in env.items()
            if not any(marker in name.upper() for marker in ("KEY", "SECRET", "TOKEN"))
        }

    def test_the_child_env_works_after_codex_default_name_filter(self):
        # The regression this guards: an indexed GIT_CONFIG_COUNT/KEY_n/VALUE_n set lost
        # its KEY_n under that filter, and git then failed EVERY command ("missing
        # config key GIT_CONFIG_KEY_0"), status and add included.
        env = self.codex_default_filter(self.env())
        status = run_git(self.repo, "status", "--short", env=env, check=False)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn("missing config key", status.stderr)
        hooks = run_git(self.repo, "config", "--get", "core.hooksPath", env=env)
        self.assertEqual(hooks.stdout.strip(), str(self.hooks))
        before = self.head()
        (self.repo / "change.txt").write_text("x", encoding="utf-8")
        self.assertEqual(
            run_git(self.repo, "add", "change.txt", env=env, check=False).returncode, 0
        )
        result = run_git(self.repo, "commit", "-q", "-m", "child", env=env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--forbid-commit", result.stderr)
        self.assertEqual(self.head(), before)

    def test_no_variable_the_hooks_need_has_a_secret_looking_name(self):
        updates = write_guard.forbid_commit_env(str(self.hooks), {})
        self.assertEqual(set(updates), {"GIT_CONFIG_PARAMETERS"})
        self.assertEqual(updates, self.codex_default_filter(updates))

    def test_a_repo_local_hooks_path_does_not_beat_the_injected_one(self):
        # Why the parameters form and not a per-run GIT_CONFIG_GLOBAL file: a global
        # file loses to the repository's own config (a repo that sets core.hooksPath
        # for its own hook manager would silently re-enable commits).
        run_git(self.repo, "config", "core.hooksPath", str(self.root / "repo-own-hooks"))
        before = self.head()
        result = self.commit(env=self.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--forbid-commit", result.stderr)
        self.assertEqual(self.head(), before)

    def test_existing_git_config_env_is_preserved(self):
        # Old form ('key=value') and new form ('key'='value') entries, plus the indexed
        # variables, all survive untouched next to the injected hooks path.
        base = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "delegate.indexed",
            "GIT_CONFIG_VALUE_0": "Indexed",
            "GIT_CONFIG_PARAMETERS": "'delegate.old=Old Form' 'delegate.new'='New Form'",
        }
        env = write_guard.forbid_commit_env(str(self.hooks), base)
        self.assertEqual(set(env), {"GIT_CONFIG_PARAMETERS"})
        merged = {**base, **env}
        for key, expected in (
            ("delegate.indexed", "Indexed"),
            ("delegate.old", "Old Form"),
            ("delegate.new", "New Form"),
            ("core.hooksPath", str(self.hooks)),
        ):
            with self.subTest(key=key):
                value = run_git(self.repo, "config", "--get", key, env=merged).stdout.strip()
                self.assertEqual(value, expected)

    def test_a_path_with_a_quote_survives_the_parameters_form(self):
        odd = self.root / "it's hooks"
        write_guard.install_forbid_commit_hooks(odd)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
        env.update(write_guard.forbid_commit_env(str(odd), env))
        value = run_git(self.repo, "config", "--get", "core.hooksPath", env=env).stdout.strip()
        self.assertEqual(value, str(odd))

    def test_launch_seam_puts_the_hooks_into_the_child_environment(self):
        run_path = self.root / "run"
        run_path.mkdir()
        out = self.root / "env.txt"
        script = self.root / "child.sh"
        script.write_text(
            f'#!/bin/sh\ngit -C "{self.repo}" config --get core.hooksPath > "{out}"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        with mock.patch.dict(os.environ):
            for name in [n for n in os.environ if n.startswith("GIT_CONFIG")]:
                del os.environ[name]
            process = runner._launch_tracked_process(
                [str(script)],
                str(self.repo),
                stdin_text=None,
                run_path=run_path,
                forbid_commit=True,
            )
            process.communicate(timeout=30)
        hooks_dir = run_path / write_guard.FORBID_COMMIT_HOOKS_DIRNAME
        self.assertEqual(out.read_text(encoding="utf-8").strip(), str(hooks_dir))
        self.assertTrue((hooks_dir / "pre-commit").exists())

    def test_launch_seam_without_forbid_commit_leaves_git_config_alone(self):
        run_path = self.root / "run"
        run_path.mkdir()
        out = self.root / "env.txt"
        script = self.root / "child.sh"
        script.write_text(f'#!/bin/sh\nprintf "%s" "${{GIT_CONFIG_COUNT:-unset}}" > "{out}"\n')
        script.chmod(0o755)
        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_CONFIG_COUNT", None)
            process = runner._launch_tracked_process(
                [str(script)], str(self.repo), stdin_text=None, run_path=run_path
            )
            process.communicate(timeout=30)
        self.assertEqual(out.read_text(encoding="utf-8"), "unset")
        self.assertFalse((run_path / write_guard.FORBID_COMMIT_HOOKS_DIRNAME).exists())


class InPlaceCommitCountTests(unittest.TestCase):
    """The post-exit check that backs the hooks up when there is no worktree."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        run_git(self.repo, "add", "a.txt")
        run_git(self.repo, "commit", "-q", "-m", "a")
        self.base = run_git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def ctx(self, **overrides):
        ctx = mock.Mock()
        ctx.isolation_lifecycle = "none"
        ctx.forbid_commit = True
        ctx.worktree_attachment = None
        ctx.creation_context = {"sourceHeadOid": self.base}
        ctx.execution_cwd = str(self.repo)
        ctx.workspace_kind = "git"
        ctx.started_at = self.started_at
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    def commit(self):
        (self.repo / "b.txt").write_text("b", encoding="utf-8")
        run_git(self.repo, "add", "b.txt")
        run_git(self.repo, "commit", "-q", "-m", "b")

    def test_a_clean_in_place_run_verifies_zero_commits_and_keeps_its_exit_code(self):
        code, extra = runner._final_extra(self.ctx(), 0)
        self.assertEqual(code, 0)
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 0)
        self.assertTrue(extra["commitPolicy"]["verified"])
        self.assertFalse(extra["commitPolicy"]["violated"])

    def test_a_commit_after_launch_fails_the_run(self):
        self.commit()
        code, extra = runner._final_extra(self.ctx(), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["error"], "commit_policy_violated")
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 1)

    def test_a_commit_left_on_a_side_branch_fails_the_run(self):
        run_git(self.repo, "checkout", "-q", "-b", "side")
        self.commit()
        run_git(self.repo, "checkout", "-q", "main")
        self.assertEqual(run_git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.base)
        code, extra = runner._final_extra(self.ctx(), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["error"], "commit_policy_violated")
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 1)

    def test_a_commit_reset_away_fails_the_run(self):
        self.commit()
        run_git(self.repo, "reset", "-q", "--hard", self.base)
        _, extra = runner._final_extra(self.ctx(), 0)
        self.assertTrue(extra["commitPolicyViolated"])
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 1)

    def test_a_worktree_run_also_counts_a_commit_left_behind(self):
        # The worktree summary sees only the final HEAD; the reflog check covers the rest.
        run_git(self.repo, "checkout", "-q", "-b", "side")
        self.commit()
        run_git(self.repo, "checkout", "-q", "main")
        summary = {"commitsCreatedCount": 0}
        with mock.patch.object(runner, "_persistent_work_summary", return_value=summary):
            code, extra = runner._final_extra(self.ctx(isolation_lifecycle="persistent"), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["commitPolicy"]["commitsCreatedCount"], 1)

    def test_moving_to_existing_commits_and_older_history_are_not_counted(self):
        # A commit made before launch, then a checkout and reset after it: nothing created.
        run_git(self.repo, "checkout", "-q", "-b", "older")
        (self.repo / "o.txt").write_text("o", encoding="utf-8")
        run_git(self.repo, "add", "o.txt")
        run_git(self.repo, "commit", "-q", "-m", "older")
        older = run_git(self.repo, "rev-parse", "HEAD").stdout.strip()
        run_git(self.repo, "checkout", "-q", "main")
        later = datetime.now(UTC) + timedelta(seconds=3)
        started_at = later.isoformat().replace("+00:00", "Z")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(later.timestamp()) + 1} +0000"}
        # Reflog entries take their time from the committer-date environment.
        run_git(self.repo, "checkout", "-q", "older", env=env)
        run_git(self.repo, "reset", "-q", "--hard", self.base, env=env)
        run_git(self.repo, "reset", "-q", "--hard", older, env=env)
        ctx = self.ctx(started_at=started_at, creation_context={"sourceHeadOid": older})
        self.assertEqual(runner._commits_made_since_launch(ctx), [])
        self.assertEqual(runner._in_place_commits_created(ctx), 0)

    def test_a_commit_left_behind_before_launch_is_not_counted(self):
        run_git(self.repo, "checkout", "-q", "-b", "earlier")
        self.commit()
        run_git(self.repo, "checkout", "-q", "main")
        later = datetime.now(UTC) + timedelta(seconds=5)
        ctx = self.ctx(started_at=later.isoformat().replace("+00:00", "Z"))
        self.assertEqual(runner._in_place_commits_created(ctx), 0)
        # The same history with the launch before it is a violation.
        self.assertEqual(runner._in_place_commits_created(self.ctx()), 1)

    def test_an_unreadable_launch_time_is_unverified(self):
        self.assertIsNone(runner._in_place_commits_created(self.ctx(started_at=None)))

    def test_an_unverifiable_run_is_reported_not_passed(self):
        code, extra = runner._final_extra(self.ctx(workspace_kind="directory"), 0)
        self.assertEqual(code, 1)
        self.assertEqual(extra["error"], "commit_policy_unverified")

    def test_a_repo_without_commits_at_launch_counts_a_new_one(self):
        with tempfile.TemporaryDirectory() as raw:
            fresh = Path(raw).resolve()
            run_git(fresh, "init", "-q", "-b", "main")
            ctx = self.ctx(creation_context=None, execution_cwd=str(fresh))
            self.assertEqual(runner._in_place_commits_created(ctx), 0)
            (fresh / "f.txt").write_text("f", encoding="utf-8")
            run_git(fresh, "add", "f.txt")
            run_git(fresh, "commit", "-q", "-m", "first")
            self.assertEqual(runner._in_place_commits_created(ctx), 1)


if __name__ == "__main__":
    unittest.main()
