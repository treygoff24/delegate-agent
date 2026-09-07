from __future__ import annotations

KIMI_PROMPT_REDACTION = "<prompt redacted: kimi argv transport>"
PROMPT_FILE_ARG_PLACEHOLDER = "<delegate-prompt-file>"
PROMPT_FILE_DISPLAY = "<prompt file>"
DROID_PROMPT_FILE_ARG_PLACEHOLDER = PROMPT_FILE_ARG_PLACEHOLDER
DROID_PROMPT_FILE_DISPLAY = PROMPT_FILE_DISPLAY
DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER = "<delegate-devin-agent-config>"
DEVIN_AGENT_CONFIG_DISPLAY = "<devin agent config>"
PERSONA_FILE_ARG_PLACEHOLDER = "<delegate-persona-file>"
PERSONA_FILE_DISPLAY = "<persona file>"


def prompt_file_display_argv(argv: list[str]) -> list[str]:
    """Map the prompt-file placeholder to its parent-facing display token."""
    return [PROMPT_FILE_DISPLAY if item == PROMPT_FILE_ARG_PLACEHOLDER else item for item in argv]


def devin_display_argv(argv: list[str]) -> list[str]:
    """Map devin's agent-config and prompt-file placeholders to display tokens."""
    return [
        DEVIN_AGENT_CONFIG_DISPLAY
        if item == DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER
        else PROMPT_FILE_DISPLAY
        if item == PROMPT_FILE_ARG_PLACEHOLDER
        else item
        for item in argv
    ]


def persona_display_argv(argv: list[str]) -> list[str]:
    return [PERSONA_FILE_DISPLAY if item == PERSONA_FILE_ARG_PLACEHOLDER else item for item in argv]


PROMPT_TRANSPORT_ARGV = "argv"
PROMPT_TRANSPORT_FILE = "file"
PROMPT_TRANSPORT_STDIN = "stdin"

# Engines whose prompt rides child argv and is therefore subject to ARG_MAX.
# Shared by the workflow interpolation guard and the resume final-prompt guard
# so the covered-engine set cannot drift between the two. Kimi is the last one:
# cursor (2026.09.02-c22c1a3) and omp (18.1.13) both read a piped prompt, so they
# use stdin and keep the prompt out of /proc/<pid>/cmdline.
ARGV_PROMPT_TRANSPORT_ENGINES = ("kimi",)
ARGV_PROMPT_GUARD_BYTES = 100 * 1024
