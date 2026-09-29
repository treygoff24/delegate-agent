"""An explicit omp provider/model id is pinned; a retired alias is not guessed at.

Live defects behind these tests (2026-09-28):

* `delegate omp safe --model opencode-go/glm-5.3` was served by fireworks/glm-5p3
  through omp's own retry `fallbackChains`, against a no-Fireworks spend order.
* `delegate omp --model kimi` (a retired alias) fell through to omp's fuzzy
  catalog match and ran fireworks/kimi-k3.

The fixes are a launch overlay that switches omp's retry failover off, a
`pinned` default for a provider/model id the caller typed, and an `invalid_alias`
refusal for a bare name that is neither a configured alias nor a real catalog id.
A delegate alias (a key of `omp.models`) and `omp.defaultModel` stay fungible
even though their targets carry a provider: aliases are how the fleet gets
failover across subscriptions.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delegate_agent import argv_builders as argv_api
from delegate_agent import (
    cli,
    harness_discovery,
    model_discovery,
    prompt_transport,
    request_build,
    run_context,
    run_status,
)
from delegate_agent import config as delegate_config
from delegate_agent import request_models as request_types
from delegate_agent.errors import DelegateError
from tests.delegate_commands_test_base import CommandTestBase

OVERLAY_PLACEHOLDER = prompt_transport.OMP_CONFIG_OVERLAY_ARG_PLACEHOLDER
# The loaded config validates alias objects, so the fixture names a thinking level.
_GLM_ALIAS = {"glm": {"model": "opencode-go/glm-5.3", "thinking": "low"}}


def _catalog(*selectors):
    return {
        "schema": 1,
        "profile": "default",
        "harnesses": {"omp": {"models": {selector: {} for selector in selectors}}},
    }


def _config_with_aliases(**aliases):
    config = json.loads(json.dumps(delegate_config.embedded_default_config()))
    config["omp"]["models"] = {
        name: ({"model": target} if isinstance(target, str) else target)
        for name, target in aliases.items()
    }
    return config


class OmpProviderPinRequestTests(CommandTestBase):
    def _request(self, model=None, *, discovery=None, config=None, alias=None, **kwargs):
        if config is None:
            config = delegate_config.embedded_default_config()
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "omp",
                "work",
                alias,
                "/repo",
                "implement",
                config,
                dry_run=True,
                model_override=model,
                **kwargs,
            )

    # -- the default: an explicit provider id is pinned -----------------------

    def test_explicit_provider_id_is_pinned_and_launched_without_failover(self):
        request = self._request("opencode-go/glm-5.3")

        self.assertEqual(request.continuity_mode, "pinned")
        self.assertEqual(request.model, "opencode-go/glm-5.3")
        # The overlay rides as a `--config <file>` pair whose content is only
        # known at launch time; the argv carries the placeholder for it.
        self.assertEqual(request.argv[-2:], ["--config", OVERLAY_PLACEHOLDER])
        self.assertEqual(
            json.loads(request.agent_config_text),
            {"retry": {"modelFallback": False, "usageAwareFallback": False}},
        )

    def test_the_overlay_shows_as_a_named_placeholder_in_display_argv(self):
        request = self._request("opencode-go/glm-5.3")

        self.assertNotIn(OVERLAY_PLACEHOLDER, request.display_argv)
        self.assertEqual(
            request.display_argv[-2:], ["--config", prompt_transport.OMP_CONFIG_OVERLAY_DISPLAY]
        )

    def test_a_multi_segment_id_is_provider_qualified_at_the_first_slash(self):
        request = self._request("gateway/acme/model-pro")
        self.assertEqual(request.continuity_mode, "pinned")
        self.assertIn("--config", request.argv)

    def test_a_typed_id_in_the_positional_slot_is_pinned_like_a_flag_id(self):
        request = self._request(None, alias="opencode-go/glm-5.3")
        self.assertEqual(request.continuity_mode, "pinned")
        self.assertEqual(request.argv[-2:], ["--config", OVERLAY_PLACEHOLDER])

    # -- aliases and the configured default keep multi-subscription failover ---

    def test_an_alias_with_a_provider_target_stays_fungible(self):
        # Every live alias points at a provider/model target, and aliases are how
        # the fleet gets failover across subscriptions: no overlay, no refusal.
        config = _config_with_aliases(builder="openai-codex/gpt-5.6-sol")
        request = self._request("builder", config=config)
        self.assertEqual(request.model, "openai-codex/gpt-5.6-sol")
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)
        self.assertIsNone(request.agent_config_text)

    def test_the_positional_alias_form_stays_fungible_too(self):
        config = _config_with_aliases(builder="openai-codex/gpt-5.6-sol")
        request = self._request(None, config=config, alias="builder")
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)

    def test_an_alias_key_wins_over_the_raw_id_reading(self):
        # A key of omp.models is an alias even when it looks like provider/model,
        # exactly as it is when the model is resolved.
        config = _config_with_aliases(**{"opencode-go/glm-5.3": "fireworks/glm-5p3"})
        request = self._request("opencode-go/glm-5.3", config=config)
        self.assertEqual(request.model, "fireworks/glm-5p3")
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)

    def test_the_configured_default_model_stays_fungible(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["omp"]["defaultModel"] = "opencode-go/glm-5.3"
        request = self._request(None, config=config)
        self.assertEqual(request.model, "opencode-go/glm-5.3")
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)
        self.assertIsNone(request.agent_config_text)

    def test_naming_pinned_on_an_alias_still_pins_it(self):
        config = _config_with_aliases(builder="openai-codex/gpt-5.6-sol")
        request = self._request("builder", config=config, continuity_mode="pinned")
        self.assertEqual(request.continuity_mode, "pinned")
        self.assertEqual(request.argv[-2:], ["--config", OVERLAY_PLACEHOLDER])
        self.assertIsNotNone(request.agent_config_text)

    def test_naming_pinned_on_the_configured_default_still_pins_it(self):
        config = json.loads(json.dumps(delegate_config.embedded_default_config()))
        config["omp"]["defaultModel"] = "opencode-go/glm-5.3"
        request = self._request(None, config=config, continuity_mode="pinned")
        self.assertEqual(request.argv[-2:], ["--config", OVERLAY_PLACEHOLDER])

    # -- the opt-in: naming a mode allows failover ----------------------------

    def test_naming_fungible_or_panel_is_the_opt_in_to_failover(self):
        for mode in ("fungible", "panel"):
            with self.subTest(mode=mode):
                request = self._request("opencode-go/glm-5.3", continuity_mode=mode)
                self.assertEqual(request.continuity_mode, mode)
                self.assertNotIn("--config", request.argv)
                self.assertIsNone(request.agent_config_text)
                self.assertNotIn(prompt_transport.OMP_CONFIG_OVERLAY_DISPLAY, request.display_argv)

    def test_naming_pinned_switches_failover_off_even_for_a_bare_id(self):
        request = self._request(
            "glm-5.3",
            discovery=_catalog("opencode-go/glm-5.3"),
            continuity_mode="pinned",
        )
        self.assertEqual(request.continuity_mode, "pinned")
        self.assertEqual(request.argv[-2:], ["--config", OVERLAY_PLACEHOLDER])

    # -- no model, bare ids, and other engines keep their behavior ------------

    def test_a_run_without_a_model_stays_fungible_and_carries_no_overlay(self):
        request = self._request(None)
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)
        self.assertIsNone(request.agent_config_text)

    def test_a_bare_catalog_id_keeps_the_fungible_default(self):
        # Exact bare ids resolve inside omp without naming a provider, so there is
        # no provider for Delegate to pin.
        request = self._request("glm-5.3", discovery=_catalog("opencode-go/glm-5.3"))
        self.assertEqual(request.continuity_mode, "fungible")
        self.assertNotIn("--config", request.argv)

    def test_other_engines_are_unaffected(self):
        for engine in ("pi", "cursor"):
            with self.subTest(engine=engine):
                request = self.build_git_request(
                    engine,
                    "work",
                    None,
                    "/repo",
                    "implement",
                    delegate_config.embedded_default_config(),
                    dry_run=True,
                    model_override="opencode-go/glm-5.3",
                )
                self.assertEqual(request.continuity_mode, "fungible")
                self.assertNotIn("--config", request.argv)
                self.assertIsNone(request.agent_config_text)

    # -- retired and unknown aliases fail instead of falling through ----------

    def test_a_retired_alias_is_refused_when_the_catalog_does_not_list_it(self):
        catalog = _catalog("fireworks/kimi-k3", "opencode-go/glm-5.3")
        with self.assertRaises(DelegateError) as caught:
            self._request("kimi", discovery=catalog)

        self.assertEqual(caught.exception.error, "invalid_alias")
        message = caught.exception.message
        self.assertIn("'kimi'", message)
        self.assertIn("fuzzy", message)
        self.assertIn("delegate capabilities refresh", message)

    def test_the_alias_refusal_lists_configured_aliases(self):
        config = _config_with_aliases(builder="openai-codex/gpt-5.6-sol")
        with self.assertRaises(DelegateError) as caught:
            self._request("kimi", discovery=_catalog("fireworks/kimi-k3"), config=config)
        self.assertIn("builder", caught.exception.message)

    def test_the_positional_form_of_a_retired_alias_is_refused_too(self):
        with self.assertRaises(DelegateError) as caught:
            self._request(None, alias="kimi", discovery=_catalog("fireworks/kimi-k3"))
        self.assertEqual(caught.exception.error, "invalid_alias")

    def test_a_configured_alias_is_accepted_whatever_the_catalog_says(self):
        config = _config_with_aliases(fast="glm-5.3")
        request = self._request("fast", discovery=_catalog("fireworks/kimi-k3"), config=config)
        self.assertEqual(request.model, "glm-5.3")

    def test_a_bare_id_the_catalog_lists_exactly_is_accepted(self):
        for model in ("glm-5.3", "opencode-go/glm-5.3"):
            with self.subTest(model=model):
                request = self._request(model, discovery=_catalog("opencode-go/glm-5.3"))
                self.assertEqual(request.model, model)

    def test_a_provider_qualified_id_is_never_second_guessed_by_the_catalog(self):
        # A stale catalog must not refuse an explicit provider/model id; the
        # existing catalog-absence warning covers it.
        request = self._request(
            "opencode-go/brand-new-model", discovery=_catalog("fireworks/kimi-k3")
        )
        self.assertEqual(request.model, "opencode-go/brand-new-model")
        self.assertTrue(any("catalog" in warning for warning in request.warnings))

    def test_a_bare_name_with_no_catalog_warns_instead_of_refusing(self):
        # No catalog is no evidence: refusing would break every unprobed machine.
        for discovery in (None, _catalog()):
            with self.subTest(discovery=discovery):
                request = self._request("kimi", discovery=discovery)
                self.assertEqual(request.model, "kimi")
                warnings = [w for w in request.warnings if "no discovered omp catalog" in w]
                self.assertEqual(len(warnings), 1)
                self.assertIn("delegate capabilities refresh", warnings[0])

    def test_a_bare_catalog_id_with_a_catalog_does_not_warn(self):
        request = self._request("glm-5.3", discovery=_catalog("opencode-go/glm-5.3"))
        self.assertFalse(
            any("no discovered omp catalog" in warning for warning in request.warnings)
        )

    def test_the_input_json_path_defaults_the_same_way(self):
        # This is also the path a workflow agent(model=...) takes: a literal
        # provider/model is pinned, an alias is not.
        config = _config_with_aliases(builder="openai-codex/gpt-5.6-sol")
        with tempfile.TemporaryDirectory() as tmp:
            for model, extra, expected in (
                ("opencode-go/glm-5.3", {}, "pinned"),
                ("opencode-go/glm-5.3", {"continuityMode": "fungible"}, "fungible"),
                ("opencode-go/glm-5.3", {"continuityMode": "pinned"}, "pinned"),
                ("builder", {}, "fungible"),
                ("builder", {"continuityMode": "pinned"}, "pinned"),
            ):
                with self.subTest(model=model, extra=extra):
                    task = Path(tmp) / "task.json"
                    task.write_text(
                        json.dumps(
                            {
                                "engine": "omp",
                                "mode": "work",
                                "model": model,
                                "cwd": tmp,
                                "prompt": "hello",
                                **extra,
                            }
                        )
                    )
                    parsed = request_types.ParsedCommand(
                        "run",
                        global_options=request_types.GlobalOptions(json_mode=True),
                        payload=request_types.RunJsonOptions(str(task)),
                    )
                    with mock.patch.object(
                        harness_discovery, "load_discovery_cache", return_value=None
                    ):
                        request = request_build.request_from_input_json(parsed, config)
                    self.assertEqual(request.continuity_mode, expected)
                    self.assertEqual("--config" in request.argv, expected == "pinned")


class OmpArgvOverlayTests(unittest.TestCase):
    def _argv(self, **kwargs):
        return argv_api.build_omp_argv(
            delegate_config.embedded_default_config()["omp"],
            "work",
            "opencode-go/glm-5.3",
            None,
            "/repo",
            **kwargs,
        )

    def test_the_overlay_flag_is_only_present_when_asked_for(self):
        self.assertNotIn("--config", self._argv())
        self.assertNotIn("--config", self._argv(no_model_fallback=False))
        argv = self._argv(no_model_fallback=True)
        self.assertEqual(argv[-2:], ["--config", OVERLAY_PLACEHOLDER])

    def test_the_overlay_disables_both_failover_paths(self):
        overlay = json.loads(argv_api.OMP_NO_MODEL_FALLBACK_OVERLAY)
        self.assertIs(overlay["retry"]["modelFallback"], False)
        self.assertIs(overlay["retry"]["usageAwareFallback"], False)


class OmpPinnedLaunchTests(unittest.TestCase):
    """The whole launch path, against a local child that only looks like omp."""

    def _fake_omp(self, root: Path, provider: str, model: str) -> Path:
        executable = root / "fake-omp"
        message_start = {
            "type": "message_start",
            "message": {"role": "assistant", "provider": provider, "model": model, "content": []},
        }
        turn_end = {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "the answer"}],
            },
        }
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            f"record = os.path.join({str(root)!r}, 'launch.json')\n"
            "overlay = None\n"
            "if '--config' in argv:\n"
            "    with open(argv[argv.index('--config') + 1], encoding='utf-8') as handle:\n"
            "        overlay = handle.read()\n"
            "with open(record, 'w', encoding='utf-8') as handle:\n"
            "    json.dump({'argv': argv, 'overlay': overlay}, handle)\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({message_start!r}))\n"
            f"print(json.dumps({turn_end!r}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _run(
        self,
        subcommand: list[str],
        provider: str,
        model: str,
        *extra: str,
        aliases: dict | None = None,
        group: str | None = None,
        input_json: dict | None = None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = (
                _config_with_aliases(**aliases)
                if aliases
                else delegate_config.embedded_default_config()
            )
            cfg["omp"]["binary"] = str(self._fake_omp(root, provider, model))
            out, err = io.StringIO(), io.StringIO()
            # An ungrouped `call` runs in the process cwd and refuses --cwd; a
            # tracked run, grouped calls included (a workflow agent() always
            # passes --group), names its workspace.
            where = [] if subcommand == ["call"] and group is None else ["--cwd", directory]
            if group is not None:
                where = [*where, "--group", group]
            argv = ["--json", *where, "omp", *subcommand, *extra, "Fixture prompt"]
            if input_json is not None:
                # `run --input-json` is what a workflow agent() launches through.
                input_path = root / "input.json"
                input_path.write_text(
                    json.dumps({"engine": "omp", "prompt": "Fixture prompt", **input_json}),
                    encoding="utf-8",
                )
                grouped = ["--group", group] if group is not None else []
                argv = ["--json", *grouped, "run", "--input-json", str(input_path)]
            with (
                mock.patch.object(request_build, "load_config", return_value=(cfg, "fixture")),
                mock.patch.object(harness_discovery, "load_discovery_cache", return_value=None),
            ):
                code = cli.main(argv, stdout=out, stderr=err)
            record = root / "launch.json"
            self.assertTrue(
                record.exists(), f"omp never launched: {out.getvalue()} {err.getvalue()}"
            )
            launch = json.loads(record.read_text(encoding="utf-8"))
            # The substitution warning is delegate-authored text in the run's
            # completion report, which lives in the workspace removed below.
            launch["report"] = ""
            report_path = json.loads(out.getvalue() or "{}").get("completionReportPath")
            if report_path and (root / report_path).exists():
                launch["report"] = (root / report_path).read_text(encoding="utf-8")
            return code, out.getvalue(), err.getvalue(), launch

    def test_a_tracked_run_gets_the_overlay_file_and_refuses_a_swap(self):
        code, out, err, launch = self._run(
            ["work"], "fireworks", "glm-5p3", "--model", "opencode-go/glm-5.3"
        )

        # The child saw a real overlay file, not the placeholder.
        self.assertEqual(
            json.loads(launch["overlay"]),
            {"retry": {"modelFallback": False, "usageAwareFallback": False}},
        )
        # And the run was refused because it was served by another provider anyway.
        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertIn("fireworks/glm-5p3", payload["message"])
        self.assertEqual(payload["servedProvider"], "fireworks")

    def test_a_tracked_run_served_by_the_pinned_provider_completes(self):
        code, out, err, _launch = self._run(
            ["work"], "opencode-go", "glm-5.3", "--model", "opencode-go/glm-5.3"
        )

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["servedProvider"], "opencode-go")
        self.assertEqual(payload["continuityMode"], "pinned")

    def test_a_tracked_alias_run_keeps_failover_and_names_the_provider_that_answered(self):
        # An alias carries multi-subscription failover: omp's retry chain stays on,
        # a cross-provider serve is not refused, and the run says who answered.
        code, out, err, launch = self._run(
            ["work"], "fireworks", "glm-5p3", "--model", "glm", aliases=_GLM_ALIAS
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("--config", launch["argv"])
        self.assertIsNone(launch["overlay"])
        payload = json.loads(out)
        self.assertEqual(payload["continuityMode"], "fungible")
        self.assertEqual(payload["servedProvider"], "fireworks")
        self.assertEqual(payload["servedModel"], "glm-5p3")
        # Visibility without refusal: the report names the provider that answered.
        self.assertIn("model_substitution", launch["report"])
        self.assertIn("fireworks/glm-5p3", launch["report"])

    def test_a_tracked_alias_run_with_pinned_named_gets_the_overlay_and_refuses_a_swap(self):
        code, out, err, launch = self._run(
            ["work"],
            "fireworks",
            "glm-5p3",
            "--model",
            "glm",
            "--continuity-mode",
            "pinned",
            aliases=_GLM_ALIAS,
        )

        self.assertEqual(
            json.loads(launch["overlay"]),
            {"retry": {"modelFallback": False, "usageAwareFallback": False}},
        )
        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertIn("fireworks/glm-5p3", payload["message"])

    def test_call_with_an_alias_leaves_failover_alone_and_names_the_provider(self):
        code, out, err, launch = self._run(
            ["call"], "fireworks", "glm-5p3", "--model", "glm", aliases=_GLM_ALIAS
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("--config", launch["argv"])
        payload = json.loads(out)
        self.assertEqual(payload["servedProvider"], "fireworks")
        self.assertEqual(payload["servedModel"], "glm-5p3")
        # Visibility without refusal, as on a tracked run: the call succeeded and
        # says, provider included, that another model answered.
        self.assertTrue(payload["ok"])
        substitutions = [w for w in payload["warnings"] if w.startswith("model_substitution")]
        self.assertEqual(len(substitutions), 1, payload["warnings"])
        self.assertIn("requested glm resolved to opencode-go/glm-5.3", substitutions[0])
        self.assertIn("served fireworks/glm-5p3", substitutions[0])

    def test_call_with_an_alias_answered_by_its_own_target_is_not_flagged(self):
        code, out, err, _launch = self._run(
            ["call"], "opencode-go", "glm-5.3", "--model", "glm", aliases=_GLM_ALIAS
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("model_substitution", json.dumps(json.loads(out).get("warnings")))

    def test_a_pinned_call_on_an_alias_is_refused_when_another_provider_answers(self):
        code, out, err, launch = self._run(
            ["call"],
            "fireworks",
            "glm-5p3",
            "--model",
            "glm",
            "--continuity-mode",
            "pinned",
            aliases=_GLM_ALIAS,
        )

        self.assertIsNotNone(launch["overlay"])
        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertIn("requested glm", payload["message"])
        self.assertIn("fireworks/glm-5p3", payload["message"])

    def test_call_launches_omp_with_the_failover_overlay_file(self):
        code, out, err, launch = self._run(
            ["call"], "opencode-go", "glm-5.3", "--model", "opencode-go/glm-5.3"
        )

        self.assertEqual(code, 0, err)
        # The placeholder never reaches the child: it gets a real file path whose
        # content switches omp's retry failover off.
        config_path = launch["argv"][launch["argv"].index("--config") + 1]
        self.assertNotEqual(config_path, OVERLAY_PLACEHOLDER)
        self.assertEqual(
            json.loads(launch["overlay"]),
            {"retry": {"modelFallback": False, "usageAwareFallback": False}},
        )
        payload = json.loads(out)
        self.assertEqual(payload["servedProvider"], "opencode-go")
        self.assertEqual(payload["servedModel"], "glm-5.3")
        # The stream named the served model, so the pin was checked: the call
        # must not claim it could not verify one.
        self.assertNotIn("pinned_continuity_unverified", json.dumps(payload.get("warnings")))

    def test_call_with_the_fungible_opt_in_leaves_omp_failover_alone(self):
        code, _out, err, launch = self._run(
            ["call"],
            "opencode-go",
            "glm-5.3",
            "--model",
            "opencode-go/glm-5.3",
            "--continuity-mode",
            "fungible",
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("--config", launch["argv"])
        self.assertIsNone(launch["overlay"])

    def test_a_pinned_call_is_refused_when_another_provider_answers(self):
        # The overlay is the prevention; if a child exits 0 while its stream says
        # another provider answered anyway, the call is refused with the code a
        # tracked pinned run uses, not returned as a success.
        code, out, err, _launch = self._run(
            ["call"], "fireworks", "glm-5p3", "--model", "opencode-go/glm-5.3"
        )

        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertEqual(payload["failureKind"], "model_continuity")
        self.assertIn("requested opencode-go/glm-5.3", payload["message"])
        self.assertIn("tried to serve fireworks/glm-5p3", payload["message"])
        # And the envelope still says who answered, rather than echoing the request.
        self.assertEqual(payload["servedProvider"], "fireworks")
        self.assertEqual(payload["servedModel"], "glm-5p3")

    def test_a_pinned_call_through_input_json_is_refused_too(self):
        code, out, err, launch = self._run(
            [],
            "fireworks",
            "glm-5p3",
            input_json={"mode": "call", "model": "opencode-go/glm-5.3"},
        )

        self.assertIsNotNone(launch["overlay"])
        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertIn("tried to serve fireworks/glm-5p3", payload["message"])

    def test_a_pinned_call_refusal_prints_the_message_in_text_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = delegate_config.embedded_default_config()
            cfg["omp"]["binary"] = str(self._fake_omp(root, "fireworks", "glm-5p3"))
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(request_build, "load_config", return_value=(cfg, "fixture")),
                mock.patch.object(harness_discovery, "load_discovery_cache", return_value=None),
            ):
                code = cli.main(
                    ["omp", "call", "--model", "opencode-go/glm-5.3", "Fixture prompt"],
                    stdout=out,
                    stderr=err,
                )

        self.assertNotEqual(code, 0)
        self.assertIn("tried to serve fireworks/glm-5p3", err.getvalue())
        self.assertNotIn("the answer", out.getvalue())

    def test_a_fungible_call_names_the_provider_that_answered_when_it_differs(self):
        code, out, err, _launch = self._run(
            ["call"],
            "fireworks",
            "glm-5p3",
            "--model",
            "opencode-go/glm-5.3",
            "--continuity-mode",
            "fungible",
        )

        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["servedProvider"], "fireworks")
        self.assertEqual(payload["servedModel"], "glm-5p3")
        self.assertIn("served fireworks/glm-5p3", json.dumps(payload["warnings"]))

    # -- the grouped call a workflow agent() launches is a tracked run -----------

    def test_a_grouped_call_with_a_typed_id_is_refused_like_a_tracked_run(self):
        code, out, err, launch = self._run(
            ["call"], "fireworks", "glm-5p3", "--model", "opencode-go/glm-5.3", group="wf-1"
        )

        self.assertIsNotNone(launch["overlay"])
        self.assertNotEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "model_continuity_paused")
        self.assertIn("fireworks/glm-5p3", payload["message"])

    def test_a_grouped_call_with_an_alias_keeps_failover_and_warns(self):
        code, out, err, launch = self._run(
            ["call"], "fireworks", "glm-5p3", "--model", "glm", aliases=_GLM_ALIAS, group="wf-1"
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("--config", launch["argv"])
        self.assertEqual(json.loads(out)["servedProvider"], "fireworks")
        self.assertIn("model_substitution", launch["report"])
        self.assertIn("fireworks/glm-5p3", launch["report"])

    def test_a_retired_alias_fails_before_anything_launches(self):
        cfg = delegate_config.embedded_default_config()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(request_build, "load_config", return_value=(cfg, "fixture")),
            mock.patch.object(
                harness_discovery,
                "load_discovery_cache",
                return_value=_catalog("fireworks/kimi-k3"),
            ),
        ):
            code = cli.main(
                ["--json", "omp", "call", "--model", "kimi", "hi"], stdout=out, stderr=err
            )
        self.assertNotEqual(code, 0)
        text = out.getvalue() + err.getvalue()
        self.assertIn("invalid_alias", text)
        self.assertIn("kimi", text)


class ServedProvenancePlumbingTests(CommandTestBase):
    """The catalog display name and the served pair travel from request to record."""

    def _cursor_request(self, discovery):
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=discovery):
            return self.build_git_request(
                "cursor",
                "work",
                None,
                "/repo",
                "implement",
                delegate_config.embedded_default_config(),
                dry_run=True,
                model_override="grok-4.7-xhigh",
            )

    def test_a_cursor_request_carries_the_catalog_display_name(self):
        discovery = {
            "schema": 1,
            "profile": "default",
            "harnesses": {
                "cursor": {
                    "models": {"grok-4.7-xhigh": {"displayName": "Grok 4.7 256K Extra High"}}
                }
            },
        }
        self.assertEqual(
            self._cursor_request(discovery).model_display_name, "Grok 4.7 256K Extra High"
        )
        self.assertIsNone(self._cursor_request(None).model_display_name)

    def test_only_cursor_requests_carry_a_display_name(self):
        catalog = _catalog("opencode-go/glm-5.3")
        catalog["harnesses"]["omp"]["models"]["opencode-go/glm-5.3"] = {"displayName": "GLM 5.3"}
        with mock.patch.object(harness_discovery, "load_discovery_cache", return_value=catalog):
            request = self.build_git_request(
                "omp",
                "work",
                None,
                "/repo",
                "implement",
                delegate_config.embedded_default_config(),
                dry_run=True,
                model_override="opencode-go/glm-5.3",
            )
        self.assertIsNone(request.model_display_name)

    def test_the_run_context_inherits_the_request_display_name(self):
        discovery = {
            "schema": 1,
            "profile": "default",
            "harnesses": {"cursor": {"models": {"grok-4.7-xhigh": {"displayName": "Mystery One"}}}},
        }
        request = self._cursor_request(discovery)
        with tempfile.TemporaryDirectory() as directory:
            ctx = run_context.from_request(
                request,
                registry_root=Path(directory),
                run_id="run-1",
                alias="alias-1",
                source_cwd=directory,
                execution_cwd=directory,
                workspace_kind="directory",
                isolated_workspace=False,
                include_dirty=False,
            )
        self.assertEqual(ctx.model_display_name, "Mystery One")

    def test_run_summaries_show_what_answered(self):
        summary = run_status.build_run_summary(
            Path("/nonexistent-registry"),
            "run-1",
            {"alias": "alias-1", "harness": "omp"},
            include_logs=False,
            state={
                "status": "succeeded",
                "servedModel": "glm-5p3",
                "servedProvider": "fireworks",
            },
            manifest={},
        )
        self.assertEqual(summary["servedModel"], "glm-5p3")
        self.assertEqual(summary["servedProvider"], "fireworks")


class ModelDiscoveryHelperTests(unittest.TestCase):
    def test_is_provider_qualified_splits_at_the_first_slash(self):
        for selector, expected in (
            ("opencode-go/glm-5.3", True),
            ("gateway/acme/model-pro", True),
            ("glm-5.3", False),
            ("kimi", False),
            ("/glm-5.3", False),
            ("opencode-go/", False),
            ("", False),
            (None, False),
        ):
            with self.subTest(selector=selector):
                self.assertIs(model_discovery.is_provider_qualified(selector), expected)

    def test_is_explicit_provider_id_is_a_typed_id_that_is_not_an_alias_key(self):
        aliases = {"glm": {"model": "opencode-go/glm-5.3"}, "acme/fast": {"model": "x/y"}}
        explicit = model_discovery.is_explicit_provider_id
        self.assertTrue(explicit("opencode-go/glm-5.3", aliases))
        self.assertTrue(explicit("gateway/acme/model-pro", {}))
        self.assertTrue(explicit("opencode-go/glm-5.3", None))
        # An alias key wins even when it looks like provider/model.
        self.assertFalse(explicit("acme/fast", aliases))
        # Neither an alias name nor a bare token is a typed provider id.
        self.assertFalse(explicit("glm", aliases))
        self.assertFalse(explicit("glm-5.3", aliases))
        self.assertFalse(explicit(None, aliases))
        self.assertFalse(explicit("", aliases))

    def test_catalog_display_name_reads_only_the_named_engine_and_selector(self):
        discovery = {
            "harnesses": {
                "cursor": {
                    "models": {
                        "grok-4.7-xhigh": {"displayName": "Grok 4.7 256K Extra High"},
                        "no-name": {},
                        "empty-name": {"displayName": ""},
                    }
                },
                "omp": {"models": {"grok-4.7-xhigh": {"displayName": "wrong engine"}}},
            }
        }
        name = model_discovery.catalog_display_name
        self.assertEqual(name(discovery, "cursor", "grok-4.7-xhigh"), "Grok 4.7 256K Extra High")
        self.assertIsNone(name(discovery, "cursor", "no-name"))
        self.assertIsNone(name(discovery, "cursor", "empty-name"))
        self.assertIsNone(name(discovery, "cursor", "missing"))
        self.assertIsNone(name(discovery, "codex", "grok-4.7-xhigh"))
        self.assertIsNone(name(None, "cursor", "grok-4.7-xhigh"))
        self.assertIsNone(name(discovery, "cursor", None))

    def test_unknown_alias_error_is_none_for_every_accepted_shape(self):
        catalog = _catalog("opencode-go/glm-5.3")
        error = model_discovery.omp_unknown_alias_error
        self.assertIsNone(error("builder", {"builder": {}}, catalog))
        self.assertIsNone(error("glm-5.3", {}, catalog))
        self.assertIsNone(error("opencode-go/glm-5.3", {}, catalog))
        self.assertIsNone(error("anything/else", {}, catalog))
        self.assertIsNone(error("kimi", {}, None))
        self.assertIsNone(error("kimi", {}, _catalog()))
        refusal = error("kimi", {}, catalog)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.error, "invalid_alias")
        self.assertIn("No omp aliases are configured", refusal.message)

    def test_unknown_alias_error_redacts_a_credential_shaped_name(self):
        secret = "sk-livesecret1234567890"
        refusal = model_discovery.omp_unknown_alias_error(secret, {}, _catalog("a/b"))
        self.assertNotIn(secret, refusal.message)


if __name__ == "__main__":
    unittest.main()
