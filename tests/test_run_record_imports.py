import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class RunRecordImportTests(unittest.TestCase):
    def test_status_import_does_not_load_the_mutating_registry(self):
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,delegate_agent.run_status; assert 'delegate_agent.run_registry' not in sys.modules",
            ],
            env={**os.environ, "PYTHONPATH": str(source)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registry_and_status_import_in_either_order_with_same_helpers(self):
        source = Path(__file__).resolve().parents[1] / "src"
        for order in (("run_status", "run_registry"), ("run_registry", "run_status")):
            code = (
                "import importlib,json; "
                f"[importlib.import_module('delegate_agent.'+name) for name in {order!r}]; "
                "from delegate_agent import run_registry,run_status,record_io; "
                "assert run_registry.load_run_state is record_io.load_run_state; "
                "assert run_registry.build_run_summary is run_status.build_run_summary; "
                "print(json.dumps(run_status.status_fields({'status':'succeeded'})))"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                env={**os.environ, "PYTHONPATH": str(source)},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["effectiveStatus"], "succeeded")


if __name__ == "__main__":
    unittest.main()
