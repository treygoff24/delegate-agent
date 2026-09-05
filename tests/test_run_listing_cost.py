from __future__ import annotations

import tempfile
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


if __name__ == "__main__":
    unittest.main()
