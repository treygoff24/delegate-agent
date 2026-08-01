from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import config, prompt_transport, request_build, run_registry
from delegate_agent.cli import parse_cli, request_from_parsed
from delegate_agent.errors import DelegateError
from delegate_agent.workflows import runtime as workflow_runtime


class PersonaSizeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.real_persona_path = Path.home() / ".delegate" / "personas" / "editor.md"
        self.real_persona_bytes = (
            self.real_persona_path.read_bytes() if self.real_persona_path.exists() else None
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(Path(self.temp.name) / "home")})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.persona_text = "Use the reviewer voice. é🌍\n"
        persona_dir = Path.home() / ".delegate" / "personas"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "editor.md").write_text(self.persona_text, encoding="utf-8")

    def tearDown(self) -> None:
        current = self.real_persona_path.read_bytes() if self.real_persona_path.exists() else None
        self.assertEqual(
            current, self.real_persona_bytes, "tests must not alter the real HOME persona"
        )

    def _framed_prompt_bytes(self, prompt: str) -> int:
        return len(
            request_build.effective_prompt(
                prompt,
                engine="cursor",
                mode="work",
                completion_report_mode=config.COMPLETION_REPORT_MODE_MARKDOWN,
                persona_text=self.persona_text,
            ).encode("utf-8")
        )

    def _boundary_prompt(self) -> tuple[str, str]:
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        overhead = self._framed_prompt_bytes("x") - 1
        exact = "x" * (limit - overhead)
        over = exact + "x"
        self.assertEqual(self._framed_prompt_bytes(exact), limit)
        self.assertEqual(self._framed_prompt_bytes(over), limit + 1)
        return exact, over

    def _cli_request(self, prompt: str):
        parsed = parse_cli(
            [
                "--cwd",
                str(self.workspace),
                "cursor",
                "work",
                "--persona",
                "editor",
                prompt,
            ]
        )
        return request_from_parsed(
            parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )

    def _input_json_request(self, prompt: str):
        input_path = self.workspace / "input.json"
        input_path.write_text(
            json.dumps(
                {
                    "engine": "cursor",
                    "mode": "work",
                    "cwd": str(self.workspace),
                    "prompt": prompt,
                    "persona": "editor",
                }
            ),
            encoding="utf-8",
        )
        parsed = parse_cli(["--cwd", str(self.workspace), "run", "--input-json", str(input_path)])
        return request_from_parsed(
            parsed,
            config.embedded_default_config(),
            io.StringIO(),
            stderr=io.StringIO(),
        )

    def test_cli_full_framed_utf8_persona_prompt_boundary(self) -> None:
        exact, over = self._boundary_prompt()
        request = self._cli_request(exact)
        self.assertEqual(
            len(request.prompt.encode("utf-8")), prompt_transport.ARGV_PROMPT_GUARD_BYTES
        )
        with self.assertRaises(DelegateError) as caught:
            self._cli_request(over)
        self.assertEqual(caught.exception.error, "prompt_too_large")

    def test_input_json_full_framed_utf8_persona_prompt_boundary(self) -> None:
        exact, over = self._boundary_prompt()
        request = self._input_json_request(exact)
        self.assertEqual(
            len(request.prompt.encode("utf-8")), prompt_transport.ARGV_PROMPT_GUARD_BYTES
        )
        with self.assertRaises(DelegateError) as caught:
            self._input_json_request(over)
        self.assertEqual(caught.exception.error, "prompt_too_large")

    def _workflow_dry_run(self, prompt: str) -> dict[str, object]:
        root = self.workspace / ".delegate" / "workflows" / "wf_aaaaaaaaaaaa"
        run_registry.ensure_private_dir(root)
        state = workflow_runtime.WorkflowState(
            wf_id="wf_aaaaaaaaaaaa",
            workspace=self.workspace,
            root=root,
            script_path=self.workspace / "workflow.py",
            config=config.embedded_default_config(),
            cli_argv=[],
            args=None,
            budget=workflow_runtime.Budget(None),
            dry_run=True,
        )
        workflow_runtime.WorkflowDsl(
            state, {"defaults": {"engine": "cursor", "mode": "safe"}}
        ).agent(
            prompt,
            engine="cursor",
            mode="safe",
            persona="editor",
        )
        self.assertEqual(len(state.dry_runs), 1)
        return state.dry_runs[0]

    def test_workflow_guard_counts_persona_bytes_at_shared_boundary(self) -> None:
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        persona_bytes = len(self.persona_text.encode("utf-8"))
        exact = "x" * (limit - persona_bytes)
        over = exact + "x"

        exact_entry = self._workflow_dry_run(exact)
        self.assertEqual(exact_entry["promptBytes"], limit)
        self.assertNotIn("warnings", exact_entry)

        over_entry = self._workflow_dry_run(over)
        self.assertEqual(over_entry["promptBytes"], limit + 1)
        self.assertIn("warnings", over_entry)
        self.assertEqual(workflow_runtime.PROMPT_ARGV_GUARD_BYTES, limit)

    def test_workflow_guard_defers_full_framed_prompt_overflow_to_final_materialization(
        self,
    ) -> None:
        limit = prompt_transport.ARGV_PROMPT_GUARD_BYTES
        overhead = (
            len(
                request_build.effective_prompt(
                    "x",
                    engine="cursor",
                    mode="safe",
                    completion_report_mode=config.COMPLETION_REPORT_MODE_MARKDOWN,
                    persona_text=self.persona_text,
                ).encode("utf-8")
            )
            - 1
        )
        over = "x" * (limit - overhead + 1)
        entry = self._workflow_dry_run(over)
        self.assertLess(entry["promptBytes"], limit)
        self.assertNotIn("warnings", entry)


if __name__ == "__main__":
    unittest.main()
