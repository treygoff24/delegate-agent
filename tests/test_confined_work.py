"""Confined work through the tracked CLI, with a local fixture engine."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_write_guard_e2e import _USABLE, _WHY_NOT, EVENTS, GuardE2ETestCase


@unittest.skipUnless(sys.platform.startswith("linux"), "confined work needs Linux")
class ConfinedWorkTests(GuardE2ETestCase):
    # Keep HOME and the sibling visible but read-only; /tmp is a separate case.
    temp_root = "/var/tmp"

    @unittest.skipUnless(_USABLE, _WHY_NOT)
    def test_confined_work_keeps_only_writes_to_the_declared_root(self):
        self.write_config()
        packet = self.workspace / "packet"
        packet.mkdir()
        with tempfile.TemporaryDirectory(prefix="delegate-confined-", dir="/tmp") as temp:
            real_tmp = Path(temp) / "canary"
            (packet / "sibling-link").symlink_to(self.sibling, target_is_directory=True)
            # The same absolute /tmp file, real HOME and sibling directory are
            # writable to an ordinary work lane. Attempt the writes in its child shell.
            self.write_engine(
                "agent",
                "#!/bin/sh\n"
                "/bin/sh -c '\n"
                '  printf kept > "$PWD/in-root"\n'
                '  printf escaped > "$PWD/../outside-packet"\n'
                '  printf escaped > "$GUARD_SIBLING/canary"\n'
                '  printf escaped > "$PWD/sibling-link/via-link"\n'
                f'  printf escaped > "{self.home}/real-home-canary"\n'
                f'  printf escaped > "{real_tmp}"\n'
                '  printf private > "$HOME/private-home" && '
                'printf private > /tmp/private-temp && test "$TMPDIR" = /tmp && '
                'printf yes > "$PWD/private-dirs-work"\n'
                "'\n" + EVENTS,
            )
            completed = self.cli("cursor", "work", "--confined-work", "tidy up", cwd=packet)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual((packet / "in-root").read_text(), "kept")
            outside = [
                self.workspace / "outside-packet",
                self.sibling / "canary",
                self.sibling / "via-link",
                self.home / "real-home-canary",
                real_tmp,
            ]
            self.assertEqual([p.name for p in outside if p.exists()], [])
            self.assertTrue((packet / "private-dirs-work").exists())
            self.assertFalse((self.home / "private-home").exists())
        manifest, _state = self.latest_run()
        self.assertTrue(manifest["confinedWork"])
        self.assertEqual(manifest["writeGuard"]["status"], "enforced")
        self.assertEqual(manifest["writeGuard"]["writeRoot"], str(packet))
        for command in ("resume", "followup"):
            with self.subTest(command=command):
                result = self.cli("--json", command, manifest["alias"])
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("confined_work_replay_unsupported", result.stdout + result.stderr)

    def test_preflight_refusal_never_falls_back_even_when_the_guard_is_disabled(self):
        self.write_config(
            {"isolation": {"writeGuard": {"enabled": False, "onUnavailable": "warn"}}}
        )
        self.write_engine("bwrap", "#!/bin/sh\necho 'namespace denied for test' >&2\nexit 1\n")
        self.write_engine("agent", '#!/bin/sh\nprintf launched > "$PWD/lane-started"\n' + EVENTS)
        result = self.cli("--json", "cursor", "work", "--confined-work", "do not start")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("confined_work_unavailable", result.stdout + result.stderr)
        self.assertIn("namespace denied for test", result.stdout + result.stderr)
        self.assertFalse((self.workspace / "lane-started").exists())
        manifest, _state = self.latest_run()
        self.assertTrue(manifest["confinedWork"])
        self.assertNotIn("writeGuard", manifest)

    def test_dry_run_shows_the_planned_root_without_running_bwrap(self):
        self.write_config()
        result = self.cli("--json", "dry-run", "cursor", "work", "--confined-work", "implement")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["confinedWork"])
        self.assertEqual(plan["writeGuard"]["status"], "planned")
        self.assertEqual(plan["writeGuard"]["writeRoot"], str(self.workspace))

    @unittest.skipUnless(_USABLE, _WHY_NOT)
    def test_input_json_and_persistent_worktree_keep_shared_git_metadata_read_only(self):
        self.write_config()
        self.write_engine(
            "agent",
            '#!/bin/sh\nprintf kept > "$PWD/in-root"\n'
            'git rev-parse --show-toplevel > "$PWD/git-root" || exit 1\n'
            "if git add in-root; then exit 42; fi\n" + EVENTS,
        )
        spec = self.root / "request.json"
        spec.write_text(
            '{"engine":"cursor","mode":"work","confinedWork":true,"isolation":"worktree",'
            '"prompt":"implement"}'
        )
        result = self.cli("--json", "run", "--input-json", str(spec))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        manifest, _state = self.latest_run()
        worktree = Path(manifest["writeGuard"]["writeRoot"])
        self.assertNotEqual(worktree, self.workspace)
        self.assertEqual((worktree / "in-root").read_text(), "kept")
        self.assertEqual((worktree / "git-root").read_text().strip(), str(worktree))
        self.assertFalse((self.workspace / "in-root").exists())
        removed = self.cli("worktree", "remove", manifest["alias"], "--discard-uncommitted")
        self.assertEqual(removed.returncode, 0, removed.stderr + removed.stdout)

    def test_incompatible_options_are_refused_before_start(self):
        self.write_config()
        for args in (
            ("cursor", "safe", "--confined-work", "review"),
            ("--pass-through", "cursor", "work", "--confined-work", "implement"),
            ("cursor", "work", "--confined-work", "--writable", self.sibling, "implement"),
            ("cursor", "work", "implement", "--confined-work"),
            ("codex", "work", "--confined-work", "implement"),
        ):
            with self.subTest(args=args):
                result = self.cli(*map(str, args))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("--confined-work", result.stdout + result.stderr)
