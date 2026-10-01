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

This is a bounded review, not a discovery task. Read the served skill index once, then read the skill files you judged relevant — nothing else. Do not run discovery CLIs to enumerate skills (`<tool> doctor`, `--help` probing, directory listings, or repeated reads of the skill index), and do not read the index again if you have already read it in this session. If you cannot find a skill index after one attempt, say so and proceed with the task; the review must never delay the work it precedes.

Respect the current Delegate run mode. In safe/read-only mode, skill instructions may guide analysis, review, or recommendations, but must not override the read-only requirement.

"""

# Two sentences, on purpose. A tracked child ends when its model stops talking,
# and a background task or Monitor dies with the session, so a child that says
# "waiting on the gate" has abandoned it. See degraded.py for the detection side.
TURN_END_INSTRUCTION = """## Delegate run rule

Ending your turn ends this Run, and nothing will wake you afterward: any background task or monitor still running is killed and its result is never seen. Run long jobs (tests, builds, gates) in the foreground and finish them before your final message."""

# Trey, 2026-09-30: lanes run targeted tests only. The rule's source of truth
# is the "Lanes run targeted tests only" bullet in
# ~/.claude-shared/rules/agent-operating-contract.md; a promoted release is a
# pinned artifact, so the text is carried here rather than read at run time.
# Change both together.
LANE_TESTS_INSTRUCTION = """## Delegate lane test rule

Lanes run targeted tests only: the test files for what you changed and their immediate neighbours, through `testrun <repo> <kind> -- <cmd>` with explicit worker caps (vitest --maxWorkers=8, cargo -j 8 / --test-threads=8, pytest -n 8). The full suite and the project gate are the coordinator's, run once on the integrated candidate; a lane never runs them, whatever a repository or skill file says. (Trey, 2026-09-30.)"""

# Work mode only: a Run has no human on the other end, so a skill that gates
# implementation behind approval (brainstorming, design review) would park it.
# Safe mode is read-only by construction and never asked to implement.
WORK_NO_APPROVAL_INSTRUCTION = (
    "Nobody can answer a question or approve a step during this Run. Treat the task in this "
    "prompt as the approval to carry it out, unless the prompt itself asks only for a plan, a "
    "review, or a read-only answer, and do not stop to ask for confirmation. If you truly cannot "
    "continue, end with a final report that says exactly what blocks you."
)
WORK_TURN_END_INSTRUCTION = TURN_END_INSTRUCTION + " " + WORK_NO_APPROVAL_INSTRUCTION

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
last after the report, without wrapping it in the report. After the final
payload, stop; do not reopen artifacts or begin another pass.
"""


def prepend_skill_review_instructions(prompt: str) -> str:
    if prompt.startswith(SKILL_REVIEW_PREFIX):
        return prompt
    return SKILL_REVIEW_PREFIX + prompt


def append_completion_report_instructions(prompt: str) -> str:
    if prompt.rstrip().endswith(COMPLETION_REPORT_SUFFIX.strip()):
        return prompt
    return prompt + COMPLETION_REPORT_SUFFIX
