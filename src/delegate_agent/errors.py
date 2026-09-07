"""Process exit codes and the shared CLI error type.

This is a dependency leaf: it imports nothing from the rest of the package
(beyond JSON typing), so every other module can raise ``DelegateError``
without creating an import cycle through ``cli``.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from delegate_agent.json_types import JsonObject

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_BINARY = 3


def command_suggestions(value: str, candidates: Iterable[str]) -> list[str]:
    """Return advisory matches from a public vocabulary; never execute them."""
    return difflib.get_close_matches(value, sorted(set(candidates)), n=3)


class DelegateError(Exception):
    def __init__(
        self,
        error: str,
        message: str,
        exit_code: int = EXIT_USAGE,
        *,
        diagnostics: JsonObject | None = None,
        next_actions: list[str] | None = None,
        command: str | None = None,
        help_topic: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code
        self.diagnostics = diagnostics
        self.next_actions = next_actions
        self.command = command
        self.help_topic = help_topic
