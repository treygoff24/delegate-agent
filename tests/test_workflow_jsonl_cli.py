import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from delegate_agent import cli, cli_parser, command_help
from delegate_agent.workflows import commands, registry


class WorkflowJsonlCliTests(unittest.TestCase):
    def test_watch_jsonl_flag_is_discoverable_and_watch_only(self):
        parsed = cli_parser.parse_cli(["workflow", "watch", "wf_123456abcdef", "--jsonl"])
        self.assertTrue(parsed.workflow_command.jsonl)
        self.assertIn(
            "--jsonl", {o.flag for o in command_help.COMMAND_SPECS["workflow watch"].options}
        )
        for action in ("status", "events", "wait", "result", "approve", "run"):
            with self.subTest(action=action), self.assertRaises(cli.DelegateError):
                cli_parser.parse_cli(["workflow", action, "wf_123456abcdef", "--jsonl"])

    def test_cli_jsonl_emits_filtered_events_and_final_state(self):
        with tempfile.TemporaryDirectory() as workspace:
            wf_id = "wf_123456abcdef"
            root = registry.ensure_workflow_dir(Path(workspace), wf_id)
            registry.write_json(root / registry.STATUS_FILE, {"wfId": wf_id, "status": "succeeded"})
            for seq in (1, 2):
                registry.append_jsonl(root / registry.JOURNAL_FILE, {"seq": seq, "type": "test"})
            out, err = io.StringIO(), io.StringIO()
            code = cli.main(
                [
                    "--cwd",
                    workspace,
                    "--json",
                    "workflow",
                    "watch",
                    wf_id,
                    "--jsonl",
                    "--since",
                    "1",
                ],
                stdout=out,
                stderr=err,
            )
            self.assertEqual(code, 0, err.getvalue())
            records = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([r["type"] for r in records], ["event", "final"])
            self.assertEqual(records[0]["event"]["seq"], 2)
            self.assertEqual(records[1]["lastSeq"], 2)
            self.assertEqual(records[1]["workflow"]["status"], "succeeded")

    def test_recovery_actions_are_executable_and_use_the_validated_workflow_path(self):
        with tempfile.TemporaryDirectory(prefix="delegate space ") as workspace:
            root = registry.ensure_workflow_dir(Path(workspace), "wf_123456abcdef")
            view = commands._status_view(root, {"wfId": "wrong; do-not-run", "status": "failed"})
            actions = [shlex.split(action) for action in view["decision"]["nextActions"]]
            for argv in actions:
                self.assertEqual(argv[:4], ["delegate", "--cwd", workspace, "workflow"])
                self.assertIn("wf_123456abcdef", argv)
                parsed = cli_parser.parse_cli(argv[1:])
                self.assertEqual(parsed.global_options.cwd, workspace)

    def test_incremental_reader_keeps_utf8_bom_refusal(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "journal.jsonl"
            path.write_bytes(b'\xef\xbb\xbf{"seq": 1}\n')
            with self.assertRaises(json.JSONDecodeError):
                list(registry.JournalReader(path).read_events())


if __name__ == "__main__":
    unittest.main()
