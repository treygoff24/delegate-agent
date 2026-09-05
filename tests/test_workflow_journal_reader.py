from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent.workflows import commands, registry, runtime


class WorkflowJournalReaderTests(unittest.TestCase):
    def test_terminal_watch_keeps_a_valid_final_record_without_newline(self) -> None:
        self.path.write_bytes(b'{"seq":1,"type":"workflow_completed"}')
        self.status("succeeded")
        expected = registry.iter_journal(self.path)
        for jsonl in (False, True):
            with self.subTest(jsonl=jsonl):
                out = io.StringIO()
                commands.emit_watch(
                    commands.WorkflowCommand(
                        "watch", wf_id=self.wf_id, json_mode=True, jsonl=jsonl
                    ),
                    workspace=self.workspace,
                    stdout=out,
                )
                if jsonl:
                    records = [json.loads(line) for line in out.getvalue().splitlines()]
                    actual = [record["event"] for record in records if record["type"] == "event"]
                    self.assertEqual(records[-1]["lastSeq"], 1)
                else:
                    payload = json.loads(out.getvalue())
                    actual = payload["events"]
                    self.assertEqual(payload["lastSeq"], 1)
                self.assertEqual(actual, expected)

    def test_terminal_watch_warns_and_ignores_an_incomplete_final_record(self) -> None:
        self.path.write_bytes(b'{"seq":1}\n{"seq":')
        self.status("stalled")
        out = io.StringIO()
        with self.assertWarnsRegex(RuntimeWarning, "truncated final workflow journal"):
            commands.emit_watch(
                commands.WorkflowCommand("watch", wf_id=self.wf_id, json_mode=True),
                workspace=self.workspace,
                stdout=out,
            )
        self.assertEqual(json.loads(out.getvalue())["events"], [{"seq": 1}])

    def test_truncated_utf8_tail_is_ignored_only_when_writer_is_settled(self) -> None:
        self.path.write_bytes(b'{"seq":1}\n{"seq":2,"text":"\xc3')
        reader = registry.JournalReader(self.path)
        self.assertEqual(list(reader.read_events()), [{"seq": 1}])
        with self.assertWarnsRegex(RuntimeWarning, "truncated final workflow journal"):
            self.assertEqual(list(reader.read_events(final=True)), [])
        with self.assertWarnsRegex(RuntimeWarning, "truncated final workflow journal"):
            self.assertEqual(registry.iter_journal(self.path), [{"seq": 1}])
        self.path.write_bytes(b'{"seq":1}\n{"seq":2,"text":"\xff"}\n')
        with self.assertRaises(UnicodeDecodeError):
            registry.iter_journal(self.path)
        with self.assertRaises(UnicodeDecodeError):
            list(registry.JournalReader(self.path).read_events(final=True))

    def test_liveness_probe_is_read_only_for_unheld_and_held_locks(self) -> None:
        lock = self.root / registry.LOCK_FILE
        lock.write_bytes(b"")
        lock.chmod(0o644)
        self.assertFalse(registry.supervisor_alive(self.root))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(registry.supervisor_alive(self.root))
            self.assertEqual(lock.stat().st_mode & 0o777, 0o644)

    def test_nonregular_lock_is_unknown_not_proof_of_a_dead_supervisor(self) -> None:
        os.mkfifo(self.root / registry.LOCK_FILE)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import tests; from pathlib import Path; from delegate_agent.workflows import registry; "
                f"print(registry.supervisor_alive(Path({str(self.root)!r})))",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.wf_id = "wf_123456abcdef"
        self.root = registry.ensure_workflow_dir(self.workspace, self.wf_id)
        self.path = self.root / registry.JOURNAL_FILE

    def append(self, seq: int, **fields: object) -> None:
        registry.append_jsonl(self.path, {"seq": seq, **fields})

    def status(self, status: str, **fields: object) -> None:
        registry.write_json(
            self.root / registry.STATUS_FILE, {"wfId": self.wf_id, "status": status, **fields}
        )

    def test_incremental_append_partial_replacement_and_truncation(self) -> None:
        reader = registry.JournalReader(self.path)
        self.assertEqual(list(reader.read_events()), [])
        self.append(1)
        self.assertEqual(list(reader.read_events()), [{"seq": 1}])
        offset = reader.offset
        self.assertEqual(list(reader.read_events()), [])
        self.assertEqual(reader.offset, offset)
        with self.path.open("ab") as handle:
            handle.write(b'{"seq": 2, "text": "\xc3')
        self.assertEqual(list(reader.read_events()), [])
        with self.path.open("ab") as handle:
            handle.write(b'\xa9"}')
        self.assertEqual(list(reader.read_events()), [], "unterminated JSON waits for newline")
        with self.path.open("ab") as handle:
            handle.write(b"\n")
        self.assertEqual(list(reader.read_events()), [{"seq": 2, "text": "\u00e9"}])
        replacement = self.root / "replacement"
        replacement.write_text('{"seq": 3}\n')
        replacement.replace(self.path)
        self.assertEqual(list(reader.read_events()), [{"seq": 3}])
        self.path.write_text('{"seq": 4}\n')
        self.assertEqual(list(reader.read_events()), [{"seq": 4}])
        self.path.write_text("{}\n")
        self.assertEqual(list(reader.read_events()), [{}])

    def test_malformed_complete_line_is_not_silently_skipped(self) -> None:
        self.path.write_bytes(b'{"seq": 1}\nnot json\n{"seq": 3}\n')
        reader = registry.JournalReader(self.path)
        events = reader.read_events()
        self.assertEqual(next(events), {"seq": 1})
        with self.assertRaises(json.JSONDecodeError):
            next(events)
        with self.assertRaises(json.JSONDecodeError):
            list(reader.read_events())

    def test_polls_read_only_boundary_and_new_bytes(self) -> None:
        self.path.write_bytes(b'{"seq": 1, "text": "' + b"x" * 100_000 + b'"}\n')
        reader = registry.JournalReader(self.path)
        self.assertEqual(len(list(reader.read_events())), 1)
        original_open = Path.open
        bytes_read = []

        class Counted:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def read(self, count=-1):
                value = self.handle.read(count)
                bytes_read.append(len(value))
                return value

            def readline(self):
                value = self.handle.readline()
                bytes_read.append(len(value))
                return value

        with mock.patch.object(
            Path, "open", lambda path, *a, **kw: Counted(original_open(path, *a, **kw))
        ):
            for _ in range(5):
                self.assertEqual(list(reader.read_events()), [])
        self.assertEqual(sum(bytes_read), 5 * 64)
        self.append(2)
        self.assertEqual(list(reader.read_events()), [{"seq": 2}])
        with self.path.open("ab") as handle:
            handle.write(b'{"seq": 3, "text": "' + b"y" * 100_000)
        self.assertEqual(list(reader.read_events()), [])
        bytes_read.clear()
        with mock.patch.object(
            Path, "open", lambda path, *a, **kw: Counted(original_open(path, *a, **kw))
        ):
            for _ in range(5):
                self.assertEqual(list(reader.read_events()), [])
        self.assertEqual(sum(bytes_read), 5 * 64, "partial tails are buffered, not reread")
        with self.path.open("ab") as handle:
            handle.write(b'"}\n')
        self.assertEqual(list(reader.read_events()), [{"seq": 3, "text": "y" * 100_000}])

    def test_startup_reads_journal_once_and_preserves_sequence_and_replay(self) -> None:
        for include_simulated in (False, True):
            for last_seq in (1, 20):
                with self.subTest(include_simulated=include_simulated, last_seq=last_seq):
                    self.path.write_bytes(b"")
                    self.append(3, type="agent_finished", key="real", result={"ok": True})
                    self.append(8, type="agent_finished", key="legacy", dryRun=True, result="stub")
                    self.append(
                        10, type="agent_finished", key="simulated", simulated=True, result="ignored"
                    )
                    self.status("running", lastSeq=last_seq)
                    with mock.patch.object(
                        registry, "iter_journal", wraps=registry.iter_journal
                    ) as reads:
                        state = runtime.WorkflowState(
                            wf_id=self.wf_id,
                            workspace=self.workspace,
                            root=self.root,
                            script_path=self.root / registry.SCRIPT_FILE,
                            config={},
                            cli_argv=["delegate"],
                            args=None,
                            budget=runtime.Budget(None),
                            replay_journal=include_simulated,
                        )
                    reads.assert_called_once_with(self.path)
                    self.assertEqual(state.sequence, max(10, last_seq))
                    self.assertEqual(state.replay["real"], {"ok": True})
                    self.assertEqual("legacy" in state.replay, include_simulated)
                    self.assertNotIn("simulated", state.replay)

    def test_watch_streams_active_appends_and_keeps_legacy_json_envelope(self) -> None:
        for jsonl in (False, True):
            with self.subTest(jsonl=jsonl):
                self.path.write_bytes(b"")
                self.append(1)
                self.status("running")
                out = io.StringIO()

                def advance(_delay, *, jsonl=jsonl, out=out):
                    if jsonl:
                        self.assertEqual(json.loads(out.getvalue())["type"], "event")
                    else:
                        self.assertEqual(out.getvalue(), "")
                    self.append(2)
                    self.status("succeeded")

                with (
                    mock.patch.object(registry, "supervisor_alive", return_value=True),
                    mock.patch.object(commands.time, "sleep", side_effect=advance),
                ):
                    code = commands.emit_watch(
                        commands.WorkflowCommand(
                            "watch", wf_id=self.wf_id, json_mode=True, jsonl=jsonl
                        ),
                        workspace=self.workspace,
                        stdout=out,
                    )
                self.assertEqual(code, 0)
                if jsonl:
                    rows = [json.loads(line) for line in out.getvalue().splitlines()]
                    self.assertEqual([row["type"] for row in rows], ["event", "event", "final"])
                    self.assertEqual([row["event"]["seq"] for row in rows[:-1]], [1, 2])
                    self.assertEqual(rows[-1]["workflow"]["status"], "succeeded")
                    self.assertNotIn("events", rows[-1])
                else:
                    payload = json.loads(out.getvalue())
                    self.assertEqual(
                        payload,
                        {
                            "ok": True,
                            "schema": commands.WORKFLOW_COMMAND_SCHEMA,
                            "events": [{"seq": 1}, {"seq": 2}],
                            "lastSeq": 2,
                        },
                    )

    def test_decision_projection_agrees_for_status_list_and_wait(self) -> None:
        for status, wait_code in (
            ("succeeded", 0),
            ("paused", 0),
            ("failed", 1),
            ("killed", 1),
            ("dry_run", 1),
            ("running", 1),
        ):
            with self.subTest(status=status):
                self.status(
                    status,
                    gateKey="gate" if status == "paused" else None,
                    gateResultHash="hash",
                    error="failure" if status == "failed" else None,
                    budget={"total": 4, "spent": 2, "remaining": 2},
                )
                out = io.StringIO()
                commands.emit_status(
                    commands.WorkflowCommand("status", wf_id=self.wf_id, json_mode=True),
                    workspace=self.workspace,
                    stdout=out,
                )
                view = json.loads(out.getvalue())
                out = io.StringIO()
                commands.emit_list(
                    commands.WorkflowCommand("list", json_mode=True),
                    workspace=self.workspace,
                    stdout=out,
                )
                listed = json.loads(out.getvalue())["workflows"][0]
                out = io.StringIO()
                code = commands.emit_wait(
                    commands.WorkflowCommand("wait", wf_id=self.wf_id, json_mode=True),
                    workspace=self.workspace,
                    stdout=out,
                )
                waited = json.loads(out.getvalue())["workflow"]
                self.assertEqual(code, wait_code)
                self.assertEqual(view["decision"], listed["decision"])
                self.assertEqual(view["decision"], waited["decision"])
                self.assertEqual(view["decision"]["budget"]["remaining"], 2)
                self.assertTrue(view["decision"]["nextActions"])


if __name__ == "__main__":
    unittest.main()
