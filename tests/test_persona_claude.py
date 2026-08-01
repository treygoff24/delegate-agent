from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.delegate_commands_test_base import CommandTestBase


class PersonaClaudeTests(CommandTestBase):
    _SENTINEL = "CLAUDE PERSONA PRIVATE SENTINEL"

    def _request(self, discovery: dict[str, object]):
        config = self.delegate.DEFAULT_CONFIG
        with mock.patch.object(
            self.delegate.harness_discovery,
            "load_discovery_cache",
            return_value=discovery,
        ):
            return self.build_git_request(
                "claude",
                "work",
                None,
                "/repo",
                "review task",
                config,
                False,
                persona="editor",
                persona_text_override=self._SENTINEL,
            )

    @staticmethod
    def _native_discovery() -> dict[str, object]:
        return {"harnesses": {"claude": {"personaTransports": {"native-file": True}}}}

    def test_native_persona_materializes_private_file_and_keeps_body_out_of_surfaces(self):
        request = self._request(self._native_discovery())
        self.assertEqual(request.persona_transport, "native-file")
        self.assertNotIn(self._SENTINEL, request.argv)
        self.assertNotIn(self._SENTINEL, json.dumps(self.delegate.dry_run_payload(request)))

        with tempfile.TemporaryDirectory() as tmp:
            registry_root = Path(tmp) / "registry"
            context = self.delegate.make_run_context(
                registry_root,
                request,
                run_id="del_20260731T120000Z_abcdef",
                alias="quiet-otter",
                source_workspace=self.delegate.ResolvedWorkspace("/repo", "git"),
            )
            manifest = self.delegate.delegate_runner.build_manifest(context, request.display_argv)
            self.assertNotIn(self._SENTINEL, json.dumps(manifest))

            launch_argv, temp_dir = self.delegate.delegate_runner._materialize_prompt_file_argv(
                request.argv,
                prompt_file_text=None,
                prompt_file_placeholder=None,
                persona_file_text=request.persona_text,
                persona_file_placeholder=self.delegate.PERSONA_FILE_ARG_PLACEHOLDER,
            )
            try:
                persona_path = Path(
                    launch_argv[launch_argv.index("--append-system-prompt-file") + 1]
                )
                self.assertEqual(persona_path.read_text(encoding="utf-8"), self._SENTINEL)
                self.assertEqual(persona_path.stat().st_mode & 0o777, 0o600)
            finally:
                if temp_dir is not None:
                    shutil.rmtree(temp_dir)

    def test_unproven_capability_uses_prepend_and_avoids_native_file_flag(self):
        request = self._request({"harnesses": {"claude": {}}})

        self.assertEqual(request.persona_transport, "prepend")
        self.assertIn(self._SENTINEL, request.stdin_text or "")
        self.assertNotIn("--append-system-prompt-file", request.argv)
        self.assertEqual(
            request.warnings,
            ("claude native-file persona transport was not proven by discovery; using prepend.",),
        )


if __name__ == "__main__":
    unittest.main()
