import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
COMMAND_HELP_PATH = ROOT / "src" / "delegate_agent" / "command_help.py"

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


command_help = load_module(COMMAND_HELP_PATH, "delegate_command_help_test")

# Top-level command names = registry keys with no space.
TOP_LEVEL_COMMANDS = (
    "cursor",
    "claude",
    "codex",
    "droid",
    "kimi",
    "dry-run",
    "profiles",
    "run",
    "snapshot",
    "runs",
    "run-output",
    "worktree",
    "models",
    "describe",
    "agent-help",
    "capabilities",
)

# Payload key set the agent-facing JSON contract must expose (D4).
PAYLOAD_KEYS = {
    "ok",
    "command",
    "summary",
    "usage",
    "arguments",
    "options",
    "examples",
    "notes",
    "seeAlso",
}

# Matches a long-option token: -- followed by a lowercase letter then >=1 of
# [a-z-]. Mirrors the lint regex pinned in the plan (D1).
FLAG_RE = re.compile(r"(?<!\w)--[a-z][a-z-]+")
BRACE_RE = re.compile(r"\{[^}]*\}")


class RenderEverySpecTests(unittest.TestCase):
    """Every spec renders non-empty text containing its command name."""

    def test_every_spec_renders_text(self):
        self.assertTrue(command_help.COMMAND_SPECS, "registry must not be empty")
        for key, spec in command_help.COMMAND_SPECS.items():
            with self.subTest(command=key):
                text = command_help.render_command_help_text(spec)
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip(), f"{key} rendered empty text")
                self.assertIn(spec.name, text, f"{key} text missing command name")

    def test_registry_key_matches_spec_name(self):
        for key, spec in command_help.COMMAND_SPECS.items():
            with self.subTest(command=key):
                self.assertEqual(key, spec.name)


class CommandPayloadShapeTests(unittest.TestCase):
    """JSON help contract shape (D4)."""

    def test_representative_payload_shape(self):
        spec = command_help.COMMAND_SPECS["worktree remove"]
        payload = command_help.command_help_payload(spec)

        self.assertEqual(set(payload.keys()), PAYLOAD_KEYS)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["command"], spec.name)
        self.assertEqual(payload["command"], "worktree remove")
        self.assertEqual(payload["summary"], spec.summary)

        for list_key in ("usage", "arguments", "options", "examples", "notes", "seeAlso"):
            self.assertIsInstance(payload[list_key], list, f"{list_key} must be a list")

        for arg in payload["arguments"]:
            self.assertEqual(set(arg.keys()), {"name", "required", "description"})
            self.assertIsInstance(arg["name"], str)
            self.assertIsInstance(arg["required"], bool)
            self.assertIsInstance(arg["description"], str)

        for opt in payload["options"]:
            self.assertEqual(set(opt.keys()), {"flag", "argument", "description"})
            self.assertIsInstance(opt["flag"], str)
            self.assertTrue(opt["argument"] is None or isinstance(opt["argument"], str))
            self.assertIsInstance(opt["description"], str)

    def test_all_payloads_shape_and_serializable(self):
        for key, spec in command_help.COMMAND_SPECS.items():
            with self.subTest(command=key):
                payload = command_help.command_help_payload(spec)
                self.assertEqual(set(payload.keys()), PAYLOAD_KEYS)
                self.assertIs(payload["ok"], True)
                self.assertEqual(payload["command"], spec.name)
                for list_key in (
                    "usage",
                    "arguments",
                    "options",
                    "examples",
                    "notes",
                    "seeAlso",
                ):
                    self.assertIsInstance(payload[list_key], list)
                for arg in payload["arguments"]:
                    self.assertEqual(set(arg.keys()), {"name", "required", "description"})
                for opt in payload["options"]:
                    self.assertEqual(set(opt.keys()), {"flag", "argument", "description"})
                # Must survive a JSON round-trip without raising.
                json.dumps(payload)


class UsageLintTests(unittest.TestCase):
    """Every --flag in a usage string (outside {...}) resolves to an option (D1)."""

    def test_usage_flags_resolve(self):
        global_flags = {opt.flag for opt in command_help.GLOBAL_OPTIONS}
        for key, spec in command_help.COMMAND_SPECS.items():
            spec_flags = {opt.flag for opt in spec.options}
            known = spec_flags | global_flags
            for usage in spec.usage:
                without_braces = BRACE_RE.sub("", usage)
                for flag in FLAG_RE.findall(without_braces):
                    with self.subTest(command=key, flag=flag, usage=usage):
                        self.assertIn(
                            flag,
                            known,
                            f"{key}: usage flag {flag!r} not in options or "
                            f"GLOBAL_OPTIONS\n  usage: {usage}",
                        )


