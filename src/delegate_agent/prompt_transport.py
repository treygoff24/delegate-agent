from __future__ import annotations

CURSOR_PROMPT_REDACTION = "<prompt redacted: cursor argv transport>"
KIMI_PROMPT_REDACTION = "<prompt redacted: kimi argv transport>"
OMP_PROMPT_REDACTION = "<prompt redacted: omp argv transport>"
PROMPT_FILE_ARG_PLACEHOLDER = "<delegate-prompt-file>"
PROMPT_FILE_DISPLAY = "<prompt file>"
DROID_PROMPT_FILE_ARG_PLACEHOLDER = PROMPT_FILE_ARG_PLACEHOLDER
DROID_PROMPT_FILE_DISPLAY = PROMPT_FILE_DISPLAY
DEVIN_AGENT_CONFIG_ARG_PLACEHOLDER = "<delegate-devin-agent-config>"
DEVIN_AGENT_CONFIG_DISPLAY = "<devin agent config>"


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


PROMPT_TRANSPORT_ARGV = "argv"
PROMPT_TRANSPORT_FILE = "file"
PROMPT_TRANSPORT_STDIN = "stdin"
