from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from delegate_agent import registry_roster, run_registry  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402


class RegistryRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.base = base
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def workspace(self, name: str) -> Path:
        path = self.base / name
        path.mkdir()
        return path

    def registry(self, name: str) -> Path:
        workspace = self.workspace(name)
        run_registry.ensure_registry(workspace, workspace_kind="directory")
        return workspace

    def roster_entries(self) -> list[dict]:
        return json.loads(registry_roster.roster_path().read_text(encoding="utf-8"))["workspaces"]

    def test_ensure_registry_appends_the_workspace_privately(self) -> None:
        workspace = self.registry("alpha")

        entries = self.roster_entries()
        self.assertEqual([entry["path"] for entry in entries], [str(workspace)])
        mode = stat.S_IMODE(registry_roster.roster_path().stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(registry_roster.known_workspaces(), [workspace])

    def test_workflow_directory_creation_also_registers_the_workspace(self) -> None:
        workspace = self.workspace("wf-only")

        workflow_registry.ensure_workflow_dir(workspace, "wf_0123456789ab")

        self.assertEqual(registry_roster.known_workspaces(), [workspace])

    def test_most_recent_workspace_comes_first_and_repeats_do_not_duplicate(self) -> None:
        first = self.registry("first")
        second = self.registry("second")
        registry_roster.note_workspace(first, now=4_000_000_000)

        self.assertEqual(registry_roster.known_workspaces(), [first, second])
        registry_roster.note_workspace(first, now=4_000_000_100)
        self.assertEqual(len(self.roster_entries()), 2)

    def test_a_fresh_entry_is_not_rewritten(self) -> None:
        workspace = self.registry("alpha")

        with mock.patch.object(registry_roster.private_io, "write_json_atomic") as write:
            registry_roster.note_workspace(workspace)

        write.assert_not_called()

    def test_roster_is_bounded_and_drops_the_least_recent(self) -> None:
        with mock.patch.object(registry_roster, "ROSTER_LIMIT", 3):
            workspaces = [self.registry(f"ws-{index}") for index in range(5)]

            entries = self.roster_entries()

        self.assertEqual(len(entries), 3)
        self.assertEqual(
            [entry["path"] for entry in entries],
            [str(path) for path in reversed(workspaces[2:])],
        )

    def test_a_deleted_registry_is_skipped_on_read_and_dropped_on_write(self) -> None:
        gone = self.registry("gone")
        kept = self.registry("kept")
        shutil.rmtree(gone / ".delegate")

        self.assertEqual(registry_roster.known_workspaces(), [kept])
        self.assertEqual(registry_roster.find_run("del_20260101T000000Z_abcdef"), [])
        newest = self.registry("newest")
        self.assertEqual(
            [entry["path"] for entry in self.roster_entries()], [str(newest), str(kept)]
        )

    def test_a_corrupt_roster_reads_as_empty_and_is_repaired_by_the_next_note(self) -> None:
        workspace = self.registry("alpha")
        registry_roster.roster_path().write_text("{not json", encoding="utf-8")

        self.assertEqual(registry_roster.known_workspaces(), [])
        registry_roster.note_workspace(workspace)

        self.assertEqual(registry_roster.known_workspaces(), [workspace])

    def test_noting_never_raises_when_home_is_unwritable(self) -> None:
        workspace = self.workspace("alpha")
        with mock.patch.object(
            registry_roster.private_io, "write_json_atomic", side_effect=PermissionError("ro")
        ):
            registry_roster.note_workspace(workspace)

        self.assertEqual(registry_roster.known_workspaces(), [])

    def test_find_run_names_only_other_registries_that_hold_the_run(self) -> None:
        here = self.registry("here")
        there = self.registry("there")
        run_id, alias = run_registry.register_run(there / ".delegate", harness="codex")

        matches = registry_roster.find_run(run_id, exclude=here)

        self.assertEqual([(match.workspace, match.alias) for match in matches], [(there, alias)])
        self.assertEqual(registry_roster.find_run(run_id, exclude=there), [])

    def test_find_alias_lists_every_registry_that_has_it(self) -> None:
        here = self.registry("here")
        left = self.registry("left")
        right = self.registry("right")
        left_id, alias = run_registry.register_run(left / ".delegate", harness="codex")
        right_id, right_alias = run_registry.register_run(right / ".delegate", harness="codex")
        self.assertEqual(alias, right_alias)

        matches = registry_roster.find_alias(alias, exclude=here)

        self.assertEqual(
            sorted((match.workspace, match.run_id) for match in matches),
            sorted([(left, left_id), (right, right_id)]),
        )

    def test_find_workflow_looks_in_workflow_stores(self) -> None:
        here = self.registry("here")
        there = self.workspace("there")
        workflow_registry.ensure_workflow_dir(there, "wf_0123456789ab")

        matches = registry_roster.find_workflow("wf_0123456789ab", exclude=here)

        self.assertEqual([match.workspace for match in matches], [there])
        self.assertEqual(registry_roster.find_workflow("wf_ffffffffffff", exclude=here), [])

    def test_describe_matches_names_workspace_and_exact_command_for_one_run(self) -> None:
        there = self.base / "there"
        match = registry_roster.RosterMatch(there, run_id="del_20260101T000000Z_abcdef")

        text, actions = registry_roster.describe_matches(
            "del_20260101T000000Z_abcdef", [match], command="run-output"
        )

        expected = f"delegate --cwd {there} run-output del_20260101T000000Z_abcdef"
        self.assertIn(f"recorded in workspace {there}", text)
        self.assertIn(expected, text)
        self.assertEqual(actions, [expected])

    def test_describe_matches_refuses_to_pick_an_alias(self) -> None:
        one = registry_roster.RosterMatch(self.base / "one", run_id="del_20260101T000000Z_aaaaaa")
        two = registry_roster.RosterMatch(self.base / "two", run_id="del_20260101T000000Z_bbbbbb")

        text, actions = registry_roster.describe_matches("codex-1", [one, two], command="snapshot")

        self.assertIn("not unique", text)
        self.assertIn(str(self.base / "one"), text)
        self.assertIn(str(self.base / "two"), text)
        self.assertEqual(len(actions), 2)


if __name__ == "__main__":
    unittest.main()
