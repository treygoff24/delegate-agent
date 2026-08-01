import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import cli


class PersonaContractTests(unittest.TestCase):
    def _write(self, path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_personas_json_is_golden_sorted_and_reports_shadowed_and_invalid_rows(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "workspace"
            global_dir = Path(tmp) / ".delegate" / "personas"
            self._write(root / ".delegate" / "personas" / "alpha.md", b"local\n")
            self._write(global_dir / "alpha.md", b"global\n")
            self._write(global_dir / "beta.md", b"line\nwith\ttab")
            self._write(root / ".delegate" / "personas" / "bad.md", b"bad\x01")
            self._write(global_dir / "broken.md", b"bad\xff")

            stdout = io.StringIO()
            stderr = io.StringIO()
            code = cli.main(
                ["--json", "--cwd", str(root), "personas"], stdout=stdout, stderr=stderr
            )
            self.assertEqual(code, 0, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(list(payload), ["personas", "schema"])
            self.assertEqual(payload["schema"], "delegate.personas.v1")

            rows = payload["personas"]
            self.assertEqual([row["name"] for row in rows], ["alpha", "bad", "beta", "broken"])
            self.assertEqual(
                set(rows[0]), {"name", "source", "sizeBytes", "preview", "shadowsGlobal"}
            )
            self.assertEqual(
                rows[0],
                {
                    "name": "alpha",
                    "source": "workspace",
                    "sizeBytes": 6,
                    "preview": "local\\n",
                    "shadowsGlobal": True,
                },
            )
            self.assertEqual(set(rows[1]), {"name", "source", "invalid"})
            self.assertIn("invalid_persona_control", rows[1]["invalid"])
            self.assertEqual(set(rows[2]), {"name", "source", "sizeBytes", "preview"})
            self.assertEqual(rows[2]["source"], "global")
            self.assertEqual(rows[2]["preview"], "line\\nwith\\ttab")
            self.assertEqual(set(rows[3]), {"name", "source", "invalid"})
            self.assertIn("invalid_persona_encoding", rows[3]["invalid"])

    def test_personas_json_errors_use_standard_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = cli.main(
                ["--json", "--cwd", str(Path(tmp) / "missing"), "personas"],
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(set(payload), {"ok", "error", "message", "exitCode"})
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "invalid_cwd")
            self.assertEqual(payload["exitCode"], 2)
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
