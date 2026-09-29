"""Extract ``delegate ...`` command lines from prose docs and expand usage synopses.

Support code for ``tests/test_docs_parser_conformance.py``. It answers two
questions and nothing else:

* Which ``delegate ...`` lines does a Markdown file present as commands?
  (``markdown_commands``: fenced ``bash``/``sh``/``text``/bare blocks and inline
  code spans, never JSON blocks, headings, or spans that merely mention the
  program name.)
* How does a line that is written as a usage synopsis (``[--flag VALUE]``,
  ``{safe,work}``, ``(A|B)``, ``<handle>``, ``[prompt...]``) turn into concrete
  argv lists a parser can be asked about? (``expand_usage``.)

The expander is deliberately not a full enumeration: a synopsis with nine optional
groups has hundreds of combinations, and a parser that accepts every option alone
and all of them together does not reject any documented pair for a reason the
synopsis could hide. It emits the minimal form, the maximal form, and the minimal
form plus each single alternative of each group, so every documented flag and
every documented alternative is exercised at least once.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

# Fence languages whose lines may carry commands. JSON, Python, and other data or
# code blocks are skipped even when a string inside them starts with "delegate ".
COMMAND_FENCE_LANGUAGES = frozenset({"", "bash", "sh", "shell", "console", "text"})

_FENCE = re.compile(r"^\s*(`{3,})\s*([A-Za-z0-9_+-]*)\s*$")
_CODE_SPAN = re.compile(r"(?<!`)`([^`]+)`(?!`)")
_UPPER_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ASSIGNMENT_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_]*=[A-Z][A-Z0-9_]*$")
_BRACKETS = "[](){}"


@dataclass(frozen=True)
class DocCommand:
    """One ``delegate ...`` line found in a Markdown file."""

    line: int
    text: str
    origin: str  # "fence" or "inline"


def markdown_commands(markdown: str) -> list[DocCommand]:
    """Return every ``delegate ...`` command line or inline span in ``markdown``."""
    found: list[DocCommand] = []
    fence_marker: str | None = None
    fence_is_command_block = False
    paragraph: list[str] = []
    paragraph_start = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        prose = " ".join(paragraph)
        for match in _CODE_SPAN.finditer(prose):
            span = " ".join(match.group(1).split())
            if span.startswith("delegate "):
                found.append(DocCommand(paragraph_start, span, "inline"))
        paragraph.clear()

    for number, raw in enumerate(markdown.splitlines(), start=1):
        fence = _FENCE.match(raw)
        if fence_marker is not None:
            if fence and fence.group(1) == fence_marker and not fence.group(2):
                fence_marker = None
                continue
            stripped = raw.strip()
            if fence_is_command_block and stripped.startswith("delegate "):
                found.append(DocCommand(number, stripped, "fence"))
            continue
        if fence:
            flush_paragraph()
            fence_marker = fence.group(1)
            fence_is_command_block = fence.group(2) in COMMAND_FENCE_LANGUAGES
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            flush_paragraph()
            continue
        if not paragraph:
            paragraph_start = number
        paragraph.append(stripped)
    flush_paragraph()
    return found


# --- usage grammar -----------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Word:
    text: str
    quoted: bool = False


@dataclass(frozen=True, eq=False)
class Opt:
    """``[...]``: absent, or present as one of its ``|`` alternatives."""

    alts: tuple[tuple[Node, ...], ...]


@dataclass(frozen=True, eq=False)
class Req:
    """``(A|B)``, ``{a,b}``, ``a|b``, ``<x|--y Z>``: exactly one alternative."""

    alts: tuple[tuple[Node, ...], ...]


Node = Word | Opt | Req


class UsageSyntaxError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def parse(self) -> tuple[Node, ...]:
        alts = self._alternatives(closer=None)
        if len(alts) == 1:
            return tuple(alts[0])
        return (Req(tuple(tuple(alt) for alt in alts)),)

    def _alternatives(self, closer: str | None) -> list[list[Node]]:
        text = self.text
        alts: list[list[Node]] = [[]]
        while self.pos < len(text):
            ch = text[self.pos]
            if ch.isspace():
                self.pos += 1
            elif closer is not None and ch == closer:
                self.pos += 1
                self._skip_ellipsis()
                return alts
            elif ch in "])}":
                raise UsageSyntaxError(f"unbalanced {ch!r} in {text!r}")
            elif ch == "[":
                self.pos += 1
                alts[-1].append(Opt(_freeze(self._alternatives("]"))))
            elif ch == "(":
                self.pos += 1
                alts[-1].append(Req(_freeze(self._alternatives(")"))))
            elif ch == "{":
                end = self._find("}")
                body = text[self.pos + 1 : end]
                self.pos = end + 1
                alts[-1].append(Req(tuple((Word(choice),) for choice in body.split(","))))
            elif ch == "<":
                self._angle(alts)
            elif ch in "\"'":
                alts[-1].append(self._quoted(ch))
            elif ch == "|":
                self.pos += 1
                alts.append([])
            elif ch == "#" and (self.pos == 0 or text[self.pos - 1].isspace()):
                self.pos = len(text)  # trailing shell comment
            else:
                self._word(alts)
        if closer is not None:
            raise UsageSyntaxError(f"missing {closer!r} in {text!r}")
        return alts

    def _find(self, char: str) -> int:
        end = self.text.find(char, self.pos)
        if end < 0:
            raise UsageSyntaxError(f"missing {char!r} in {self.text!r}")
        return end

    def _skip_ellipsis(self) -> None:
        if self.text.startswith("...", self.pos):
            self.pos += 3

    def _angle(self, alts: list[list[Node]]) -> None:
        text = self.text
        following = text[self.pos + 1 : self.pos + 2]
        if following == "" or following.isspace():
            # A shell input redirect ("< rubric.md"): not part of the argv.
            self.pos += 1
            while self.pos < len(text) and text[self.pos].isspace():
                self.pos += 1
            while self.pos < len(text) and not text[self.pos].isspace():
                self.pos += 1
            return
        end = self._find(">")
        body = text[self.pos + 1 : end]
        self.pos = end + 1
        self._skip_ellipsis()
        if "|" not in body:
            alts[-1].append(Word(f"<{body}>"))
            return
        options: list[tuple[Node, ...]] = []
        for piece in body.split("|"):
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", piece):
                options.append((Word(f"<{piece}>"),))  # `<alias|runId>`: two placeholders
            else:
                options.append(tuple(_Parser(piece).parse()))  # `<handle|--group NAME>`
        alts[-1].append(Req(tuple(options)))

    def _quoted(self, quote: str) -> Word:
        end = self.text.find(quote, self.pos + 1)
        if end < 0:
            raise UsageSyntaxError(f"unterminated {quote} in {self.text!r}")
        word = Word(self.text[self.pos + 1 : end], quoted=True)
        self.pos = end + 1
        return word

    def _word(self, alts: list[list[Node]]) -> None:
        text = self.text
        start = self.pos
        while (
            self.pos < len(text)
            and not text[self.pos].isspace()
            and text[self.pos] not in _BRACKETS
        ):
            self.pos += 1
        raw = text[start : self.pos]
        pieces = raw.split("|")
        if len(pieces) == 1:
            alts[-1].append(Word(raw))
        elif all(piece and not piece.startswith("-") for piece in pieces):
            # A value enumeration after a flag: `--isolation auto|none|worktree`.
            alts[-1].append(Req(tuple((Word(piece),) for piece in pieces)))
        else:
            # Alternatives at the sequence level: `--fast|--no-fast`,
            # `--output-schema PATH|--no-output-schema`, `FILE|-`.
            if pieces[0]:
                alts[-1].append(Word(pieces[0]))
            for piece in pieces[1:]:
                alts.append([Word(piece)] if piece else [])


def _freeze(alts: list[list[Node]]) -> tuple[tuple[Node, ...], ...]:
    return tuple(tuple(alt) for alt in alts)


def parse_usage(text: str) -> tuple[Node, ...]:
    return _Parser(text).parse()


# --- expansion ---------------------------------------------------------------

Choose = Callable[[Opt | Req], int | None]


def _render(nodes: tuple[Node, ...], choose: Choose) -> Iterator[Word]:
    for node in nodes:
        if isinstance(node, Word):
            yield node
            continue
        index = choose(node)
        if index is not None:
            yield from _render(node.alts[index], choose)


def _walk(
    nodes: tuple[Node, ...], ancestors: tuple[tuple[Opt | Req, int], ...] = ()
) -> Iterator[tuple[Opt | Req, tuple[tuple[Opt | Req, int], ...]]]:
    for node in nodes:
        if isinstance(node, Word):
            continue
        yield node, ancestors
        for index, alt in enumerate(node.alts):
            yield from _walk(alt, (*ancestors, (node, index)))


def usage_variants(nodes: tuple[Node, ...]) -> list[list[Word]]:
    """Minimal, maximal, and one-alternative-at-a-time renderings of ``nodes``."""

    def build(forced: dict[Opt | Req, int], *, everything_on: bool) -> list[Word]:
        def choose(node: Opt | Req) -> int | None:
            if node in forced:
                return forced[node]
            if isinstance(node, Opt):
                return 0 if everything_on else None
            return 0

        return list(_render(nodes, choose))

    variants = [build({}, everything_on=False), build({}, everything_on=True)]
    for node, ancestors in _walk(nodes):
        for index in range(len(node.alts)):
            forced = {node: index}
            for ancestor, ancestor_index in ancestors:
                forced.setdefault(ancestor, ancestor_index)
            variants.append(build(forced, everything_on=False))
    unique: list[list[Word]] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for variant in variants:
        key = tuple((word.text, word.quoted) for word in variant)
        if key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique


# --- placeholder substitution --------------------------------------------------


class UnmappedPlaceholder(KeyError):
    pass


@dataclass(frozen=True)
class Placeholders:
    """Plausible values for the placeholders docs and help text use.

    ``by_flag`` maps ``(flag, placeholder)`` to a value and wins over
    ``by_name``, so ``--timeout SEC`` can be an integer while ``--reason TEXT``
    is prose. A placeholder with no entry raises rather than passing through, so a
    newly documented placeholder is a visible decision.
    """

    by_name: dict[str, str]
    by_flag: dict[tuple[str, str], str] = field(default_factory=dict)
    prompt: str = "Review the change."
    # Descriptive words that stand for "some options", such as `[resume-options]`.
    ignored: frozenset[str] = frozenset()

    def resolve(self, word: str, previous: str | None) -> str:
        if word.startswith("<") and word.endswith(">"):
            key = word[1:-1]
        elif _UPPER_PLACEHOLDER.match(word) or _ASSIGNMENT_PLACEHOLDER.match(word):
            key = word
        else:
            return word
        if previous is not None and (previous, key) in self.by_flag:
            return self.by_flag[(previous, key)]
        if key in self.by_name:
            return self.by_name[key]
        raise UnmappedPlaceholder(f"{word!r} after {previous!r}")


def concretize(words: list[Word], placeholders: Placeholders) -> list[str]:
    """Turn rendered words into argv tokens (without the leading ``delegate``)."""
    argv: list[str] = []
    for word in words:
        text = word.text
        if word.quoted:
            argv.append(text)
            continue
        if text == "..." or text in placeholders.ignored:
            continue
        if text.endswith("..."):
            text = text[:-3]
            if text == "prompt":
                argv.append(placeholders.prompt)
                continue
        argv.append(placeholders.resolve(text, argv[-1] if argv else None))
    return argv


@dataclass(frozen=True)
class Expansion:
    argvs: list[list[str]]  # concrete argv lists, without the leading `delegate`
    elided: bool  # the line ended in a bare `...`: arguments are intentionally not shown


def expand_usage(line: str, placeholders: Placeholders) -> Expansion:
    """Concrete argv lists for one command or synopsis line."""
    words = list(parse_usage(_strip_env_prefix(line)))
    first = words[0] if words else None
    if not isinstance(first, Word) or first.text != "delegate":
        raise UsageSyntaxError(f"not a delegate command: {line!r}")
    last = words[-1]
    elided = isinstance(last, Word) and not last.quoted and last.text == "..."
    argvs: list[list[str]] = []
    for variant in usage_variants(tuple(words[1:])):
        argv = concretize(variant, placeholders)
        if argv not in argvs:
            argvs.append(argv)
    return Expansion(argvs, elided)


def _strip_env_prefix(line: str) -> str:
    """Drop a leading `env -u NAME` / `env NAME=value` wrapper: the wrapped command is the claim."""
    tokens = line.split()
    if not tokens or tokens[0] != "env":
        return line
    index = 1
    while index < len(tokens) and tokens[index] != "delegate":
        index += 2 if tokens[index] == "-u" else 1
    return " ".join(tokens[index:])
