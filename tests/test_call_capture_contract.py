"""CLI receipt for a local OMP-shaped child; no provider request is made."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli, config
from delegate_agent import request_build as request_api
from tests.test_omp_output_capture import final_line, thinking_line


class CallCaptureContractTests(unittest.TestCase):
    def test_call_json_reports_real_compacted_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake-omp"
            executable.write_text(
                "#!/usr/bin/env python3\nimport os\n"
                f"for _ in range(30): os.write(1,{thinking_line()!r})\n"
                f"os.write(1,{final_line()!r})\n"
            )
            executable.chmod(0o755)
            cfg = config.embedded_default_config()
            cfg["omp"]["binary"] = str(executable)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(request_api, "load_config", return_value=(cfg, "fixture")):
                code = cli.main(["--json", "omp", "call", "Fixture prompt"], stdout=out, stderr=err)
            self.assertEqual(code, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["text"], "completed after bounded telemetry")
            capture = payload["stdoutCapture"]
            self.assertEqual(capture["scope"], "final-attempt")
            self.assertTrue(capture["truncated"])
            self.assertGreater(capture["omittedThinkingRecords"], 0)
            self.assertGreater(capture["transportBytes"], capture["capturedBytes"])
            self.assertEqual(capture["capturedBytes"], payload["stdoutBytes"])
            self.assertTrue(any("raw stdout is incomplete" in w for w in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
