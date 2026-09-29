"""Contract tests: the argv Delegate builds against the REAL installed binaries.

Every other adapter test in the suite drives a fake ``codex`` or ``post`` that
accepts any argv, which is how two dead flags shipped: mail push injected
``-c hooks=true`` (Codex 0.157.1 rejects it at config load) and the notifier
passed ``--allow-self`` (Post 0.9.0 rejects it at argument parsing). A fake
cannot tell the argv Delegate emits from the argv the tool accepts, so these
tests run the installed binaries in modes that neither spend nor send outside a
throwaway store.

Skip policy: a class skips only when its binary is not on PATH (CI, fresh
clones) or when ``DELEGATE_SKIP_REAL_BINARY_CONTRACTS`` is set to turn the
contract tests off on purpose; both skips say why. A binary that IS on PATH but
will not run (``--version`` fails or times out, the throwaway store cannot be
set up) is a broken integration, so it FAILS the class with the captured
output instead of skipping past it.

Codex is checked in two layers because Codex parses in two layers:

* flags: ``codex exec <argv> --help``. clap validates every token before the
  trailing ``--help`` fires, so an unknown flag exits 2 and a known one prints
  help and exits 0. ``--help`` short-circuits before config load, so this layer
  cannot see a bad ``-c`` value.
* config overrides: ``codex features list <-c/--enable/--disable pairs>`` loads
  the same bootstrap configuration a real launch does (which is what rejects
  ``-c hooks=true``) and then only lists feature flags. It sends nothing.

Post is exercised end to end through :func:`notify.send_notification` against a
throwaway ``POST_MAIL_ROOT`` and HOME, so the message lands in a temp store and
nowhere else.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import argv_builders, mail, notify, run_registry, run_status
from delegate_agent import config as delegate_config
from tests import ORIGINAL_HOME

_OVERRIDE_FLAGS = frozenset({"-c", "--config", "--enable", "--disable"})
_TIMEOUT_SEC = 60
_BOGUS_FLAG = "--delegate-contract-bogus-flag"
_OPT_OUT_ENV = "DELEGATE_SKIP_REAL_BINARY_CONTRACTS"
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _tool_env(extra: dict[str, str]) -> dict[str, str]:
    """A minimal environment for a real binary.

    The suite redirects HOME to a temp dir, but the shims that front ``codex``
    on a developer machine resolve their helpers under the real home, so the
    original HOME is restored. Anything that stores state is pointed at a temp
    dir through ``extra`` (CODEX_HOME, POST_MAIL_ROOT).
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": ORIGINAL_HOME or ""}
    env.update(extra)
    return env


def _run(argv: list[str], *, env: dict[str, str], cwd: str | None = None):
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
        check=False,
    )


