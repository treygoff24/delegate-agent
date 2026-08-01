"""Fail-closed AI_PROFILE guard for the Python CLI entrypoint.

``bin/delegate-profile-shim`` is a *template* users may copy in front of the
installed ``delegate`` command. It is not the only way to reach
``delegate_agent.cli:main`` — the pip console script, ``python -m
delegate_agent.cli``, and ``bin/delegate.py`` all call it directly, with no
shell shim in front. This module re-implements the shim's fail-closed check
inside the Python CLI itself so the guarantee holds regardless of entrypoint:
a recognized ``AI_PROFILE`` (``work``/``personal``) with a missing overlay
config must never silently fall through to the base account for a command
that launches a child harness or mutates state. See docs/security-model.md.

When the shim already validated and exported ``DELEGATE_CONFIG``, this guard
does not re-fire: its trigger condition requires ``DELEGATE_CONFIG`` to be
unset, so a successful shim run and a direct Python invocation compose
without double warnings or a redundant check.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent.errors import DelegateError
from delegate_agent.request_models import ParsedCommand

# Mirrors bin/delegate-profile-shim's `_delegate_is_readonly_request` allow-list.
# Kept as the real subcommand names the parser produces (not positional argv
# guessing) so the two layers can only drift if someone edits one and forgets
# the other, not because of divergent classification logic.
READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "help",
        "version",
        "agent-help",
        "personas",
        "describe",
        "profiles",
        "snapshot",
        "runs",
        "ps",
        "run-output",
    }
)
READ_ONLY_WORKTREE_ACTIONS = frozenset({"show", "list"})
READ_ONLY_WORKFLOW_ACTIONS = frozenset(
    {"check", "status", "watch", "events", "result", "wait", "list"}
)


def is_read_only_command(parsed: ParsedCommand) -> bool:
    """Classify a parsed command as read-only diagnostic vs launch/mutation.

    Cached ``models`` and ``capabilities`` are read-only, while ``models
    --live`` and ``capabilities refresh`` spawn auth-sensitive child probes.
    ``worktree`` is read-only only for
    ``show``/``list``; ``remove``/``prune``/``gc`` mutate the worktree store.
    ``config``, ``dry-run``, ``wait``, ``cancel``, and every engine/``run``
    launch remain classified as mutation/launch (conservative by design).
    """
    subcommand = parsed.subcommand
    if subcommand in READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand == "models":
        return parsed.inspection is not None and not parsed.inspection.live
    if subcommand == "capabilities":
        return parsed.capabilities is not None and not parsed.capabilities.refresh
    if subcommand == "worktree":
        return parsed.worktree is not None and parsed.worktree.action in READ_ONLY_WORKTREE_ACTIONS
    if subcommand == "workflow":
        return (
            parsed.workflow_command is not None
            and parsed.workflow_command.action in READ_ONLY_WORKFLOW_ACTIONS
        )
    if subcommand == "mail":
        command = parsed.mail_command
        if command is None:
            return False
        if command.action in {"inbox", "status", "watch", "hook-pump"}:
            return True
        return command.action == "read" and command.peek
    return False


def _fix_message(profile: str, config_path: Path) -> str:
    return (
        f"delegate: AI_PROFILE={profile} but {config_path} is missing/unreadable; "
        "refusing to run a launch or mutation command on the wrong account.\n"
        "Fix: create the profile config from config.example.json, or run "
        "'env -u AI_PROFILE delegate config sync-profiles' to materialize missing profile overlays.\n"
        "Temporary bypasses: 'env -u AI_PROFILE delegate ...' uses the base config (work Claude, "
        "ambient Codex), or set 'DELEGATE_CONFIG=/path/to/config.json delegate ...'."
    )


def enforce_profile_guard(parsed: ParsedCommand, *, stderr: TextIO) -> None:
    """Fail closed when AI_PROFILE names a profile whose overlay config is missing.

    No-ops (silently) when: DELEGATE_CONFIG is already set (shim precedence,
    or the user set it directly), or AI_PROFILE is unset/empty. Warns and
    proceeds for an unrecognized non-empty AI_PROFILE, or for a read-only
    command with a missing overlay. Raises DelegateError (fail closed) for a
    launch/mutation command with a missing or unreadable overlay.
    """
    if os.environ.get(delegate_config.CONFIG_ENV):
        return
    profile = os.environ.get("AI_PROFILE", "")
    if not profile:
        return
    if profile not in delegate_config.PROFILE_CONFIG_NAMES:
        print(
            f"delegate: AI_PROFILE={profile!r} is not a recognized profile (work|personal); "
            "running on the base account",
            file=stderr,
        )
        return

    config_path = delegate_config.profile_config_path(
        delegate_config.default_config_path(), profile
    )
    if config_path.is_file() and os.access(config_path, os.R_OK):
        return

    if is_read_only_command(parsed):
        print(
            f"delegate: warning: AI_PROFILE={profile} but {config_path} is missing/unreadable; "
            f"continuing because '{parsed.subcommand}' is read-only. Launch and mutation commands "
            "remain blocked until the profile config exists.",
            file=stderr,
        )
        return

    raise DelegateError("profile_config_missing", _fix_message(profile, config_path))
