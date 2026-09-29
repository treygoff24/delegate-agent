"""Documented ``delegate ...`` command lines must parse.

The CLI's syntax is written down three times: the parser, ``command_help.py``'s
``COMMAND_SPECS``, and ``docs/cli-reference.md``. Nothing tied the copies together,
so every drift surfaced as one agent typing a documented command and getting a
refusal (``mail send -`` was documented and refused; ``models --live`` was written
in a skill without the engine it needs).

This test asks the parser about every ``delegate ...`` line those two documents
present as a command:

* ``docs/cli-reference.md``: fenced ``bash``/``text``/bare blocks and inline code
  spans that begin ``delegate ``. JSON blocks, headings, and mentions such as
  ``delegate.doctor.v1`` are not commands.
* ``COMMAND_SPECS``: every ``usage`` and ``examples`` line.

A line written as a synopsis (``[--flag VALUE]``, ``{safe,work}``, ``(A|B)``,
``<handle>``, ``[prompt...]``) is expanded into concrete argv lists by
``tests/doc_commands.py``: the minimal form, the maximal form, the minimal form
plus each single alternative of each group, and the minimal form plus every pair of
groups (a synopsis of independent brackets claims any two flags may appear
together, and the pairs are where the parser says otherwise). Three-way conflicts
are not searched for. Every one must be accepted by
``parse_cli`` (parse only; nothing runs) unless its concrete text is on
``REJECTED_BY_DESIGN`` with the reason.

Two kinds of line make a weaker claim and are checked accordingly. A line that ends
in a bare ``...`` says its arguments are not shown, and an inline span that is only
a command path (``delegate wait``) names the command in prose. Both pass when the
parser accepts them, or when the parser refuses them and the command path is a
known ``COMMAND_SPECS`` entry (the refusal is then the missing arguments the line
never claimed to show).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from delegate_agent import command_help, mail, run_registry
from delegate_agent.cli_parser import parse_cli
from delegate_agent.errors import DelegateError
from tests import doc_commands as dc
from tests.mail_test_helpers import mail_temporary_directory

ROOT = Path(__file__).resolve().parents[1]
CLI_REFERENCE = ROOT / "docs" / "cli-reference.md"

# Concrete commands the parser rejects on purpose although a line expands to them.
# Keyed by the exact text tested ("delegate " + the argv), with the reason. Every
# entry must still be rejected (an accepted entry is stale) and must still be
# produced by some line (a vanished entry is stale).
REJECTED_BY_DESIGN: dict[str, str] = {
    "delegate followup x fix it --dry-run": (
        "The cli-reference shows it as the example of a followup option inside the prompt "
        "text, which is refused (option_after_handle) so a stray --dry-run cannot start a "
        "real run."
    ),
}


def _placeholders(directory: str, file: str) -> dc.Placeholders:
    return dc.Placeholders(
        by_name={
            # Values.
            "PATH": directory,
            "FILE": file,
            "NAME": "example",
            "LEVEL": "high",
            "SECONDS": "30",
            "SEC": "30",
            "N": "5",
            "DAYS": "30",
            "SEQ": "1",
            "KEY": "key",
            "TEXT": "text",
            "JSON": "{}",
            "S": "status",
            "BODY": "The review is ready.",
            "WHO": "trey",
            "HEX": "a" * 64,
            "STATUS": "present",
            "NAME=VALUE": "FOO=bar",
            "REF": "main",
            "CMD": "true",
            "TARGET": "room:ops",
            "MODEL": "gpt-5.5",
            # Names of things that exist in a registry.
            "ENGINE": "codex",
            "HARNESS": "codex",
            "ALIAS": "codex-1",
            "HANDLE": "codex-1",
            "ID": "abc123",
            "ID_PREFIX": "abc123",
            "MESSAGE_ID": "abc123",
            "SENDER": "codex-1",
            # Angle-bracket placeholders (written without the brackets).
            "alias-or-model": "gpt-5.5",
            "text": "continue with the fixes",
            "engine": "codex",
            "command": "codex",
            "subcommand": "safe",
            "action": "init",
            "handle": "codex-1",
            "alias": "codex-1",
            "runId": "del_20260520T100000Z_abcdef",
            "wfId": "wf_0123456789ab",
            "script.py": "flow.py",
            "key-or-label": "key",
            "saved-name": "review",
            "name": "review",
            "you": "trey",
            "commit": "abc123",
        },
        by_flag={("--interval-ms", "N"): "1000"},
        ignored=frozenset({"options", "resume-options"}),
    )


_VALUE_GLOBALS = frozenset(o.flag for o in command_help.GLOBAL_OPTIONS if o.arg is not None)


def _leading_path(argv: list[str]) -> str:
    """The command path of ``argv``: what is left after global options and their values."""
    path: list[str] = []
    for index, token in enumerate(argv):
        if token.startswith("-") or (index and argv[index - 1] in _VALUE_GLOBALS):
            continue
        path.append(token)
    return " ".join(path)


def _resolves(argv: list[str]) -> bool:
    """True when the parser recognises the command path (``--help`` needs no arguments)."""
    try:
        parse_cli([*argv, "--help"])
    except DelegateError:
        return False
    return True


def _rejections(
    command: dc.DocCommand | str, placeholders: dc.Placeholders
) -> list[tuple[str, str]]:
    """``(concrete command, "code: message")`` for each expansion the parser refuses."""
    line = command.text if isinstance(command, dc.DocCommand) else command
    inline = isinstance(command, dc.DocCommand) and command.origin == "inline"
    expansion = dc.expand_usage(line, placeholders)
    refused: list[tuple[str, str]] = []
    for argv in expansion.argvs:
        text = "delegate " + shlex.join(argv)
        try:
            parse_cli(list(argv))
        except DelegateError as error:
            mention = inline and " ".join(argv) in command_help.COMMAND_SPECS
            partial = expansion.elided or mention
            if partial and _leading_path(argv) in command_help.COMMAND_SPECS and _resolves(argv):
                continue
            refused.append((text, f"{error.error}: {error.message}"))
    return refused


class DocsParserConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="delegate-doc-conformance-")
        )
        file = os.path.join(directory, "file.json")
        Path(file).write_text("{}", encoding="utf-8")
        self.placeholders = _placeholders(directory, file)

    def _spec_lines(self) -> list[tuple[str, str]]:
        return [
            (f"COMMAND_SPECS[{name!r}].{kind}", line)
            for name, spec in command_help.COMMAND_SPECS.items()
            for kind, lines in (("usage", spec.usage), ("examples", spec.examples))
            for line in lines
        ]

    def _doc_lines(self) -> list[tuple[str, dc.DocCommand]]:
        commands = dc.markdown_commands(CLI_REFERENCE.read_text(encoding="utf-8"))
        return [(f"docs/cli-reference.md:{command.line}", command) for command in commands]

    def _report(self, sources: list[tuple[str, dc.DocCommand | str]]) -> tuple[list[str], set[str]]:
        problems: list[str] = []
        allowed_seen: set[str] = set()
        for label, command in sources:
            for text, message in _rejections(command, self.placeholders):
                if text in REJECTED_BY_DESIGN:
                    allowed_seen.add(text)
                    continue
                problems.append(f"{label}\n    {text}\n    -> {message}")
        return problems, allowed_seen

    def test_cli_reference_commands_parse(self) -> None:
        sources = self._doc_lines()
        self.assertGreater(len(sources), 150, "extraction found suspiciously few commands")
        problems, _ = self._report(list(sources))
        self.assertEqual(
            problems,
            [],
            "\ndocs/cli-reference.md shows commands the parser refuses. Fix the doc when "
            "it is wrong, fix the parser when the doc states the intended behaviour, or "
            "add the concrete command to REJECTED_BY_DESIGN with the reason:\n\n"
            + "\n".join(problems),
        )

    def test_command_help_usage_and_examples_parse(self) -> None:
        sources = self._spec_lines()
        self.assertGreater(len(sources), 250, "extraction found suspiciously few spec lines")
        problems, _ = self._report(list(sources))
        self.assertEqual(
            problems,
            [],
            "\ncommand_help.py shows commands the parser refuses. Fix the CommandSpec "
            "when it is wrong, fix the parser when the spec states the intended "
            "behaviour, or add the concrete command to REJECTED_BY_DESIGN with the "
            "reason:\n\n" + "\n".join(problems),
        )

    def test_rejected_by_design_entries_are_still_needed(self) -> None:
        sources = [*self._doc_lines(), *self._spec_lines()]
        produced: set[str] = set()
        for _label, command in sources:
            line = command.text if isinstance(command, dc.DocCommand) else command
            for argv in dc.expand_usage(line, self.placeholders).argvs:
                produced.add("delegate " + shlex.join(argv))
        for text, reason in REJECTED_BY_DESIGN.items():
            with self.subTest(command=text):
                self.assertTrue(reason.strip(), "an allowlist entry needs its reason")
                self.assertIn(text, produced, "no documented line expands to this entry")
                with self.assertRaises(
                    DelegateError, msg="the parser now accepts this; drop the entry"
                ):
                    parse_cli(shlex.split(text)[1:])


class MarkdownExtractionTests(unittest.TestCase):
    def test_takes_command_blocks_and_inline_spans_only(self) -> None:
        markdown = textwrap.dedent(
            """\
            # A heading that names `delegate heading`

            Run `delegate wait --group NAME` next. The `delegate.doctor.v1` schema,
            the `delegate/*` branch, and `env -u X delegate promote` are not command
            lines, but a span that wraps `delegate runs
            --active` is one.

            ```bash
            delegate codex safe "x"
            echo not-a-delegate-line
            ```

            ```json
            delegate inside json
            ```

            - A list item with an indented fence:

              ```bash
              delegate opencode call --read-only
              ```

            ```
            delegate mail inbox
            ```
            """
        )
        found = [(c.text, c.origin) for c in dc.markdown_commands(markdown)]
        self.assertEqual(
            found,
            [
                ("delegate wait --group NAME", "inline"),
                ("delegate runs --active", "inline"),
                ('delegate codex safe "x"', "fence"),
                ("delegate opencode call --read-only", "fence"),
                ("delegate mail inbox", "fence"),
            ],
        )

    def test_real_reference_yields_both_kinds(self) -> None:
        origins = {
            c.origin for c in dc.markdown_commands(CLI_REFERENCE.read_text(encoding="utf-8"))
        }
        self.assertEqual(origins, {"fence", "inline"})


class UsageExpansionTests(unittest.TestCase):
    """The expander is the instrument; a wrong expansion would hide a wrong doc."""

    placeholders = dc.Placeholders(
        by_name={"NAME": "n", "N": "5", "ALIAS": "a", "FILE": "f", "BODY": "b", "handle": "h"},
        ignored=frozenset({"options"}),
    )

    def expand(self, line: str) -> list[str]:
        return [" ".join(argv) for argv in dc.expand_usage(line, self.placeholders).argvs]

    def test_optionals_appear_minimal_maximal_and_one_at_a_time(self) -> None:
        self.assertEqual(
            self.expand("delegate run [--a] [--b NAME] <handle>"),
            ["run h", "run --a --b n h", "run --a h", "run --b n h"],
        )

    def test_every_pair_of_optional_groups_is_combined(self) -> None:
        # Independent brackets claim any two flags may appear together. The old
        # runs synopsis said so about --limit and --summary; the parser disagrees, and
        # only a variant carrying both can show it.
        variants = self.expand("delegate x [--a] [--b] [--c NAME] [--d]")
        for pair in ("--a --b", "--a --c n", "--b --d", "--c n --d"):
            self.assertIn(f"x {pair}", variants)
        self.assertNotIn("x --a --b --c n", variants, "three at once is not searched")

    def test_pairs_span_every_alternative_of_each_group(self) -> None:
        variants = self.expand("delegate x [--mode auto|none] [--a]")
        self.assertIn("x --mode auto --a", variants)
        self.assertIn("x --mode none --a", variants)

    def test_pairs_never_combine_exclusive_alternatives(self) -> None:
        # The corrected shape: --summary excludes both flags of its sibling group.
        variants = self.expand("delegate x [--summary | [--limit N] [--structural]]")
        self.assertIn("x --limit 5 --structural", variants)
        self.assertIn("x --summary", variants)
        self.assertNotIn("x --summary --limit 5", variants)
        self.assertNotIn("x --summary --structural", variants)
        # The old shape, where the independent [--limit N] beside [--structural|--summary]
        # claimed the pair the parser refuses.
        old = self.expand("delegate x [--limit N] [--structural|--summary]")
        self.assertIn("x --limit 5 --summary", old)
        # The other nested branch: --tail and --max-chars are each reachable inside
        # the group, and --raw excludes both.
        raw = self.expand("delegate x [--raw | [--tail N] [--max-chars N]]")
        self.assertIn("x --raw", raw)
        self.assertIn("x --tail 5", raw)
        self.assertIn("x --max-chars 5", raw)
        self.assertNotIn("x --raw --tail 5", raw)
        self.assertNotIn("x --raw --max-chars 5", raw)

    def test_brace_choice_and_value_enumeration(self) -> None:
        self.assertEqual(self.expand("delegate x {safe,work} y"), ["x safe y", "x work y"])
        self.assertEqual(
            self.expand("delegate x [--isolation auto|none|worktree]"),
            [
                "x",
                "x --isolation auto",
                "x --isolation none",
                "x --isolation worktree",
            ],
        )

    def test_sequence_level_alternatives_keep_their_own_arguments(self) -> None:
        self.assertEqual(
            self.expand("delegate x [--persona NAME|--no-persona]"),
            ["x", "x --persona n", "x --no-persona"],
        )
        self.assertCountEqual(
            self.expand("delegate x (--to ALIAS|coordinator | --group NAME)"),
            ["x --to a", "x --to coordinator", "x --group n"],
        )
        self.assertEqual(
            self.expand("delegate x <handle|--group NAME>"),
            ["x h", "x --group n"],
        )

    def test_stdin_dash_is_an_alternative_like_any_other(self) -> None:
        self.assertEqual(
            self.expand("delegate mail send (BODY|--file FILE|-)"),
            ["mail send b", "mail send --file f", "mail send -"],
        )

    def test_ellipsis_prompt_and_elision_marker(self) -> None:
        self.assertEqual(
            self.expand("delegate x [prompt...]"),
            ["x", "x Review the change."],
        )
        expansion = dc.expand_usage("delegate mail {send,inbox} ...", self.placeholders)
        self.assertTrue(expansion.elided)
        self.assertEqual([" ".join(a) for a in expansion.argvs], ["mail send", "mail inbox"])
        self.assertFalse(dc.expand_usage("delegate x [prompt...]", self.placeholders).elided)

    def test_shell_spellings_quotes_comments_redirects_and_env_prefix(self) -> None:
        self.assertEqual(self.expand('delegate x "NAME here" # why'), ["x NAME here"])
        self.assertEqual(
            self.expand("delegate claude call --pure < rubric.md"), ["claude call --pure"]
        )
        self.assertEqual(
            self.expand("env -u AI_PROFILE delegate config sync-profiles"), ["config sync-profiles"]
        )
        self.assertEqual(self.expand("delegate resume [options] <handle>"), ["resume h"])

    def test_literal_words_are_never_rewritten(self) -> None:
        self.assertEqual(self.expand("delegate runs actve"), ["runs actve"])

    def test_unknown_placeholder_and_bad_syntax_fail_loudly(self) -> None:
        with self.assertRaises(dc.UnmappedPlaceholder):
            dc.expand_usage("delegate x FROBNICATE", self.placeholders)
        with self.assertRaises(dc.UsageSyntaxError):
            dc.expand_usage("delegate x [--a", self.placeholders)
        with self.assertRaises(dc.UsageSyntaxError):
            dc.expand_usage("codex safe", self.placeholders)


class PartialLineTests(unittest.TestCase):
    """Lines that show only a command path are held to the path, not to full arguments."""

    placeholders = _placeholders("dir", "file")

    def refused(self, line: str, origin: str = "fence") -> list[str]:
        command = dc.DocCommand(1, line, origin)
        return [text for text, _message in _rejections(command, self.placeholders)]

    def test_elided_line_needs_a_real_command_path(self) -> None:
        self.assertEqual(self.refused("delegate mail send ..."), [])
        self.assertEqual(self.refused("delegate mail sendd ..."), ["delegate mail sendd"])
        self.assertEqual(self.refused("delegate worktree zzz ..."), ["delegate worktree zzz"])

    def test_bare_command_name_is_a_mention_only_inline(self) -> None:
        self.assertEqual(self.refused("delegate wait", "inline"), [])
        self.assertEqual(self.refused("delegate waitt", "inline"), ["delegate waitt"])
        self.assertEqual(self.refused("delegate wait", "fence"), ["delegate wait"])

    def test_a_full_line_is_not_excused_by_its_path(self) -> None:
        self.assertEqual(
            self.refused("delegate mail send --bogus x"), ["delegate mail send --bogus x"]
        )


class MailSendStdinTests(unittest.TestCase):
    """The mail send help promises three body sources; the parser must honour each."""

    def test_bare_dash_delivers_the_bytes_on_stdin(self) -> None:
        """parse_cli accepting ``-`` says nothing about the runtime reading stdin.

        Runs the real launcher with a redirected HOME and a real pipe on stdin, then
        reads the delivered message back off disk.
        """
        body = (
            "First line.\ncaf\u00e9 \u2014 second line.\n\nlast line, trailing newline.\n".encode()
        )
        with (
            mail_temporary_directory("delegate-mail-stdin-workspace-") as workspace_dir,
            mail_temporary_directory("delegate-mail-stdin-home-") as home_dir,
        ):
            workspace = Path(workspace_dir).resolve()
            home = Path(home_dir).resolve()
            registry_root = run_registry.ensure_registry(workspace, workspace_kind="directory")
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"AI_PROFILE", "DELEGATE_PROFILE", "DELEGATE_CONFIG"}
            }
            env["HOME"] = str(home)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin" / "delegate.py"),
                    "--json",
                    "--cwd",
                    str(workspace),
                    "mail",
                    "send",
                    "--to",
                    "coordinator",
                    "-",
                ],
                cwd=ROOT,
                env=env,
                input=body,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            message_id = json.loads(completed.stdout)["message"]["msgId"]
            delivered = (
                mail.boxes_root(registry_root)
                / mail.COORDINATOR_BOX
                / "inbox"
                / f"{message_id}.mail"
            ).read_bytes()
        _envelope, delivered_body = delivered.split(mail.MESSAGE_SEPARATOR, 1)
        self.assertEqual(delivered_body, body)

    def test_synopsis_expansion_exercises_the_stdin_dash(self) -> None:
        spec = command_help.COMMAND_SPECS["mail send"]
        expansion = dc.expand_usage(spec.usage[0], _placeholders("dir", "file"))
        self.assertIn(["mail", "send", "--to", "codex-1", "-"], expansion.argvs)


if __name__ == "__main__":
    unittest.main()
