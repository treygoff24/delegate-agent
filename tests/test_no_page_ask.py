"""Opt-in no-page ``ask`` stub for tracked children, against a real child process."""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import run_registry, runner

CHILD = (
    "import subprocess\n"
    "done = subprocess.run(['ask', 'ship it?'], capture_output=True, text=True)\n"
    "print('ask-rc=%d' % done.returncode, flush=True)\n"
    "print('ask-out=' + done.stdout.strip(), flush=True)\n"
)


class NoPageAskTests(unittest.TestCase):
    def run_child(self, *, opt_in: bool) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            real_ask = fake_bin / "ask"
            real_ask.write_text("#!/bin/sh\necho PAGED\nexit 0\n", encoding="utf-8")
            real_ask.chmod(0o755)
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            run_id, alias = run_registry.register_run(registry_root, harness="codex")
            context = runner.RunContext(
                registry_root=registry_root,
                run_id=run_id,
                alias=alias,
                harness="codex",
                engine="codex",
                mode="work",
                model=None,
                source_cwd=str(workspace),
                execution_cwd=str(workspace),
                workspace_kind="directory",
                isolated_workspace=False,
                started_at=run_registry.utc_now_iso(),
            )
            env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop(runner.NO_PAGE_ASK_ENV, None)
                if opt_in:
                    os.environ[runner.NO_PAGE_ASK_ENV] = "1"
                runner.execute_tracked(
                    [sys.executable, "-c", CHILD],
                    str(workspace),
                    context,
                    json_mode=True,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            run_path = run_registry.run_directory(registry_root, run_id)
            return (run_path / runner.STDOUT_LOG).read_text(encoding="utf-8")

    def test_opted_in_children_get_a_stub_that_never_pages(self):
        output = self.run_child(opt_in=True)
        self.assertIn(f"ask-rc={runner.NO_PAGE_ASK_EXIT}", output)
        self.assertNotIn("PAGED", output)

    def test_without_the_opt_in_the_real_ask_is_untouched(self):
        output = self.run_child(opt_in=False)
        self.assertIn("ask-rc=0", output)
        self.assertIn("ask-out=PAGED", output)


if __name__ == "__main__":
    unittest.main()