class NoDeleteTests(unittest.TestCase):
    """The word 'delete' must not appear anywhere in any spec field (I1)."""

    def _assert_no_delete(self, text: str, where: str):
        self.assertNotIn("delete", text.lower(), f"'delete' found in {where}: {text!r}")

    def test_no_delete_in_any_spec_field(self):
        for key, spec in command_help.COMMAND_SPECS.items():
            self._assert_no_delete(spec.summary, f"{key} summary")
            for usage in spec.usage:
                self._assert_no_delete(usage, f"{key} usage")
            for arg in spec.arguments:
                self._assert_no_delete(arg.name, f"{key} arg name")
                self._assert_no_delete(arg.description, f"{key} arg description")
            for opt in spec.options:
                self._assert_no_delete(opt.flag, f"{key} option flag")
                self._assert_no_delete(opt.description, f"{key} option description")
            for note in spec.notes:
                self._assert_no_delete(note, f"{key} note")
            for example in spec.examples:
                self._assert_no_delete(example, f"{key} example")

    def test_no_delete_in_global_options(self):
        for opt in command_help.GLOBAL_OPTIONS:
            self._assert_no_delete(opt.flag, "global option flag")
            self._assert_no_delete(opt.description, "global option description")


class OverviewTests(unittest.TestCase):
    """render_overview_text invariants (I1 + completeness)."""

    def setUp(self):
        self.overview = command_help.render_overview_text()

    def test_overview_contains_worktree_prune_literal(self):
        self.assertIn("worktree prune", self.overview)

    def test_overview_has_no_delete(self):
        self.assertNotIn("delete", self.overview.lower())

    def test_overview_lists_every_top_level_command(self):
        for command in TOP_LEVEL_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, self.overview)

    def test_top_level_commands_match_registry(self):
        registry_top_level = {name for name in command_help.COMMAND_SPECS if " " not in name}
        # 'help' is a registry top-level key too; the overview-completeness set
        # in the plan excludes it from the literal-presence check, but it must
        # still be a registry top-level command.
        self.assertEqual(registry_top_level, set(TOP_LEVEL_COMMANDS) | {"help"})

    def test_overview_advertises_codex_output_schema(self):
        self.assertIn("codex {safe,work}", self.overview)
        self.assertIn("--output-schema FILE", self.overview)


class FocusedGlobalOptionsTests(unittest.TestCase):
    """Focused help should not advertise globals rejected by the parser."""

    def _global_option_lines(self, text: str):
        in_globals = False
        for line in text.splitlines():
            if line == "Global options (before the subcommand):":
                in_globals = True
                continue
            if in_globals and not line:
                break
            if in_globals:
                yield line

    def test_worktree_help_hides_unsupported_isolation_global(self):
        for key, spec in command_help.COMMAND_SPECS.items():
            if key != "worktree" and not key.startswith("worktree "):
                continue
            with self.subTest(command=key):
                text = command_help.render_command_help_text(spec)
                self.assertFalse(
                    any("--isolation" in line for line in self._global_option_lines(text))
                )

    def test_non_worktree_help_keeps_isolation_global(self):
        text = command_help.render_command_help_text(command_help.COMMAND_SPECS["cursor"])
        self.assertTrue(any("--isolation" in line for line in self._global_option_lines(text)))

    def test_codex_help_documents_output_schema(self):
        text = command_help.render_command_help_text(command_help.COMMAND_SPECS["codex"])
        self.assertIn("--output-schema", text)
        self.assertIn("JSON Schema", text)


class HelpIndexPayloadTests(unittest.TestCase):
    """help_index_payload shape and coverage."""

    def setUp(self):
        self.payload = command_help.help_index_payload()

    def test_ok_true(self):
        self.assertIs(self.payload["ok"], True)

    def test_commands_list_shape(self):
        commands = self.payload["commands"]
        self.assertIsInstance(commands, list)
        for entry in commands:
            self.assertEqual(set(entry.keys()), {"command", "summary"})
            self.assertIsInstance(entry["command"], str)
            self.assertIsInstance(entry["summary"], str)

    def test_commands_cover_all_top_level(self):
        listed = {entry["command"] for entry in self.payload["commands"]}
        for command in TOP_LEVEL_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, listed)

    def test_global_options_shape(self):
        global_options = self.payload["globalOptions"]
        self.assertIsInstance(global_options, list)
        self.assertTrue(global_options)
        for opt in global_options:
            self.assertEqual(set(opt.keys()), {"flag", "argument", "description"})
            self.assertIsInstance(opt["flag"], str)
            self.assertTrue(opt["argument"] is None or isinstance(opt["argument"], str))
            self.assertIsInstance(opt["description"], str)

    def test_serializable(self):
        json.dumps(self.payload)


class IsHelpTokenTests(unittest.TestCase):
    """is_help_token recognizes only --help and -h."""

    def test_true_tokens(self):
        self.assertTrue(command_help.is_help_token("--help"))
        self.assertTrue(command_help.is_help_token("-h"))

    def test_false_tokens(self):
        for tok in ("help", "-help", "--h", "cursor", ""):
            with self.subTest(token=tok):
                self.assertFalse(command_help.is_help_token(tok))


if __name__ == "__main__":
    unittest.main()