def _text(value: object) -> str:
    """Captured output as text; ``TimeoutExpired`` carries bytes or ``None``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _require_binary(name: str) -> str:
    """The installed ``name``'s path, or a skip that says why there is none.

    This is the only place the contract tests skip: the operator turned them
    off, or the binary is not installed. Anything else about an installed
    binary is a failure for the caller to raise.
    """
    if os.environ.get(_OPT_OUT_ENV, "").strip().lower() not in _FALSEY:
        raise unittest.SkipTest(
            f"{_OPT_OUT_ENV} is set: real-{name} contract tests are deliberately off on this host"
        )
    path = shutil.which(name)
    if path is None:
        raise unittest.SkipTest(f"{name} is not installed (not found on PATH)")
    return path


def _run_required(argv: list[str], *, env: dict[str, str], cwd: str | None = None):
    """Run ``argv`` and fail with everything it printed unless it exits 0.

    The binary was found on PATH, so a non-zero exit, a timeout or an exec
    error means the installed tool is broken here. Reporting that as a skip
    would let a broken integration pass unnoticed.
    """
    label = " ".join(argv)
    try:
        result = _run(argv, env=env, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"`{label}` timed out after {_TIMEOUT_SEC}s; the installed binary is on PATH but "
            f"does not respond.\nstdout: {_text(exc.stdout).strip()}\n"
            f"stderr: {_text(exc.stderr).strip()}"
        ) from None
    except OSError as exc:
        raise AssertionError(
            f"`{label}` could not be executed ({exc}); the binary is on PATH but is not runnable."
        ) from None
    if result.returncode != 0:
        raise AssertionError(
            f"`{label}` exited {result.returncode}; the installed binary is on PATH but is not "
            f"working, so the contract cannot be checked.\nstdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _override_args(argv: list[str]) -> list[str]:
    """The ``-c``/``--enable``/``--disable`` pairs of ``argv``, in order."""
    pairs: list[str] = []
    for index, token in enumerate(argv[:-1]):
        if token in _OVERRIDE_FLAGS:
            pairs.extend([token, argv[index + 1]])
    return pairs


class CodexArgvContractTests(unittest.TestCase):
    """The exact argv Delegate builds for Codex is accepted by the real Codex."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = _require_binary("codex")
        cls._codex_home = tempfile.mkdtemp(prefix="delegate-codex-contract-")
        cls.addClassCleanup(shutil.rmtree, cls._codex_home, ignore_errors=True)
        cls.env = _tool_env({"CODEX_HOME": cls._codex_home})
        _run_required([cls.binary, "--version"], env=cls.env)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="delegate-codex-contract-ws-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.registry_root = run_registry.ensure_registry(
            self.workspace, workspace_kind="directory"
        )
        self.run_id, self.alias = run_registry.register_run(
            self.registry_root,
            harness="codex",
            metadata={"mode": "work", "cwd": str(self.workspace)},
        )
        run_registry.write_json_atomic(
            run_registry.run_directory(self.registry_root, self.run_id) / run_registry.STATE_FILE,
            {
                "schema": run_registry.STATE_SCHEMA,
                "runId": self.run_id,
                "alias": self.alias,
                "status": run_status.STATUS_RUNNING,
                "pid": os.getpid(),
                "recentEvents": [],
                "eventsTotal": 0,
                "warnings": [],
            },
        )
        self.source_home = self.workspace / "source-codex-home"
        self.source_home.mkdir()

    def _build(self, mode: str, *, policy_overrides: dict | None = None, **kwargs) -> list[str]:
        config = delegate_config.deep_merge(
            delegate_config.embedded_default_config(),
            {"policy": {mode: policy_overrides}} if policy_overrides else {},
        )
        policy = delegate_config.effective_policy(config, engine="codex", mode=mode)
        return argv_builders.build_codex_argv(
            config["codex"],
            mode,
            str(self.workspace),
            None,
            "contract prompt",
            policy,
            workspace_kind="git",
            **kwargs,
        )

    def _with_mail_push(self, argv: list[str]) -> list[str]:
        provision = mail.provision_mail_push(
            "codex",
            argv,
            list(argv),
            self.registry_root,
            self.run_id,
            {"CODEX_HOME": str(self.source_home)},
        )
        self.assertIsNone(provision.warning, "mail push provisioning degraded")
        return provision.argv

    def _variants(self) -> dict[str, list[str]]:
        resume = {"resume_session_id": "00000000-0000-0000-0000-000000000000"}
        return {
            "work": self._build("work"),
            "work + mail push": self._with_mail_push(self._build("work")),
            "work + mail push + stdin prompt": self._with_mail_push(
                self._build("work", prompt_transport="stdin", fast=True)
            ),
            "work + web search + mail push": self._with_mail_push(
                self._build("work", policy_overrides={"webSearch": True})
            ),
            "work resume + mail push": self._with_mail_push(
                self._build("work", persist_session=True, **resume)
            ),
            "safe": self._build("safe"),
            "call": self._build("call"),
        }

    def _flags_accepted(self, argv: list[str]) -> subprocess.CompletedProcess:
        # Drop the binary and the trailing prompt token, then let `--help`
        # fire after clap has validated everything before it.
        return _run([self.binary, *argv[1:-1], "--help"], env=self.env)

    def _overrides_accepted(self, argv: list[str]) -> subprocess.CompletedProcess:
        return _run([self.binary, "features", "list", *_override_args(argv)], env=self.env)

    def test_every_codex_argv_delegate_builds_passes_the_real_flag_parser(self) -> None:
        for name, argv in self._variants().items():
            with self.subTest(variant=name):
                result = self._flags_accepted(argv)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"codex rejected the argv delegate built: {argv}\n{result.stderr.strip()}",
                )

    def test_every_codex_config_override_delegate_builds_loads_in_the_real_codex(self) -> None:
        for name, argv in self._variants().items():
            with self.subTest(variant=name):
                result = self._overrides_accepted(argv)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"codex rejected a config override in: {argv}\n{result.stderr.strip()}",
                )

    def test_mail_push_turns_hooks_on_through_the_feature_flag(self) -> None:
        argv = self._with_mail_push(self._build("work"))
        overrides = _override_args(argv)
        pairs = list(zip(overrides[::2], overrides[1::2], strict=True))
        self.assertIn(("--enable", "hooks"), pairs)
        self.assertNotIn("hooks=true", argv)

    def test_the_flag_layer_rejects_an_unknown_flag(self) -> None:
        # Negative control: proves the flag check can fail. Without it a
        # parser that accepted everything would leave both layers green.
        argv = self._build("work")
        result = self._flags_accepted([*argv[:-1], _BOGUS_FLAG, argv[-1]])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_BOGUS_FLAG, result.stderr)

    def test_the_override_layer_rejects_a_mistyped_config_value(self) -> None:
        # Negative control for the layer that reads values. `features.hooks`
        # is a boolean in every Codex that has the feature, so a string is a
        # stable rejection; it does not depend on which spelling is current.
        result = _run(
            [self.binary, "features", "list", "-c", 'features.hooks="not-a-bool"'],
            env=self.env,
        )
        self.assertNotEqual(result.returncode, 0)


