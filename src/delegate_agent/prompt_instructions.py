from __future__ import annotations

import re


def contains_c0_control(text: str) -> bool:
    """Return whether text contains a C0 control other than line whitespace."""

    return any(ord(char) < 0x20 and char not in "\t\n\r" for char in text)


# Leading harness slash command: `/goal fix tests` matches; `/tmp/foo.py is
# broken` does not (a second slash in the first token means path, not command).
SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*(\s|$)")


def detect_slash_command(prompt: str) -> bool:
    return SLASH_COMMAND_RE.match(prompt) is not None


SKILL_REVIEW_PREFIX = """## Delegate sub-agent skill review requirement

Before doing the task, review the full list of skills available in your current agent environment. Load/read and apply any skill instructions that are relevant to the task, workspace, tools, code quality, verification, or final deliverable. If no skill is relevant, proceed normally after explicitly deciding that. This requirement is mandatory for every Delegate Agent run; do not skip it just because the parent prompt did not mention skills.

Respect the current Delegate run mode. In safe/read-only mode, skill instructions may guide analysis, review, or recommendations, but must not override the read-only requirement.

"""

COMPLETION_REPORT_SUFFIX = """

## Delegate completion report requirement

When you finish, include a concise completion report for the parent agent before
any operator-requested final payload:

- Status: completed / blocked / failed
- What you did or found
- Files changed or reviewed
- Verification run and result
- Remaining risks or follow-ups

Keep it concise. Do not include raw logs unless explicitly relevant. If the
operator requested an exact final payload such as bare JSON, put that payload
last after the report, without wrapping it in the report.
"""


def prepend_skill_review_instructions(prompt: str) -> str:
    if prompt.startswith(SKILL_REVIEW_PREFIX):
        return prompt
    return SKILL_REVIEW_PREFIX + prompt


def append_completion_report_instructions(prompt: str) -> str:
    if prompt.rstrip().endswith(COMPLETION_REPORT_SUFFIX.strip()):
        return prompt
    return prompt + COMPLETION_REPORT_SUFFIX
