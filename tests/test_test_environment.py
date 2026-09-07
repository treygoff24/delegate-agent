import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


class TestEnvironmentTests(unittest.TestCase):
    def test_unittest_and_pytest_imports_share_private_home_and_temp_ownership(self):
        source = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import tests,json,tempfile,os; from pathlib import Path; "
                "print(json.dumps({'home':str(Path.home()),'temp':tempfile.gettempdir(),"
                "'env':[os.environ.get(k) for k in ('TMPDIR','TMP','TEMP')],"
                "'attempt':os.environ.get('DELEGATE_WORKFLOW_ATTEMPT'),"
                "'stall':os.environ.get('DELEGATE_STALL_MINUTES')}))",
            ],
            cwd=source,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DELEGATE_WORKFLOW_ATTEMPT": "/unavailable-parent-attempt",
                "DELEGATE_STALL_MINUTES": "999",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        root = Path(payload["home"]).parent
        self.assertEqual(Path(payload["temp"]).parent, root)
        self.assertEqual(payload["env"], [payload["temp"]] * 3)
        self.assertIsNone(payload["attempt"])
        self.assertIsNone(payload["stall"])
        self.assertFalse(root.exists(), "suite exit must clean only its private root")
        self.assertNotEqual(payload["home"], os.environ["HOME"])


if __name__ == "__main__":
    unittest.main()
