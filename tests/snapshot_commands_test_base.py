import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.delegate_fixtures import write_snapshot_run

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
CLI_PATH = ROOT / "src" / "delegate_agent" / "cli.py"
REGISTRY_PATH = ROOT / "src" / "delegate_agent" / "run_registry.py"
RENDERING_PATH = ROOT / "src" / "delegate_agent" / "rendering.py"
REDACTION_PATH = ROOT / "src" / "delegate_agent" / "redaction.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SnapshotCommandTestBase(unittest.TestCase):
    def setUp(self):
        self.delegate = load_module(CLI_PATH, "delegate_cli_snapshot_test")
        self.registry = load_module(REGISTRY_PATH, "delegate_registry_snapshot_test")
        self.rendering = load_module(RENDERING_PATH, "delegate_rendering_snapshot_test")
        self.redaction = load_module(REDACTION_PATH, "delegate_redaction_snapshot_test")
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = self.registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_run(
        self,
        *,
        harness: str = "cursor",
        status: str = "running",
        assistant_text: str = "planning the change",
        stdout_bytes: int = 0,
        stderr_bytes: int = 0,
        pid: int | None = os.getpid(),
        started_at: str | None = None,
    ) -> tuple[str, str]:
        return write_snapshot_run(
            self.registry,
            self.registry_root,
            self.workspace,
            harness=harness,
            status=status,
            assistant_text=assistant_text,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            pid=pid,
            started_at=started_at,
        )