class PostNotifyContractTests(unittest.TestCase):
    """The exact ``post`` argv ``--notify`` builds is accepted by the real post."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = _require_binary("post")
        cls.root = Path(tempfile.mkdtemp(prefix="delegate-post-contract-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, ignore_errors=True)
        (cls.root / "home").mkdir()
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()
        cls.base_env = _tool_env(
            {"HOME": str(cls.root / "home"), "POST_MAIL_ROOT": str(cls.root / "mail")}
        )
        _run_required([cls.binary, "--version"], env=cls.base_env)
        cls.room = "delegate-contract-room"
        _run_required(
            [cls.binary, "rooms", "add", cls.room, str(cls.workspace)],
            env=cls.base_env,
            cwd=str(cls.workspace),
        )
        cls.sender = cls._bind("sender")
        cls.peer = cls._bind("peer")

    @classmethod
    def _bind(cls, harness: str) -> str:
        bound = _run_required(
            [
                cls.binary,
                "participant",
                "bind",
                "--workspace",
                cls.room,
                "--new",
                "--harness",
                f"delegate-contract-{harness}",
            ],
            env=cls.base_env,
            cwd=str(cls.workspace),
        )
        prefix = "export POST_PARTICIPANT="
        for line in bound.stdout.splitlines():
            if line.startswith(prefix):
                participant = line.removeprefix(prefix).strip()
                if participant:
                    return participant
        raise AssertionError(
            f"`post participant bind` for {harness!r} printed no `{prefix}...` line, so the "
            f"throwaway participant could not be set up.\nstdout: {bound.stdout.strip()}\n"
            f"stderr: {bound.stderr.strip()}"
        )

    def _env_for(self, participant: str) -> dict[str, str]:
        return {**self.base_env, "POST_PARTICIPANT": participant}

    def test_room_ping_is_accepted_and_reaches_the_rooms_other_participants(self) -> None:
        target = notify.parse_notify_target(f"room:{self.room}")
        outcome = notify.send_notification(
            target,
            "delegate contract room ping",
            cwd=str(self.workspace),
            env=self._env_for(self.sender),
        )
        self.assertTrue(outcome.ok, f"post rejected the room ping: {outcome.payload()}")
        self.assertIsNotNone(outcome.message_id)
        inbox = _run([self.binary, "inbox"], env=self._env_for(self.peer), cwd=str(self.workspace))
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn(outcome.message_id or "", inbox.stdout)

    def test_channel_ping_is_accepted(self) -> None:
        channel = "delegate-contract-channel"
        joined = _run(
            [self.binary, "chat", channel, "--join"],
            env=self._env_for(self.sender),
            cwd=str(self.workspace),
        )
        self.assertEqual(joined.returncode, 0, joined.stderr)
        outcome = notify.send_notification(
            notify.parse_notify_target(f"channel:{channel}"),
            "delegate contract channel ping",
            cwd=str(self.workspace),
            env=self._env_for(self.sender),
        )
        self.assertTrue(outcome.ok, f"post rejected the channel ping: {outcome.payload()}")
        self.assertIsNotNone(outcome.message_id)

    def test_every_flag_the_notifier_passes_is_in_posts_own_help(self) -> None:
        # Cheap second witness that needs no mailbox: each `--flag` token in
        # the argv appears in the help text of the subcommand that receives it.
        for target_spec, subcommand in ((f"room:{self.room}", "send"), ("channel:c", "chat")):
            with self.subTest(subcommand=subcommand):
                argv = notify.notify_argv(notify.parse_notify_target(target_spec), "message")
                helped = _run([self.binary, subcommand, "--help"], env=self.base_env)
                self.assertEqual(helped.returncode, 0, helped.stderr)
                for token in argv:
                    if token.startswith("--"):
                        self.assertIn(token, helped.stdout, f"post {subcommand} has no {token}")

    def test_the_post_check_rejects_an_unknown_flag(self) -> None:
        # Negative control: an argv with a flag post does not have must fail
        # here, or every green result above would prove nothing.
        argv = notify.notify_argv(notify.parse_notify_target(f"room:{self.room}"), "message")
        result = _run(
            [self.binary, *argv[1:-2], _BOGUS_FLAG, *argv[-2:]],
            env=self._env_for(self.sender),
            cwd=str(self.workspace),
        )
        self.assertNotEqual(result.returncode, 0)


class SkipPolicyTests(unittest.TestCase):
    """The classes above skip only for an absent binary or the explicit opt-out.

    Each case runs a real contract class through a real unittest runner, with a
    stand-in binary first on PATH, so it exercises the same ``setUpClass`` path
    production does. These need no installed codex or post.
    """

    def setUp(self) -> None:
        self.bin_dir = tempfile.TemporaryDirectory(prefix="delegate-contract-policy-")
        self.addCleanup(self.bin_dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(_OPT_OUT_ENV, None)

    def _install(self, name: str, body: str) -> None:
        path = Path(self.bin_dir.name) / name
        path.write_text(body)
        path.chmod(0o755)

    def _run_class(self, case: type[unittest.TestCase]) -> unittest.TestResult:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        return unittest.TextTestRunner(stream=io.StringIO()).run(suite)

    def _assert_fails_with(self, case: type[unittest.TestCase], *needles: str) -> None:
        result = self._run_class(case)
        self.assertEqual(
            result.skipped, [], f"a present-but-broken binary was skipped: {result.skipped}"
        )
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertEqual(result.testsRun, 0)
        report = result.errors[0][1]
        for needle in needles:
            self.assertIn(needle, report)

    def test_an_absent_binary_skips_and_says_it_is_not_installed(self) -> None:
        for case, name in ((CodexArgvContractTests, "codex"), (PostNotifyContractTests, "post")):
            with self.subTest(binary=name):
                result = self._run_class(case)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.skipped), 1, result.skipped)
                self.assertIn(f"{name} is not installed", result.skipped[0][1])

    def test_the_opt_out_skips_even_a_broken_binary_and_names_the_variable(self) -> None:
        self._install("codex", "#!/bin/sh\necho boom >&2\nexit 1\n")
        self._install("post", "#!/bin/sh\necho boom >&2\nexit 1\n")
        for value in ("1", "true", "yes"):
            for case in (CodexArgvContractTests, PostNotifyContractTests):
                with (
                    self.subTest(value=value, case=case.__name__),
                    mock.patch.dict(os.environ, {_OPT_OUT_ENV: value}),
                ):
                    result = self._run_class(case)
                    self.assertEqual(result.errors, [])
                    self.assertEqual(len(result.skipped), 1, result.skipped)
                    self.assertIn(_OPT_OUT_ENV, result.skipped[0][1])
                    self.assertIn("deliberately off", result.skipped[0][1])

    def test_a_falsey_opt_out_value_does_not_skip(self) -> None:
        self._install("codex", "#!/bin/sh\necho boom >&2\nexit 1\n")
        for value in ("0", "false", ""):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {_OPT_OUT_ENV: value}),
            ):
                self._assert_fails_with(CodexArgvContractTests, "boom")

    def test_codex_on_path_that_fails_version_fails_with_its_stderr(self) -> None:
        self._install("codex", "#!/bin/sh\necho 'codex exploded: no config' >&2\nexit 1\n")
        self._assert_fails_with(CodexArgvContractTests, "codex exploded: no config", "exited 1")

    def test_codex_on_path_that_hangs_on_version_fails_as_a_timeout(self) -> None:
        self._install("codex", "#!/bin/sh\nexec /bin/sleep 30\n")
        with mock.patch(f"{__name__}._TIMEOUT_SEC", 1):
            self._assert_fails_with(CodexArgvContractTests, "timed out")

    def test_codex_on_path_that_cannot_be_executed_fails(self) -> None:
        self._install("codex", "#!/nonexistent/interpreter\n")
        self._assert_fails_with(CodexArgvContractTests, "could not be executed")

    def test_post_on_path_that_fails_version_fails_with_its_stderr(self) -> None:
        self._install("post", "#!/bin/sh\necho 'post exploded: no store' >&2\nexit 1\n")
        self._assert_fails_with(PostNotifyContractTests, "post exploded: no store", "exited 1")

    def test_post_on_path_that_cannot_register_the_room_fails_with_its_stderr(self) -> None:
        self._install(
            "post",
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "post 0.0.0"; exit 0; fi\n'
            "echo 'rooms add refused' >&2\nexit 1\n",
        )
        self._assert_fails_with(PostNotifyContractTests, "rooms add refused", "exited 1")

    def test_post_on_path_that_binds_no_participant_fails(self) -> None:
        self._install(
            "post",
            "#!/bin/sh\n"
            'if [ "$1" = "participant" ]; then echo "bound, but no export line"; exit 0; fi\n'
            'echo "post 0.0.0"\nexit 0\n',
        )
        self._assert_fails_with(PostNotifyContractTests, "printed no", "bound, but no export line")


if __name__ == "__main__":
    unittest.main()
