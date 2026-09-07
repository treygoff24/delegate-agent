"""Lane A: cursor and Oh My Pi read the prompt on stdin, never on child argv.

Both harnesses were verified to consume a piped prompt at their current
releases (cursor 2026.09.02-c22c1a3 live; omp 18.1.13 in the shipped
`src/main.ts`), so the argv carve-out — and the process-argv prompt exposure
that came with it — is gone.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import argv_builders as argv_api
from delegate_agent import (
    argv_utils,
    command_help,
    harness_discovery,
    mail_core,
    reasoning,
    request_build,
    structured_output,
)
from delegate_agent import cli_parser as parser_api
from delegate_agent import (
    config as delegate_config,
)
from delegate_agent import describe_payload as describe_api
from delegate_agent import errors as errors_api
from delegate_agent import prompt_transport as transport_api
from tests.delegate_commands_test_base import CommandTestBase, make_git_repo


class CursorStdinTransportTests(CommandTestBase):
    def test_cursor_safe_request_sends_the_prompt_on_stdin_not_argv(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "hello world",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.prompt_transport, transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, request.prompt)
        self.assertIn("hello world", request.stdin_text)
        # The whole point of the move: nothing prompt-shaped reaches /proc/<pid>/cmdline.
        for token in request.argv:
            self.assertNotIn("hello world", token)
        # With no argv prompt there is nothing left to redact, so the parent-facing
        # argv is the real argv.
        self.assertEqual(request.display_argv, request.argv)
        self.assertEqual(request.argv[-1], "stream-json")

    def test_cursor_argv_builder_never_appends_the_prompt(self):
        argv = argv_api.build_cursor_argv(["cursor-agent"], "work", "/repo", "composer-2.5")
        self.assertEqual(
            argv,
            [
                "cursor-agent",
                "--workspace",
                "/repo",
                "-p",
                "--trust",
                "--approve-mcps",
                "--force",
                "--model",
                "composer-2.5",
                "--output-format",
                "stream-json",
            ],
        )

    def test_cursor_resume_keeps_stdin_transport(self):
        # The resume path is a separate argv branch; a prompt reintroduced there
        # would be just as exposed as the one A1 removed.
        argv = argv_api.build_cursor_argv(
            ["cursor-agent"],
            "safe",
            "/repo",
            "composer-2.5",
            resume_session_id="sess-1",
        )
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        self.assertEqual(argv[-1], "stream-json")


class OmpStdinTransportTests(CommandTestBase):
    def test_omp_safe_request_sends_the_prompt_on_stdin_not_argv(self):
        request = self.build_git_request(
            "omp",
            "safe",
            None,
            "/repo",
            "review task",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.prompt_transport, transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(request.stdin_text, request.prompt)
        self.assertIn("review task", request.stdin_text)
        for token in request.argv:
            self.assertNotIn("review task", token)
        self.assertEqual(request.display_argv, request.argv)
        # The read-only lockdown is untouched by the transport move.
        self.assertIn("--approval-mode", request.argv)
        self.assertIn("always-ask", request.argv)

    def test_omp_accepts_a_flag_like_prompt_now_that_it_never_reaches_argv(self):
        # The planted negative for the deleted guard: these prompts used to raise
        # pi_family_prompt_flag_like. On stdin they are inert text, and the run
        # must both succeed and keep them out of argv.
        for prompt in ("--auto-approve", "@/etc/hostname", "-x"):
            with self.subTest(prompt=prompt):
                request = self.build_git_request(
                    "omp",
                    "work",
                    None,
                    "/repo",
                    prompt,
                    delegate_config.embedded_default_config(),
                    dry_run=True,
                )
                self.assertEqual(request.stdin_text, prompt)
                self.assertNotIn(prompt, request.argv)

    def test_omp_argv_builder_takes_no_prompt(self):
        argv = argv_api.build_omp_argv(
            delegate_config.embedded_default_config()["omp"], "call", None, None, "/ws"
        )
        self.assertEqual(
            argv,
            [
                "omp",
                "-p",
                "--no-session",
                "--mode",
                "json",
                "--cwd",
                "/ws",
                "--approval-mode",
                "yolo",
            ],
        )
        self.assertNotIn("task", argv)


class SharedTransportSurfaceTests(unittest.TestCase):
    def test_only_kimi_still_rides_argv(self):
        self.assertEqual(transport_api.ARGV_PROMPT_TRANSPORT_ENGINES, ("kimi",))

    def test_cursor_and_omp_redaction_constants_are_gone(self):
        self.assertFalse(hasattr(transport_api, "CURSOR_PROMPT_REDACTION"))
        self.assertFalse(hasattr(transport_api, "OMP_PROMPT_REDACTION"))
        self.assertTrue(hasattr(transport_api, "KIMI_PROMPT_REDACTION"))

    def test_describe_payload_reports_the_new_transports(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        transports = payload["promptTransports"]
        self.assertEqual(transports["cursor"], transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(transports["omp"], transport_api.PROMPT_TRANSPORT_STDIN)
        self.assertEqual(transports["kimi"], transport_api.PROMPT_TRANSPORT_ARGV)

    def test_mail_flags_are_appended_for_omp_without_an_argv_prompt_to_dodge(self):
        # The old branch spliced --add-dir before the trailing argv prompt. With
        # the prompt on stdin the flags simply append; a splice would now put
        # them before nothing and is no longer needed.
        with tempfile.TemporaryDirectory() as registry_root:
            argv, _display = mail_core.wire_work_mail_launch(
                "omp",
                ["omp", "-p", "--mode", "json"],
                None,
                Path(registry_root),
                prompt_transport=transport_api.PROMPT_TRANSPORT_STDIN,
                isolated_workspace=True,
            )
        self.assertTrue(argv[-1].startswith("--add-dir="))
        self.assertEqual(argv[:4], ["omp", "-p", "--mode", "json"])


if __name__ == "__main__":
    unittest.main()


class CursorReadOnlyModeTests(CommandTestBase):
    """A2, as amended: the harness read-only mode is worth its cost only in call.

    Cursor documents `--mode ask` as "Q&A style for explanations and questions
    (read-only)" and `-p/--print` as having "access to all tools, including
    write and shell". Live testing found that both read-only modes, `ask` and
    `plan`, block the shell outright, so a `cursor safe` reviewer cannot run
    `git diff` or the test suite. Safe mode keeps the isolated workspace copy as
    its boundary and no longer emits the flag; read-only call, which has no
    isolated copy, keeps it.
    """

    def test_cursor_safe_argv_keeps_the_isolated_copy_as_its_boundary(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "review this",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertNotIn("--mode", request.argv)
        self.assertNotIn("--force", request.argv)
        self.assertNotIn("--approve-mcps", request.argv)

    def test_cursor_read_only_call_carries_mode_ask_and_plain_call_does_not(self):
        read_only = argv_api.build_cursor_argv(
            ["cursor-agent"], "call", "/ws", "model", call_read_only=True
        )
        self.assertEqual(read_only[read_only.index("--mode") + 1], "ask")
        self.assertNotIn("--force", read_only)

        # The planted negative: a write-capable call must not be silently
        # downgraded to a read-only harness mode.
        write_call = argv_api.build_cursor_argv(["cursor-agent"], "call", "/ws", "model")
        self.assertNotIn("--mode", write_call)
        self.assertIn("--force", write_call)

    def test_cursor_work_never_gets_a_read_only_mode(self):
        work = argv_api.build_cursor_argv(["cursor-agent"], "work", "/ws", "model")
        self.assertNotIn("--mode", work)
        self.assertIn("--force", work)

    def test_cursor_read_only_call_carries_mode_ask_on_every_argv_branch(self):
        # Both output branches and the resume branch must carry the flag; a mode
        # flag on only one is not a boundary.
        for kwargs in ({}, {"stream_capture": False}, {"resume_session_id": "s1"}):
            with self.subTest(kwargs=sorted(kwargs)):
                argv = argv_api.build_cursor_argv(
                    ["cursor-agent"], "call", "/ws", "model", call_read_only=True, **kwargs
                )
                self.assertEqual(argv[argv.index("--mode") + 1], "ask")

    def test_cursor_safe_emits_no_mode_flag_on_any_argv_branch(self):
        for kwargs in ({}, {"stream_capture": False}, {"resume_session_id": "s1"}):
            with self.subTest(kwargs=sorted(kwargs)):
                argv = argv_api.build_cursor_argv(
                    ["cursor-agent"], "safe", "/ws", "model", **kwargs
                )
                self.assertNotIn("--mode", argv)
                self.assertNotIn("--force", argv)
                self.assertNotIn("--approve-mcps", argv)

    def test_cursor_safe_still_emits_print_once(self):
        # The `-p`/`--print` dedupe from the same change stays: cursor took both
        # spellings of the same flag before it.
        argv = argv_api.build_cursor_argv(["cursor-agent"], "safe", "/ws", "model")
        self.assertEqual(argv.count("-p"), 1)
        self.assertNotIn("--print", argv)

    def test_describe_mode_mapping_shows_no_mode_flag_for_cursor_safe(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        cursor_safe = payload["modeMapping"]["cursor"]["safe"]
        self.assertNotIn("--mode", cursor_safe)
        self.assertNotIn("--force", cursor_safe)
        self.assertNotIn("--mode", payload["modeMapping"]["cursor"]["work"])


class CodexExecScopedOverrideTests(CommandTestBase):
    """A3: codex exec never saw --search or --ask-for-approval.

    Both flags are declared on the interactive TUI parser, and the exec arm
    forwards only the shared options, so codex parsed and dropped them. The
    equivalent config overrides ride `-c` after `exec`, where exec does read them.
    """

    @staticmethod
    def _policy(mode, **overrides):
        config = delegate_config.deep_merge(
            delegate_config.embedded_default_config(),
            {"policy": {mode: overrides}} if overrides else {},
        )
        return config, delegate_config.effective_policy(config, engine="codex", mode=mode)

    def _argv(self, mode, **overrides):
        config, policy = self._policy(mode, **overrides)
        return argv_api.build_codex_argv(
            config["codex"],
            mode,
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
            prompt_transport=transport_api.PROMPT_TRANSPORT_STDIN,
        )

    def test_web_search_rides_a_config_override_after_exec(self):
        argv = self._argv("work", webSearch=True)
        exec_index = argv.index("exec")
        self.assertNotIn("--search", argv)
        self.assertIn('web_search="live"', argv[exec_index:])
        self.assertEqual(argv[argv.index('web_search="live"') - 1], "-c")

    def test_web_search_override_is_absent_when_the_policy_does_not_ask_for_it(self):
        # The planted negative: the safe default must not turn live web search on.
        argv = self._argv("safe")
        self.assertNotIn('web_search="live"', argv)
        self.assertNotIn("--search", argv)

    def test_approval_policy_rides_a_config_override_after_exec(self):
        argv = self._argv("safe")
        exec_index = argv.index("exec")
        self.assertNotIn("--ask-for-approval", argv)
        self.assertIn('approval_policy="never"', argv[exec_index:])
        self.assertEqual(argv[argv.index('approval_policy="never"') - 1], "-c")

    def test_bypass_work_still_omits_the_approval_override(self):
        # The bypass flag conflicts with an approval policy; work runs that carry
        # --dangerously-bypass-approvals-and-sandbox must not also pin one.
        config = delegate_config.deep_merge(
            delegate_config.embedded_default_config(),
            {"policy": {"profile": "external-sandbox"}},
        )
        policy = delegate_config.effective_policy(config, engine="codex", mode="work")
        self.assertIs(policy.get("bypassApprovalsAndSandbox"), True)
        argv = argv_api.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
            prompt_transport=transport_api.PROMPT_TRANSPORT_STDIN,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn('approval_policy="never"', argv)

    def test_resume_path_also_places_the_overrides_after_exec(self):
        config, policy = self._policy("work", webSearch=True)
        argv = argv_api.build_codex_argv(
            config["codex"],
            "work",
            "/repo",
            None,
            "hello",
            policy,
            workspace_kind="git",
            prompt_transport=transport_api.PROMPT_TRANSPORT_STDIN,
            persist_session=True,
            resume_session_id="codex-thread",
        )
        exec_index = argv.index("exec")
        resume_index = argv.index("resume")
        for override in ('web_search="live"', 'approval_policy="never"'):
            with self.subTest(override=override):
                self.assertIn(override, argv[exec_index:resume_index])

    def test_describe_text_no_longer_advertises_the_dropped_flags(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        notes = json.dumps(payload["modeMapping"]["codex"])
        self.assertNotIn("--search", notes)
        self.assertNotIn("--ask-for-approval", notes)
        self.assertIn("approval_policy", notes)


class OmpWorkspaceAndApprovalTests(CommandTestBase):
    """A5: omp work pins its approval mode, and omp names its working directory.

    omp's `tools.approvalMode` schema default is yolo, but a user-level or
    project-level config.yml can set always-ask or write and silently downgrade a
    work run with no Delegate-side signal. Separately, omp auto-chdirs out of the
    home directory when the launch cwd is `$HOME` and no `--cwd`/`--allow-home` is
    given, so a workspace that resolves to home is silently redirected to a temp
    directory while the manifest records the home path.
    """

    def test_omp_work_pins_yolo_approval(self):
        request = self.build_git_request(
            "omp",
            "work",
            None,
            "/repo",
            "implement",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.argv[request.argv.index("--approval-mode") + 1], "yolo")

    def test_omp_safe_keeps_always_ask_and_never_gets_yolo(self):
        # The planted negative: the safe lockdown's load-bearing flag must not be
        # replaced by the work-mode value.
        request = self.build_git_request(
            "omp",
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.argv[request.argv.index("--approval-mode") + 1], "always-ask")
        self.assertNotIn("yolo", request.argv)

    def test_pi_work_never_gets_an_approval_flag(self):
        # pi is the same builder but a different CLI; --approval-mode is omp's.
        request = self.build_git_request(
            "pi",
            "work",
            None,
            "/repo",
            "implement",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertNotIn("--approval-mode", request.argv)

    def test_omp_names_its_working_directory(self):
        request = self.build_git_request(
            "omp",
            "work",
            None,
            "/repo",
            "implement",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.argv[request.argv.index("--cwd") + 1], "/repo")

    def test_pi_gets_no_cwd_flag_because_its_parser_has_none(self):
        request = self.build_git_request(
            "pi",
            "work",
            None,
            "/repo",
            "implement",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertNotIn("--cwd", request.argv)

    def test_omp_cwd_is_rewritten_when_execution_moves_to_another_workspace(self):
        # A --cwd that is not rewritten with the rest of the argv would point the
        # child at the source tree while the process cwd is the isolated copy.
        argv = argv_api.build_omp_argv(
            delegate_config.embedded_default_config()["omp"], "work", None, None, "/source"
        )
        rewritten = argv_utils.replace_workspace_arg_in_argv("omp", argv, "/isolated")
        self.assertEqual(rewritten[rewritten.index("--cwd") + 1], "/isolated")

    def test_describe_shows_the_omp_workspace_and_approval_mode(self):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        omp_work = payload["modeMapping"]["omp"]["work"]
        self.assertEqual(omp_work[omp_work.index("--approval-mode") + 1], "yolo")
        self.assertEqual(omp_work[omp_work.index("--cwd") + 1], "<workspace>")
        omp_safe = payload["modeMapping"]["omp"]["safe"]
        self.assertEqual(omp_safe[omp_safe.index("--cwd") + 1], "<isolated-workspace>")


class OmpCatalogWarningTests(CommandTestBase):
    """A9: warn when a resolved omp selector is not in the discovered catalog.

    omp resolves `--model` by exact `provider/modelId`, then exact bare id, then a
    provider-scoped fuzzy and substring pass, so a stale exact-form id does not
    fail — it can silently land on a different concrete model. omp also runs
    under `continuityMode: fungible`, so the substitution is not a violation
    either. A warning is the whole fix: never a hard reject, never a rewrite of
    the operator's alias.
    """

    @staticmethod
    def _catalog(*selectors):
        return {
            "schema": 1,
            "profile": "default",
            "harnesses": {"omp": {"models": {selector: {} for selector in selectors}}},
        }

    def _request(self, model, discovery):
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "omp",
                "work",
                None,
                "/repo",
                "implement",
                delegate_config.embedded_default_config(),
                dry_run=True,
                model_override=model,
            )

    def test_absent_selector_warns_and_still_launches(self):
        request = self._request(
            "opencode-go/deepseek-v4-pro", self._catalog("fireworks/deepseek-v4-pro")
        )
        self.assertEqual(request.model, "opencode-go/deepseek-v4-pro")
        self.assertIn("--model", request.argv)
        self.assertTrue(
            any(
                "opencode-go/deepseek-v4-pro" in warning and "catalog" in warning
                for warning in request.warnings
            ),
            request.warnings,
        )

    def test_present_selector_does_not_warn(self):
        # The planted negative: a selector that is in the catalog must stay quiet,
        # or the warning is noise on every run.
        request = self._request(
            "fireworks/deepseek-v4-pro", self._catalog("fireworks/deepseek-v4-pro")
        )
        self.assertFalse(
            any("catalog" in warning for warning in request.warnings), request.warnings
        )

    def test_no_catalog_means_no_warning(self):
        # Absence of discovery is not evidence of an absent model.
        request = self._request("fireworks/anything", None)
        self.assertFalse(
            any("catalog" in warning for warning in request.warnings), request.warnings
        )
        request = self._request("fireworks/anything", self._catalog())
        self.assertFalse(
            any("catalog" in warning for warning in request.warnings), request.warnings
        )


class EffortHelpStringTests(unittest.TestCase):
    """A9: help prose is read from the effort enums, not typed out beside them.

    The audit found Grok's help advertising `max`, which grok 1.0.13 rejects
    outright, because the list was maintained by hand next to the tuple it was
    meant to describe. Both surfaces now render the tuple.
    """

    _CASES = (
        ("grok", "--effort", "GROK_NATIVE_EFFORTS"),
        ("pi", "--thinking", "PI_NATIVE_EFFORTS"),
        ("omp", "--thinking", "OMP_NATIVE_EFFORTS"),
    )

    def _payload_notes(self, engine):
        payload = describe_api.describe_payload(
            delegate_config.embedded_default_config(), "embedded default"
        )
        mapping = payload["modeMapping"][engine]
        return list(mapping.get("safeNotes", [])) + list(mapping.get("workNotes", []))

    def test_help_and_describe_name_exactly_the_supported_efforts(self):
        for engine, flag, enum_name in self._CASES:
            efforts = getattr(reasoning, enum_name)
            with self.subTest(engine=engine):
                notes = [
                    note for note in command_help.COMMAND_SPECS[engine].notes if flag in note
                ] + [note for note in self._payload_notes(engine) if flag in note]
                self.assertTrue(notes, engine)
                for note in notes:
                    for effort in efforts:
                        self.assertIn(effort, note)

    def test_grok_help_no_longer_advertises_max(self):
        # The specific defect: grok rejects `max`, and a run that asks for it
        # dies after workspace isolation and prompt materialization.
        self.assertNotIn("max", reasoning.GROK_NATIVE_EFFORTS)
        notes = [
            note for note in command_help.COMMAND_SPECS["grok"].notes if "--effort" in note
        ] + [note for note in self._payload_notes("grok") if "--effort" in note]
        self.assertTrue(notes)
        for note in notes:
            self.assertNotIn("max", note)


class OptionAfterPromptWarningTests(CommandTestBase):
    """A7: a command-local option typed after the prompt is absorbed as prompt text.

    `delegate claude safe --model X "p"` pins the model; `delegate claude safe "p"
    --model X` silently runs the default model with exit 0 and no signal, because
    the trailing prompt is variadic. An unknown option there is already rejected;
    a recognized one is the silent case.
    """

    def _warnings(self, argv):
        parsed = parser_api.parse_cli(argv)
        return tuple(parsed.payload.warnings)

    def test_recognized_option_after_the_prompt_warns(self):
        warnings = self._warnings(["claude", "safe", "x", "--model", "claude-opus-5"])
        self.assertTrue(any("--model" in warning for warning in warnings), warnings)
        self.assertTrue(any("prompt text" in warning for warning in warnings), warnings)

    def test_the_warning_reaches_the_request(self):
        repo = make_git_repo(with_commit=True)
        self.addCleanup(repo.cleanup)
        request = request_build.request_from_parsed(
            parser_api.parse_cli(
                ["--cwd", repo.name, "dry-run", "claude", "safe", "x", "--model", "claude-opus-5"]
            ),
            delegate_config.embedded_default_config(),
            io.StringIO(""),
        )
        self.assertIsNone(request.model_requested)
        self.assertTrue(
            any("option after the prompt" in warning for warning in request.warnings),
            request.warnings,
        )

    def test_prose_mentioning_an_option_does_not_warn(self):
        # The planted negative: matching is per token, not substring, so a prompt
        # that talks about a flag stays quiet.
        warnings = self._warnings(["claude", "safe", "explain what --model does"])
        self.assertEqual(warnings, ())

    def test_tokens_after_an_explicit_separator_do_not_warn(self):
        warnings = self._warnings(["claude", "safe", "x", "--", "--model", "literal"])
        self.assertEqual(warnings, ())

    def test_an_option_before_the_prompt_does_not_warn(self):
        warnings = self._warnings(["claude", "safe", "--model", "claude-opus-5", "x"])
        self.assertEqual(warnings, ())


class PinnedClaudeAliasPreflightTests(CommandTestBase):
    """A10: a pinned run started with an unmappable Claude alias fails up front.

    Claude reports a fully-dated served model id, never the alias. `opus`,
    `sonnet`, `haiku`, and `fable` still carry an evidenced family segment that
    the served id can be matched against; `best`, `opusplan`, and `default` name
    no family at all, so a pinned run using them can only end as a mid-run
    continuity failure that costs the whole launch.
    """

    def _build(self, model, continuity_mode="pinned"):
        return self.build_git_request(
            "claude",
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
            model_override=model,
            continuity_mode=continuity_mode,
        )

    def test_unmappable_alias_is_refused_before_launch(self):
        for alias in ("best", "opusplan", "default", "best[1m]"):
            with self.subTest(alias=alias):
                with self.assertRaises(errors_api.DelegateError) as caught:
                    self._build(alias)
                self.assertEqual(caught.exception.error, "unsupported_continuity_mode")
                self.assertIn(alias, caught.exception.message)

    def test_family_aliases_and_concrete_ids_still_build(self):
        # The planted negative: the family aliases resolve to a served id whose
        # family segment matches, so pinning them is legitimate and must not be
        # swept up by the same check.
        for model in ("opus", "sonnet", "haiku", "fable", "opus[1m]", "claude-opus-5"):
            with self.subTest(model=model):
                request = self._build(model)
                self.assertEqual(request.model, model)

    def test_fungible_runs_accept_every_alias(self):
        for alias in ("best", "opusplan", "default"):
            with self.subTest(alias=alias):
                request = self._build(alias, continuity_mode="fungible")
                self.assertEqual(request.model, alias)

    def test_other_engines_are_untouched(self):
        request = self.build_git_request(
            "cursor",
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
            model_override="best",
            continuity_mode="pinned",
        )
        self.assertEqual(request.model, "best")


class ClaudePermissionPromptsTests(CommandTestBase):
    """A6: make the safe-mode denial explicit instead of assuming no approver.

    Claude Code 2.1.263 documents `--permission-prompts none` as "nobody:
    anything that would prompt is denied automatically; the permission mode still
    decides everything else". Delegate's read-only modes relied on there being no
    approver in `-p`, which is a property of the invocation rather than a stated
    contract. The flag is only emitted when discovery proved the installed binary
    lists it, the same way the native persona-file transport is gated.
    """

    @staticmethod
    def _discovery(supported):
        return {
            "schema": 1,
            "profile": "default",
            "harnesses": {"claude": {"capabilities": {"permissionPrompts": supported}}},
        }

    def _argv(self, mode, discovery, **kwargs):
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "claude",
                mode,
                None,
                "/repo",
                "review",
                delegate_config.embedded_default_config(),
                dry_run=True,
                **kwargs,
            ).argv

    def test_safe_emits_the_flag_when_discovery_proves_it(self):
        argv = self._argv("safe", self._discovery(True))
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")

    def test_read_only_call_emits_the_flag(self):
        argv = self._argv("call", self._discovery(True), call_read_only=True)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")

    def test_work_and_write_capable_call_never_emit_the_flag(self):
        # The planted negative: a write-capable run must keep its approver.
        self.assertNotIn("--permission-prompts", self._argv("work", self._discovery(True)))
        self.assertNotIn("--permission-prompts", self._argv("call", self._discovery(True)))

    def test_unproven_capability_omits_the_flag(self):
        # An unknown flag is an immediate usage error on Claude, so a run must
        # never gamble on it; no discovery record means no flag.
        self.assertNotIn("--permission-prompts", self._argv("safe", self._discovery(False)))
        self.assertNotIn("--permission-prompts", self._argv("safe", None))

    def test_pure_call_is_unchanged(self):
        argv = argv_api.build_claude_argv(
            delegate_config.embedded_default_config()["claude"],
            "call",
            None,
            {},
            pure=True,
            call_read_only=True,
            permission_prompts_supported=True,
        )
        self.assertNotIn("--permission-prompts", argv)


class OpencodeClaudeInstructionLeakTests(CommandTestBase):
    """B5 (made by Lane A on Lane B's behalf): keep ~/.claude out of read-only runs.

    OpenCode reads `~/.claude/CLAUDE.md` and `.claude/skills` by default, so a
    safe review pulled in the operator's global Claude Code instructions and any
    skills present in the mirrored workspace. `--pure` does not cover this; it
    only skips external plugins. The permission deny-all still bound, so no write
    tool was granted — this is instruction-surface leakage into a boundary the
    security model describes as locked down.
    """

    def test_read_only_runs_disable_the_claude_directory(self):
        request = self.build_git_request(
            "opencode",
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertEqual(request.env_overrides["OPENCODE_DISABLE_CLAUDE_CODE"], "1")

    def test_read_only_call_disables_it_too(self):
        request = request_build.request_from_parsed(
            parser_api.parse_cli(["opencode", "call", "--read-only", "score"]),
            delegate_config.embedded_default_config(),
            io.StringIO(""),
        )
        self.addCleanup(shutil.rmtree, request.workspace, ignore_errors=True)
        self.assertEqual(request.env_overrides["OPENCODE_DISABLE_CLAUDE_CODE"], "1")

    def test_work_runs_keep_their_instruction_surface(self):
        # The planted negative: work mode is meant to see the operator's own
        # instructions, so the override must not leak out of the read-only set.
        request = self.build_git_request(
            "opencode",
            "work",
            None,
            "/repo",
            "implement",
            delegate_config.embedded_default_config(),
            dry_run=True,
        )
        self.assertNotIn("OPENCODE_DISABLE_CLAUDE_CODE", request.env_overrides)


class ClaudeNativeSchemaPreflightTests(CommandTestBase):
    """Lane S's eligibility helper, wired into the direct --output-schema path.

    Claude enforces `--json-schema` natively, so a schema the API will reject
    should be refused at preflight rather than after the launch. The rule itself
    belongs to Lane S; this only calls it and reports the reason.
    """

    def _schema_path(self, schema):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "schema.json"
        path.write_text(json.dumps(schema), encoding="utf-8")
        return str(path)

    def _build(self, schema, engine="claude"):
        return self.build_git_request(
            engine,
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
            output_schema=self._schema_path(schema),
        )

    def test_ineligible_schema_is_refused_with_schema_not_native(self):
        # Lane S's helper rejects a non-object root; this asserts the direct path
        # reports that verdict instead of forwarding an argv Claude will reject.
        with self.assertRaises(errors_api.DelegateError) as caught:
            self._build({"type": "array", "items": {"type": "string"}})
        self.assertEqual(caught.exception.error, "schema_not_native")
        self.assertIn("object", caught.exception.message)

    def test_an_oversize_schema_is_refused_before_the_argv_is_built(self):
        # The other half of Lane S's rule: Claude carries the schema in argv, so
        # a schema over the argv ceiling cannot be launched at all.
        oversize = {
            "type": "object",
            "properties": {f"field_{index}": {"type": "string"} for index in range(12000)},
        }
        with self.assertRaises(errors_api.DelegateError) as caught:
            self._build(oversize)
        self.assertEqual(caught.exception.error, "schema_not_native")
        self.assertIn("argv limit", caught.exception.message)

    def _schema_text_path(self, text: str) -> str:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = Path(directory) / "schema.json"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_pretty_printed_schema_is_measured_by_its_own_bytes(self):
        """Claude carries the file's bytes in argv; whitespace counts against the limit."""
        limit = structured_output.CLAUDE_NATIVE_SCHEMA_ARGV_MAX_BYTES
        schema = {
            "type": "object",
            "properties": {f"field_{index}": {"type": "string"} for index in range(2600)},
        }
        pretty = json.dumps(schema, indent=2)
        self.assertLess(len(json.dumps(schema).encode("utf-8")), limit)
        self.assertGreaterEqual(len(pretty.encode("utf-8")), limit)

        with self.assertRaises(errors_api.DelegateError) as caught:
            self.build_git_request(
                "claude",
                "safe",
                None,
                "/repo",
                "review",
                delegate_config.embedded_default_config(),
                dry_run=True,
                output_schema=self._schema_text_path(pretty),
            )

        self.assertEqual(caught.exception.error, "schema_not_native")
        self.assertIn("argv limit", caught.exception.message)

    def test_a_pretty_printed_schema_under_the_limit_still_builds(self):
        """The planted negative: measuring the real bytes must not reject everything."""
        schema = {
            "type": "object",
            "properties": {f"field_{index}": {"type": "string"} for index in range(1000)},
        }
        pretty = json.dumps(schema, indent=2)
        self.assertLess(
            len(pretty.encode("utf-8")), structured_output.CLAUDE_NATIVE_SCHEMA_ARGV_MAX_BYTES
        )

        request = self.build_git_request(
            "claude",
            "safe",
            None,
            "/repo",
            "review",
            delegate_config.embedded_default_config(),
            dry_run=True,
            output_schema=self._schema_text_path(pretty),
        )

        self.assertIn("--json-schema", request.argv)
        self.assertIn(pretty, request.argv)

    def test_eligible_schema_still_builds(self):
        # The planted negative: an eligible schema must reach argv untouched.
        request = self._build({"type": "object", "properties": {"ok": {"type": "boolean"}}})
        self.assertIn("--json-schema", request.argv)

    def test_codex_keeps_its_own_preflight(self):
        # The claude branch must not intercept codex, which has a different rule
        # (strict-mode normalization) and its own error.
        request = self._build(
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            engine="codex",
        )
        self.assertIn("--output-schema", request.argv)
