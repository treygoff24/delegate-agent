"""Unit coverage for ``cli._set_child_root_env`` root-env invariants."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delegate_agent.cli import _set_child_root_env
from delegate_agent.constants import MODE_CALL, MODE_WORK
from delegate_agent.request_models import Request, ResolvedWorkspace


def _request(
    mode: str,
    workspace: str,
    *,
    env_overrides: dict[str, str] | None = None,
) -> Request:
    return Request(
        engine="codex",
        mode=mode,
        workspace=workspace,
        prompt="task",
        argv=["codex"],
        model=None,
        env_overrides=env_overrides,
    )


class ChildRootEnvTests(unittest.TestCase):
    def test_call_mode_removes_inherited_execution_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp).resolve())
            request = _request(
                MODE_CALL,
                root,
                env_overrides={"DELEGATE_EXECUTION_ROOT": "/outer/stale-execution"},
            )
            _set_child_root_env(request, ResolvedWorkspace(root, "directory"))

            env = request.env_overrides
            assert env is not None
            self.assertEqual(env["DELEGATE_SOURCE_ROOT"], root)
            self.assertEqual(env["WORKSPACE_ROOT"], root)
            self.assertNotIn("DELEGATE_EXECUTION_ROOT", env)

    def test_work_mode_non_isolated_removes_execution_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp).resolve())
            request = _request(
                MODE_WORK,
                root,
                env_overrides={"DELEGATE_EXECUTION_ROOT": "/outer/stale-execution"},
            )
            _set_child_root_env(request, ResolvedWorkspace(root, "directory"))

            env = request.env_overrides
            assert env is not None
            self.assertEqual(env["DELEGATE_SOURCE_ROOT"], root)
            self.assertEqual(env["WORKSPACE_ROOT"], root)
            self.assertNotIn("DELEGATE_EXECUTION_ROOT", env)

    def test_overwrites_inherited_stale_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp).resolve())
            request = _request(
                MODE_WORK,
                root,
                env_overrides={"DELEGATE_SOURCE_ROOT": "/outer/stale-source"},
            )
            _set_child_root_env(request, ResolvedWorkspace(root, "directory"))

            env = request.env_overrides
            assert env is not None
            self.assertEqual(env["DELEGATE_SOURCE_ROOT"], root)
            self.assertEqual(env["WORKSPACE_ROOT"], root)
            self.assertNotIn("DELEGATE_EXECUTION_ROOT", env)

    def test_divergent_execution_root_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = str((base / "source").resolve())
            execution = str((base / "execution").resolve())
            Path(source).mkdir()
            Path(execution).mkdir()
            request = _request(MODE_WORK, execution)
            _set_child_root_env(request, ResolvedWorkspace(source, "directory"))

            env = request.env_overrides
            assert env is not None
            self.assertEqual(env["DELEGATE_SOURCE_ROOT"], source)
            self.assertEqual(env["WORKSPACE_ROOT"], execution)
            self.assertEqual(env["DELEGATE_EXECUTION_ROOT"], execution)
