from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delegate_agent import account_binding
from delegate_agent.cli import dry_run_payload
from delegate_agent.request_models import Request
from delegate_agent.runner import RunContext, completion_json_payload


class CursorAccountBindingTests(unittest.TestCase):
    def write_status_command(self, root: Path, *, authenticated: bool = True) -> tuple[str, ...]:
        script = root / "cursor-agent"
        status = {
            "status": "authenticated" if authenticated else "unauthenticated",
            "isAuthenticated": authenticated,
            "hasAccessToken": authenticated,
            "hasRefreshToken": authenticated,
            "userInfo": {
                "email": "operator@example.invalid",
                "userId": 12345,
                "firstName": "Never persisted",
            },
        }
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "assert sys.argv[1:] == ['status', '--format', 'json']\n"
            f"print({json.dumps(json.dumps(status))})\n"
        )
        script.chmod(0o755)
        return (str(script), "status", "--format", "json")

    def test_dry_run_and_terminal_emit_only_the_same_hashed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self.write_status_command(root)
            request = Request(
                engine="cursor",
                mode="safe",
                workspace=str(root),
                prompt="review",
                argv=[str(root / "cursor-agent"), "--print", "review"],
                model="cursor-model",
                account_binding_command=command,
            )
            dry_run = dry_run_payload(request)
            context = RunContext(
                registry_root=root,
                run_id="cursor-1",
                alias="cursor",
                harness="cursor",
                engine="cursor",
                mode="safe",
                model="cursor-model",
                source_cwd=str(root),
                execution_cwd=str(root),
                workspace_kind="git",
                isolated_workspace=True,
                started_at="2000-01-01T00:00:00Z",
                account_binding_command=command,
            )
            terminal = completion_json_payload(
                context,
                ok=True,
                status="succeeded",
                exit_code=0,
                duration_ms=1,
                stdout_bytes=0,
                stderr_bytes=0,
            )

            self.assertRegex(dry_run["accountFingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(dry_run["accountFingerprint"], terminal["accountFingerprint"])
            serialized = json.dumps({"dry_run": dry_run, "terminal": terminal})
            self.assertNotIn("operator@example.invalid", serialized)
            self.assertNotIn("12345", serialized)
            self.assertNotIn("Never persisted", serialized)

    def test_unauthenticated_status_omits_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            command = self.write_status_command(Path(raw), authenticated=False)
            payload: dict[str, object] = {}

            account_binding.add_account_fingerprint(
                payload,
                engine="cursor",
                command=command,
                env_overrides=None,
            )

            self.assertNotIn("accountFingerprint", payload)


if __name__ == "__main__":
    unittest.main()
