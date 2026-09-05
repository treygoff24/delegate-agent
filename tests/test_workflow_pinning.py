from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
sys.path.insert(0, str(ROOT / "src"))

from delegate_agent import profiles, safe_workspace, workflow_pinning  # noqa: E402
from delegate_agent.workflows import registry as workflow_registry  # noqa: E402


class WorkflowPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".delegate" / "personas").mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self) -> None:
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def test_create_pin_is_content_addressed_and_excludes_secret_config(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("Review only.\n", encoding="utf-8")
        config = {
            "codex": {"binary": "/bin/codex", "defaultModel": "safe"},
            "credentials": {"apiKey": "must-not-be-copied"},
            "policy": {"profile": "safe"},
        }
        pool = self.home / ".delegate" / "worktrees"
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config=config,
            data_home=pool,
            home=self.home,
        )

        self.assertTrue(pin.path.is_file())
        self.assertFalse(pin.path.is_relative_to(pool))
        self.assertTrue(pin.runtime_root.is_dir())
        self.assertTrue(pin.import_root.is_dir())
        self.assertEqual(pin.personas["reviewer"]["text"], "Review only.\n")
        payload = json.loads(pin.path.read_text(encoding="utf-8"))
        self.assertNotIn("credentials", payload["config"])
        self.assertEqual(payload["config"]["codex"]["binary"], "/bin/codex")
        self.assertEqual(pin.path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(pin.config_path.stat().st_mode & 0o777, 0o400)

    def test_applying_pin_environment_twice_does_not_duplicate_pythonpath(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        user_pythonpath = str(self.root / "user-pythonpath")

        with mock.patch.dict(os.environ, {"PYTHONPATH": user_pythonpath}, clear=False):
            workflow_pinning.temporarily_apply_environment(pin)
            applied_once = os.environ["PYTHONPATH"]
            workflow_pinning.temporarily_apply_environment(pin)
            applied_twice = os.environ["PYTHONPATH"]

        self.assertEqual(applied_once, os.pathsep.join((str(pin.import_root), user_pythonpath)))
        self.assertEqual(applied_twice, applied_once)

    def test_engine_child_environment_scrubs_pin_without_poisoning_python(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        user_pythonpath = self.root / "user-pythonpath"
        similar_prefix = self.home / f"{workflow_pinning.PIN_ROOT_DIRNAME}-user"
        user_pythonpath.mkdir()
        similar_prefix.mkdir()
        child_pythonpath = os.pathsep.join(
            (
                str(pin.import_root),
                str(pin.runtime_root),
                str(user_pythonpath),
                str(similar_prefix),
            )
        )

        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "DELEGATE_CONFIG": str(pin.config_path),
            },
            clear=True,
        ):
            child_env = profiles.child_environment(
                overrides={
                    "DELEGATE_WORKFLOW_PIN": str(pin.path),
                    "DELEGATE_WORKFLOW_LOCK_FD": "123",
                    "PYTHONPATH": child_pythonpath,
                }
            )

        self.assertNotIn("DELEGATE_WORKFLOW_PIN", child_env)
        self.assertNotIn("DELEGATE_WORKFLOW_LOCK_FD", child_env)
        self.assertEqual(child_env["DELEGATE_CONFIG"], str(pin.config_path))
        self.assertEqual(
            child_env["PYTHONPATH"],
            os.pathsep.join((str(user_pythonpath), str(similar_prefix))),
        )

        probe = subprocess.run(
            [sys.executable, "-c", "import sys; print('delegate_agent' in sys.modules)"],
            cwd=self.workspace,
            env=child_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "False")

    def test_pinned_delegate_cli_child_passes_persona_resolver_guard(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        probe = subprocess.run(
            [*pin.cli_argv, "--json", "describe"],
            cwd=self.workspace,
            env={**os.environ, **pin.environment},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertNotIn("workflow_persona_pin_unavailable", probe.stderr)

    def test_safe_workspace_cleanup_preserves_pin_store_modes(self) -> None:
        pin = workflow_pinning.create_pin(
            "wf_0123456789ab",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        pinned_paths = (pin.runtime_root, pin.import_root, pin.entrypoint)
        source_modes = {path: path.stat().st_mode & 0o777 for path in pinned_paths}

        copy_path, temp_base = safe_workspace.create_directory_safe_workspace(str(pin.runtime_root))
        self.assertEqual(Path(copy_path).stat().st_mode & 0o777, 0o500)
        safe_workspace.cleanup_safe_isolated_workspace(
            git_root=None,
            isolated_workspace=copy_path,
            temp_base=temp_base,
            source_root=str(pin.runtime_root),
        )

        self.assertFalse(Path(temp_base).exists())
        self.assertEqual(
            {path: path.stat().st_mode & 0o777 for path in pinned_paths},
            source_modes,
        )

    def test_pinned_runtime_and_persona_are_used_by_child_imports(self) -> None:
        persona = self.home / ".delegate" / "personas" / "reviewer.md"
        persona.write_text("Pinned bytes\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_abcdefabcdef",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        persona.write_text("Live bytes\n", encoding="utf-8")
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from delegate_agent.personas import resolve_persona; print(resolve_persona('.', 'reviewer').text, end='')",
            ],
            env={
                **os.environ,
                "DELEGATE_WORKFLOW_PIN": str(pin.path),
                "PYTHONPATH": str(pin.import_root),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout, "Pinned bytes\n")

    def test_safe_workspace_persona_never_falls_through_to_live_home(self) -> None:
        workspace_persona = self.workspace / ".delegate" / "personas" / "reviewer.md"
        workspace_persona.parent.mkdir(parents=True)
        workspace_persona.write_text("Pinned workspace bytes\n", encoding="utf-8")
        live_persona = self.home / ".delegate" / "personas" / "reviewer.md"
        live_persona.write_text("LIVE HOME BYTES\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_123456789abc",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        workspace_persona.unlink()
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from delegate_agent.errors import DelegateError; "
                "from delegate_agent.personas import resolve_persona; "
                "\ntry:\n"
                " print(resolve_persona('.', 'reviewer', mode='safe').text, end='')\n"
                "except DelegateError as exc:\n"
                " print(exc.error)",
            ],
            cwd=self.workspace,
            env={
                **os.environ,
                "DELEGATE_WORKFLOW_PIN": str(pin.path),
                "PYTHONPATH": str(pin.import_root),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "workspace_persona_refused")
        self.assertNotIn("LIVE HOME BYTES", probe.stdout)

    def test_cli_hard_exits_when_pinned_resolver_installation_is_missing(self) -> None:
        live_persona = self.home / ".delegate" / "personas" / "reviewer.md"
        live_persona.write_text("LIVE HOME BYTES\n", encoding="utf-8")
        pin = workflow_pinning.create_pin(
            "wf_456789abcdef",
            workspace=self.workspace,
            config={},
            home=self.home,
        )
        sitecustomize = pin.import_root / "sitecustomize.py"
        sitecustomize.chmod(0o600)
        sitecustomize.write_text('"""Deliberately leaves the pin resolver uninstalled."""\n')

        probe_code = (
            "from delegate_agent.cli import main; "
            "from delegate_agent.personas import resolve_persona; "
            "print(resolve_persona('.', 'reviewer').text, end='')"
        )
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PYTHONPATH": str(pin.import_root),
        }
        pinned = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=self.workspace,
            env={**env, "DELEGATE_WORKFLOW_PIN": str(pin.path)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(pinned.returncode, 0)
        self.assertEqual(pinned.stdout, "")
        self.assertNotIn("LIVE HOME BYTES", pinned.stderr)

        pinless = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=self.workspace,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(pinless.returncode, 0, pinless.stderr)
        self.assertEqual(pinless.stdout, "LIVE HOME BYTES\n")

    def test_partial_runtime_temporary_directory_is_rebuilt_before_publish(self) -> None:
        files = workflow_pinning._runtime_source_files()
        digest = workflow_pinning._runtime_digest(files)
        partial = self.home / workflow_pinning.PIN_ROOT_DIRNAME / workflow_pinning.RUNTIME_DIR
        partial = partial / f"{digest}.tmp"
        (partial / "src").mkdir(parents=True)
        (partial / "src" / "partial.py").write_text("partial", encoding="utf-8")

        pin = workflow_pinning.create_pin(
            "wf_23456789abcd",
            workspace=self.workspace,
            config={},
            home=self.home,
        )

        self.assertTrue(pin.entrypoint.is_file())
        self.assertFalse(partial.exists())

    def test_active_supervisor_index_reconciles_stale_entries(self) -> None:
        workspace_root = self.workspace / ".delegate" / "workflows" / "wf_0123456789ab"
        workspace_root.mkdir(parents=True)
        lock_fd = workflow_registry.acquire_workflow_lock(workspace_root)
        try:
            pin = workflow_pinning.create_pin(
                "wf_0123456789ab",
                workspace=self.workspace,
                config={},
                home=self.home,
            )
            workflow_pinning.register_active_supervisor(
                "wf_0123456789ab",
                workflow_root=workspace_root,
                workspace=self.workspace,
                pin=pin,
                home=self.home,
            )
            live = workflow_pinning.reconcile_active_supervisors(home=self.home)
            self.assertIn("wf_0123456789ab", live["supervisors"])
        finally:
            os.close(lock_fd)
        stale = workflow_pinning.reconcile_active_supervisors(home=self.home)
        self.assertNotIn("wf_0123456789ab", stale["supervisors"])

    def test_concurrent_supervisor_registration_preserves_both_entries(self) -> None:
        writer = """
import os
import sys
import time
from pathlib import Path
from delegate_agent import workflow_pinning
from delegate_agent.workflows import registry as workflow_registry

home, workspace, workflow_id = map(Path, sys.argv[1:])
root = workspace / '.delegate' / 'workflows' / str(workflow_id)
root.mkdir(parents=True)
lock_fd = workflow_registry.acquire_workflow_lock(root)
try:
    pin = workflow_pinning.create_pin(str(workflow_id), workspace=workspace, config={}, home=home)
    workflow_pinning.register_active_supervisor(
        str(workflow_id), workflow_root=root, workspace=workspace, pin=pin, home=home
    )
    print('ready', flush=True)
    time.sleep(2)
finally:
    os.close(lock_fd)
"""
        processes = []
        for workflow_id in ("wf_0123456789ab", "wf_abcdefabcdef"):
            workspace = self.root / workflow_id
            workspace.mkdir()
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", writer, str(self.home), str(workspace), workflow_id],
                    env={**os.environ, "PYTHONPATH": SRC},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        try:
            self.assertEqual(processes[0].stdout.readline().strip(), "ready")
            self.assertEqual(processes[1].stdout.readline().strip(), "ready")
            index = workflow_pinning.reconcile_active_supervisors(home=self.home)
            self.assertEqual(set(index["supervisors"]), {"wf_0123456789ab", "wf_abcdefabcdef"})
        finally:
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")

    def test_doctor_and_promote_share_machine_local_surface(self) -> None:
        stamp = workflow_pinning.promote(
            actor="test",
            runtime_digest="a" * 64,
            source="unit-test",
            home=self.home,
        )
        report = workflow_pinning.doctor(home=self.home)
        self.assertEqual(report["promotion"], stamp)
        self.assertEqual(report["schema"], workflow_pinning.DOCTOR_SCHEMA)
        # A stamp naming some other runtime is exactly the rsync-without-stamp
        # case; doctor must call it out rather than report a clean surface.
        self.assertEqual(report["runtimeDigest"], workflow_pinning.live_runtime_digest())
        self.assertFalse(report["promotionMatchesRuntime"])
        self.assertTrue(
            any("without 'delegate promote'" in warning for warning in report["warnings"])
        )

    def test_doctor_warns_when_no_promotion_stamp_exists(self) -> None:
        report = workflow_pinning.doctor(home=self.home)
        self.assertIsNone(report["promotion"])
        self.assertFalse(report["promotionMatchesRuntime"])
        self.assertTrue(any("no promotion stamp" in warning for warning in report["warnings"]))

    def test_checkout_stamp_cannot_certify_an_installed_runtime(self) -> None:
        launcher = self.home / ".delegate" / "bin" / "delegate.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(workflow_pinning._LAUNCHER)
        workflow_pinning.promote(actor="test", source="claimed-commit", home=self.home)
        report = workflow_pinning.doctor(home=self.home)
        self.assertFalse(report["promotionMatchesRuntime"])

    def _installed_fixture(self) -> tuple[Path, Path]:
        installed = self.home / ".delegate"
        shutil.copytree(ROOT / "src" / "delegate_agent", installed / "src" / "delegate_agent")
        launcher = installed / "bin" / "delegate.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(workflow_pinning._LAUNCHER)
        outer = self.home / "bin" / "delegate"
        outer.parent.mkdir()
        outer.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{launcher}" "$@"\n')
        outer.chmod(0o700)
        return launcher, outer

    def _installed_command(self, outer: Path, *args: str) -> dict:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("DELEGATE_WORKFLOW_PIN", None)
        env["PATH"] = str(outer.parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [str(outer), "--json", *args],
            cwd=self.workspace,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_installed_artifact_manifest_detects_package_and_outer_launcher_drift(self) -> None:
        launcher, outer = self._installed_fixture()
        stamp = self._installed_command(outer, "promote", "--actor", "fixture", "--source", "label")
        self.assertTrue(stamp["artifactVerified"])
        self.assertFalse(stamp["sourceVerified"])
        before = self._home_snapshot()
        report = self._installed_command(outer, "doctor")
        self.assertTrue(report["promotionMatchesRuntime"])
        self.assertEqual(before, self._home_snapshot())
        with mock.patch.dict(os.environ, {"PATH": str(outer.parent)}):
            mixed = workflow_pinning.doctor(home=self.home)
        self.assertEqual(mixed["runtimeDigest"], mixed["installedRuntimeDigest"])
        self.assertFalse(mixed["promotionMatchesRuntime"], "identical bytes do not unify roots")
        original = outer.read_bytes()
        outer.write_bytes(original + b"\n# changed outer shim\n")
        self.assertFalse(self._installed_command(outer, "doctor")["promotionMatchesRuntime"])
        outer.write_bytes(original)
        self.assertTrue(self._installed_command(outer, "doctor")["promotionMatchesRuntime"])
        module = launcher.parent.parent / "src" / "delegate_agent" / "__init__.py"
        module.write_bytes(module.read_bytes() + b"\n# same version, different bytes\n")
        changed = self._installed_command(outer, "doctor")
        self.assertFalse(changed["promotionMatchesRuntime"])
        self.assertNotEqual(changed["runtimeDigest"], stamp["runtimeDigest"])

    def test_overrides_old_stamps_and_missing_launchers_cannot_certify(self) -> None:
        launcher, outer = self._installed_fixture()
        stamp = self._installed_command(outer, "promote", "--actor", "fixture", "--source", "label")
        override = self._installed_command(
            outer,
            "promote",
            "--actor",
            "fixture",
            "--source",
            "label",
            "--runtime-digest",
            stamp["runtimeDigest"],
        )
        self.assertTrue(override["runtimeDigestOverride"])
        self.assertFalse(self._installed_command(outer, "doctor")["promotionMatchesRuntime"])
        workflow_pinning.promotion_path(self.home).write_text(
            json.dumps(
                {
                    "schema": workflow_pinning.PROMOTION_SCHEMA,
                    "runtimeDigest": stamp["runtimeDigest"],
                }
            )
        )
        self.assertFalse(self._installed_command(outer, "doctor")["promotionMatchesRuntime"])
        self._installed_command(outer, "promote", "--actor", "fixture", "--source", "label")
        launcher.rename(launcher.with_suffix(".absent"))
        with mock.patch.dict(os.environ, {"PATH": str(outer.parent)}):
            missing = workflow_pinning.doctor(home=self.home)
        self.assertFalse(missing["installedArtifactComplete"])
        self.assertFalse(missing["promotionMatchesRuntime"])

    def test_promote_defaults_to_executing_digest_without_certifying_checkout(self) -> None:
        stdout = io.StringIO()
        code = workflow_pinning.emit_promote(
            actor="test", source="unit-test", home=self.home, stdout=stdout, json_mode=True
        )
        self.assertEqual(code, 0)
        stamp = json.loads(stdout.getvalue())
        self.assertEqual(stamp["runtimeDigest"], workflow_pinning.live_runtime_digest())
        report = workflow_pinning.doctor(home=self.home)
        self.assertFalse(report["promotionMatchesRuntime"])
        self.assertFalse(stamp["artifactVerified"])
        self.assertFalse(stamp["sourceVerified"])

    def _home_snapshot(self) -> dict[str, bytes | None]:
        snapshot: dict[str, bytes | None] = {}
        for path in sorted(self.home.rglob("*")):
            snapshot[str(path)] = path.read_bytes() if path.is_file() else None
        return snapshot

    def test_doctor_never_writes_under_home(self) -> None:
        index_path = workflow_pinning.active_index_path(self.home)
        self.home.mkdir(parents=True, exist_ok=True)
        before = self._home_snapshot()
        workflow_pinning.doctor(home=self.home)
        self.assertEqual(self._home_snapshot(), before, "doctor wrote into an empty home")
        self.assertFalse(index_path.exists(), "doctor created the index it claims only to read")
        # A stale entry (dead workflow root) is dropped from the view but the
        # whole home tree -- index bytes, lock files, directories -- stays
        # exactly as it was.
        stale = json.dumps(
            {
                "schema": workflow_pinning.ACTIVE_INDEX_SCHEMA,
                "supervisors": {
                    "wf_deadbeef0000": {"workflowRoot": str(self.root / "gone"), "workspace": "x"}
                },
            },
            indent=1,
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(stale, encoding="utf-8")
        before = self._home_snapshot()
        report = workflow_pinning.doctor(home=self.home)
        self.assertEqual(report["activeSupervisors"], {})
        self.assertEqual(self._home_snapshot(), before, "doctor left new files or bytes behind")
        # The mutating reconcile still prunes, so the two paths are distinct.
        workflow_pinning.reconcile_active_supervisors(home=self.home)
        self.assertNotEqual(index_path.read_text(encoding="utf-8"), stale)

    def test_doctor_preserves_supervisor_lock_permissions_and_pin(self) -> None:
        import fcntl

        root = self.workspace / "workflow"
        root.mkdir()
        lock = root / workflow_registry.LOCK_FILE
        lock.write_bytes(b"")
        lock.chmod(0o644)
        pin = workflow_pinning.create_pin(
            "wf_123456abcdef", workspace=self.workspace, config={}, home=self.home
        )
        index = workflow_pinning.active_index_path(self.home)
        index.write_text(
            json.dumps(
                {
                    "supervisors": {
                        pin.workflow_id: {"workflowRoot": str(root), "pinPath": str(pin.path)}
                    }
                }
            )
        )
        before = self._home_snapshot()
        with lock.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            report = workflow_pinning.doctor(home=self.home)
            self.assertIn(pin.workflow_id, report["activeSupervisors"])
            self.assertTrue(any("pinned" in warning for warning in report["warnings"]))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        self.assertEqual(before, self._home_snapshot())
        self.assertEqual(workflow_pinning.load_pin(pin.workflow_id, home=self.home), pin)

    def test_launcher_identity_is_the_installed_file_not_argv(self) -> None:
        self.assertIsNone(workflow_pinning.entrypoint_path(self.home))
        launcher = self.home / ".delegate" / "bin" / "delegate.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(workflow_pinning._LAUNCHER)
        expected = workflow_pinning.entrypoint_digest(self.home)
        for argv0 in ("delegate", "/usr/bin/python3", str(self.root / "elsewhere.py"), ""):
            with mock.patch.object(sys, "argv", [argv0, "doctor"]):
                self.assertEqual(workflow_pinning.entrypoint_path(self.home), launcher)
                self.assertEqual(workflow_pinning.entrypoint_digest(self.home), expected)

    def test_doctor_warns_when_installed_launcher_changed_since_promotion(self) -> None:
        launcher = self.home / ".delegate" / "bin" / "delegate.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(workflow_pinning._LAUNCHER)
        stamp = workflow_pinning.promote(actor="test", source="unit-test", home=self.home)
        self.assertEqual(stamp["entrypoint"], str(launcher))
        self.assertEqual(stamp["entrypointDigest"], workflow_pinning.entrypoint_digest(self.home))
        self.assertFalse(workflow_pinning.doctor(home=self.home)["promotionMatchesRuntime"])
        # A launcher rewritten without a fresh promotion is the real defect
        # this catches: the package stamp still matches, the launcher does not.
        launcher.write_bytes(workflow_pinning._LAUNCHER + b"\n# rewritten by a later install\n")
        report = workflow_pinning.doctor(home=self.home)
        self.assertFalse(report["promotionMatchesRuntime"])
        self.assertEqual(report["entrypoint"], str(launcher))
        self.assertTrue(any("launcher" in warning for warning in report["warnings"]))

    def test_promote_reads_the_live_digest_under_the_promotion_lock(self) -> None:
        import fcntl

        from delegate_agent import run_registry

        lock_path = workflow_pinning.promotion_path(self.home).with_name(
            workflow_pinning.PROMOTION_LOCK_FILE
        )
        observed: list[bool] = []

        def digest_while_checking_lock() -> str:
            fd = run_registry.open_private_file(lock_path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                observed.append(False)
            finally:
                os.close(fd)
            return "c" * 64

        with mock.patch.object(
            workflow_pinning, "live_runtime_digest", side_effect=digest_while_checking_lock
        ):
            stamp = workflow_pinning.promote(actor="test", source="unit-test", home=self.home)
            # The CLI path is the one that used to capture the digest early.
            stdout = io.StringIO()
            workflow_pinning.emit_promote(
                actor="cli", source="unit-test", home=self.home, stdout=stdout, json_mode=True
            )
        self.assertEqual(observed, [True, True], "live digest was read outside the promotion lock")
        self.assertEqual(stamp["runtimeDigest"], "c" * 64)
        self.assertEqual(json.loads(stdout.getvalue())["runtimeDigest"], "c" * 64)

    def test_promote_serializes_under_the_promotion_lock(self) -> None:
        import threading

        from delegate_agent import run_registry

        lock_path = workflow_pinning.promotion_path(self.home).with_name(
            workflow_pinning.PROMOTION_LOCK_FILE
        )
        finished = threading.Event()

        def promote_in_thread() -> None:
            workflow_pinning.promote(
                actor="late", runtime_digest="b" * 64, source="thread", home=self.home
            )
            finished.set()

        workflow_pinning.promote(
            actor="early", runtime_digest="a" * 64, source="main", home=self.home
        )
        run_registry.ensure_private_dir(lock_path.parent)
        with run_registry.file_lock(lock_path):
            worker = threading.Thread(target=promote_in_thread)
            worker.start()
            self.assertFalse(finished.wait(0.5), "promote wrote while the lock was held")
            held = json.loads(
                workflow_pinning.promotion_path(self.home).read_text(encoding="utf-8")
            )
            self.assertEqual(held["actor"], "early")
        worker.join(timeout=10)
        self.assertTrue(finished.is_set())
        final = json.loads(workflow_pinning.promotion_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(final["actor"], "late")
        self.assertGreater(final["promotedAt"], held["promotedAt"])

    def test_cli_doctor_is_allowed_by_profile_guard_with_missing_overlay(self) -> None:
        from delegate_agent import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AI_PROFILE": "work"}):
            code = cli.main(["--json", "doctor"], stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("read-only", err.getvalue())
        self.assertFalse(workflow_pinning.active_index_path(self.home).exists())
        with mock.patch.dict(os.environ, {"AI_PROFILE": "work"}):
            code = cli.main(
                ["--json", "promote", "--actor", "a", "--source", "b"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(workflow_pinning.promotion_path(self.home).exists())

    def test_cli_doctor_and_promote_round_trip_through_main(self) -> None:
        from delegate_agent import cli

        out = io.StringIO()
        self.assertEqual(cli.main(["--json", "doctor"], stdout=out, stderr=io.StringIO()), 0)
        before = json.loads(out.getvalue())
        self.assertIsNone(before["promotion"])
        self.assertFalse(before["promotionMatchesRuntime"])

        out = io.StringIO()
        code = cli.main(
            ["--json", "promote", "--actor", "unit", "--source", "deadbeef"],
            stdout=out,
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 0)
        stamp = json.loads(out.getvalue())
        self.assertEqual(stamp["actor"], "unit")
        self.assertEqual(stamp["source"], "deadbeef")
        path = workflow_pinning.promotion_path(self.home)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        out = io.StringIO()
        self.assertEqual(cli.main(["doctor"], stdout=out, stderr=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn(f"runtime digest: {stamp['runtimeDigest']}", text)
        self.assertIn("by unit (deadbeef)", text)
        self.assertIn("warning: installed artifact parity is not verified", text)

        out = io.StringIO()
        self.assertEqual(cli.main(["--json", "doctor"], stdout=out, stderr=io.StringIO()), 0)
        after = json.loads(out.getvalue())
        self.assertFalse(after["promotionMatchesRuntime"])
        self.assertEqual(after["promotion"], stamp)


if __name__ == "__main__":
    unittest.main()
