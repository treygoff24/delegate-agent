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

The Python guard validates the overlay even when ``DELEGATE_CONFIG`` is set.
An explicit config selects runtime policy; it does not replace the recognized
``AI_PROFILE`` credential overlay or bypass its fail-closed validation.
"""

from __future__ import annotations

import json
import os
import sys
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
        "doctor",
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
        return parsed.payload is not None and not parsed.payload.live
    if subcommand == "capabilities":
        return parsed.payload is not None and not parsed.payload.refresh
    if subcommand == "worktree":
        return parsed.payload is not None and parsed.payload.action in READ_ONLY_WORKTREE_ACTIONS
    if subcommand == "workflow":
        return parsed.payload is not None and parsed.payload.action in READ_ONLY_WORKFLOW_ACTIONS
    if subcommand == "mail":
        command = parsed.payload
        if command is None:
            return False
        if command.action in {"inbox", "status", "watch"}:
            return True
        return command.action == "read" and command.peek
    return False


def _fix_message(profile: str, config_path: Path) -> str:
    return (
        f"delegate: AI_PROFILE={profile} but {config_path} is missing/unreadable; "
        "refusing to run a launch or mutation command on the wrong account.\n"
        "Fix: create the profile config from config.example.json, or run "
        "'env -u AI_PROFILE delegate config sync-profiles' to materialize missing profile overlays.\n"
        "Temporary bypass: 'env -u AI_PROFILE delegate ...' uses the base config "
        "(work Claude, ambient Codex)."
    )


def enforce_profile_guard(parsed: ParsedCommand, *, stderr: TextIO) -> None:
    """Fail closed when AI_PROFILE names a profile whose overlay config is missing.

    No-ops when AI_PROFILE is unset/empty. Warns and proceeds for an
    unrecognized non-empty AI_PROFILE, or for a read-only command with a
    missing overlay. Raises DelegateError (fail closed) for a launch/mutation
    command with a missing or unreadable overlay.
    """
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

    if os.environ.get("DELEGATE_ESTATE_REALM") == profile and os.environ.get(
        delegate_config.CONFIG_ENV
    ):
        config_path = delegate_config.config_path()
    else:
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


def _explicit_config_realm(path: Path) -> str | None:
    # Inspect the explicit layer, not a default inherited from the base config.
    # A canonical overlay name binds the realm even without profiles.default.
    realms = delegate_config.PROFILE_CONFIG_NAMES
    named = next((r for r in realms if path.name == f"config.{r}.json"), None)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        profiles = value.get("profiles", {}) if isinstance(value, dict) else {}
        configured = profiles.get("default") if isinstance(profiles, dict) else None
    except (OSError, ValueError) as exc:
        raise DelegateError("invalid_realm_config", f"cannot read config {str(path)!r}") from exc
    if configured not in realms:
        configured = None
    if named and configured and named != configured:
        raise DelegateError(
            "realm_conflict", "contradictory realm in config filename and profiles.default"
        )
    return named or configured


def _estate_selection(parsed: ParsedCommand) -> tuple[str, Path | None]:
    realms = delegate_config.PROFILE_CONFIG_NAMES
    flag = parsed.global_options.auth_profile
    if flag is not None and (flag := flag.strip()) not in realms:
        raise DelegateError(
            "invalid_realm", "--auth-profile must name work or personal on the estate"
        )

    raw_config = os.environ.get(delegate_config.CONFIG_ENV)
    explicit = Path(raw_config).expanduser().absolute() if raw_config else None
    config_realm = _explicit_config_realm(explicit) if explicit else None
    if flag and config_realm and flag != config_realm:
        raise DelegateError(
            "realm_conflict",
            "contradictory realm in --auth-profile and DELEGATE_CONFIG; unset one to switch realms",
        )

    realm = flag or config_realm
    if not realm:
        inherited = {
            value
            for name in ("DELEGATE_PROFILE", "AI_PROFILE", "ESTATE_AGENT_REALM")
            if (value := os.environ.get(name))
        }
        if inherited - set(realms):
            raise DelegateError("invalid_realm", "inherited estate realm must be work or personal")
        if len(inherited) > 1:
            raise DelegateError(
                "realm_conflict",
                "contradictory realm in inherited markers; select --auth-profile or DELEGATE_CONFIG",
            )
        realm = next(iter(inherited), "")

    read_only = is_read_only_command(parsed)
    if not realm:
        if not read_only:
            raise DelegateError(
                "missing_realm",
                "no work/personal profile selected; refusing an account-bearing launch or mutation",
            )
        return "", explicit

    overlay = delegate_config.profile_config_path(delegate_config.default_config_path(), realm)
    if not config_realm and not (overlay.is_file() and os.access(overlay, os.R_OK)):
        if flag or not read_only:
            raise DelegateError("profile_config_missing", _fix_message(realm, overlay))
        print(
            f"delegate: warning: {str(overlay)!r} is unavailable; continuing read-only without realm keys",
            file=sys.stderr,
        )
        # Leave realm markers intact but don't ask the launcher to load keys.
        return realm, None
    return realm, explicit or overlay


def emit_estate_selection(argv: list[str]) -> int:
    """Private, names-only preflight for the estate launcher, before sourcing any keys."""
    from delegate_agent import cli_parser

    try:
        realm, path = _estate_selection(cli_parser.parse_cli(argv))
        if path is not None and any(char in str(path) for char in ("\n", "\r")):
            raise DelegateError("invalid_realm_config", "config path must not contain a newline")
    except DelegateError as exc:
        print(f"delegate: {exc.message}", file=sys.stderr)
        return 2
    shown_path = path or delegate_config.default_config_path()
    print(f"delegate: realm={realm or 'none'} config={str(shown_path)!r}", file=sys.stderr)
    print(realm)
    print(path or "")
    return 0
