from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, run_status


class RunListingCostTests(unittest.TestCase):
    def test_only_returned_rows_probe_logs_without_losing_totals_or_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = {}
            for number in range(50):
                run_id = f"del_20260905T000000Z_{number:06x}"
                run_path = root / "runs" / run_id
                run_path.mkdir(parents=True)
                run_registry.write_json_atomic(
                    run_path / run_registry.STATE_FILE,
                    {"status": "succeeded", "finishedAt": f"2026-09-05T00:00:{number:02d}Z"},
                )
                run_registry.write_json_atomic(run_path / run_registry.MANIFEST_FILE, {})
                (run_path / run_registry.STDOUT_LOG).write_bytes(b"abc")
                runs[run_id] = {"harness": "codex", "alias": f"codex-{number + 1}"}
            with mock.patch.object(
                run_status, "effective_log_byte_sizes", wraps=run_status.effective_log_byte_sizes
            ) as probes:
                rows, total, scope = run_status.list_run_summaries(root, {"runs": runs}, limit=3)
            self.assertEqual((total, scope), (50, 50))
            self.assertEqual([row["alias"] for row in rows], ["codex-50", "codex-49", "codex-48"])
            self.assertEqual([row["stdoutBytes"] for row in rows], [3, 3, 3])
            self.assertEqual(probes.call_count, 3)

    def test_terminal_projection_bounds_bytes_and_latency_for_large_history(self) -> None:
        """A 30K presentation payload must not be read for every terminal row."""
        with tempfile.TemporaryDirectory() as temporary:
            root = run_registry.ensure_registry(Path(temporary), workspace_kind="directory")
            index = run_registry.load_index(root)
            for number in range(1_000):
                run_id = f"del_20260905T000000Z_{number:06x}"
                run_path = run_registry.run_directory(root, run_id)
                run_path.mkdir(parents=True)
                finished_at = (
                    f"2026-09-05T{number // 3600:02d}:{(number // 60) % 60:02d}:{number % 60:02d}Z"
                )
                state = {
                    "status": "succeeded",
                    "finishedAt": finished_at,
                    "assistantText": "x" * 30_000,
                    "recentEvents": [],
                }
                run_registry.write_json_atomic(run_path / run_registry.STATE_FILE, state)
                run_registry.write_json_atomic(run_path / run_registry.MANIFEST_FILE, {})
                selection = run_registry._terminal_selection(
                    state, run_path / run_registry.STATE_FILE
                )
                assert selection is not None
                index["runs"][run_id] = {
                    "harness": "codex",
                    "alias": f"codex-{number}",
                    run_registry.TERMINAL_SELECTION_KEY: selection,
                }
            run_registry.save_index(root, index)

            bytes_read = 0
            state_loader = run_status.record_io.load_run_state_or_none

            def counted_state_loader(registry_root: Path, run_id: str):
                nonlocal bytes_read
                bytes_read += (
                    (run_registry.run_directory(registry_root, run_id) / run_registry.STATE_FILE)
                    .stat()
                    .st_size
                )
                return state_loader(registry_root, run_id)

            with mock.patch.object(
                run_status.record_io,
                "load_run_state_or_none",
                side_effect=counted_state_loader,
            ):
                started = time.perf_counter()
                rows, total, scope = run_status.list_run_summaries(root, index, limit=3)
                elapsed = time.perf_counter() - started

            self.assertEqual((total, scope), (1_000, 1_000))
            self.assertEqual(
                [row["alias"] for row in rows], ["codex-999", "codex-998", "codex-997"]
            )
            self.assertLess(bytes_read, 100_000)
            self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
