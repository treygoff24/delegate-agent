"""Stable public facade for Delegate mail.

Storage and pull-mail behavior live in :mod:`mail_core`; launch-scoped hook
adapters live in :mod:`mail_push`.  Keep this module as the caller-facing seam
so existing imports and patches keep their established dispatch behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import config as delegate_config
from delegate_agent import mail_core as _core
from delegate_agent import mail_push as _push
from delegate_agent import profiles
from delegate_agent.json_types import JsonObject
from delegate_agent.mail_core import *  # noqa: F403
from delegate_agent.mail_core import (
    MailCommand,
    _next_message_id,
    _recipient_for_alias,
    prepare_mail_storage,
    wire_work_mail_launch,
)
from delegate_agent.mail_push import *  # noqa: F403
from delegate_agent.mail_push import (
    MAIL_PUSH_MAX_BYTES,
    MailPushProvision,
    _codex_home_for_mail_push,
    _mark_hook_pending_emitted,
    mail_push_fallback_env_overrides,
)
from delegate_agent.mail_push import (
    provision_mail_push as _provision_mail_push,
)
from delegate_agent.request_models import Request


def __getattr__(name: str) -> object:
    if hasattr(_push, name):
        return getattr(_push, name)
    return getattr(_core, name)


def launch_enabled(mode: str, config: JsonObject) -> bool:
    return mode == "work" and delegate_config.mail_enabled(config)


def prepare_launch_storage(
    request: Request, config: JsonObject, registry_root: Path, stderr: TextIO
) -> None:
    """Fail open for this launch, keeping explicit mail commands independent."""
    if not launch_enabled(request.mode, config):
        return
    try:
        prepare_mail_storage(registry_root)
    except _core.MailError as exc:
        warning = f"mail disabled for this launch: {exc.error}: {exc.message}"
        if warning not in request.warnings:
            request.warnings = (*request.warnings, warning)
            print(f"delegate mail: WARNING: {warning}", file=stderr)
        config["mail"] = {"enabled": False}
        request.mail_push = False
        # Only remove Delegate's final suffix, never matching text inside user data.
        if request.prompt_instruction_mode == "wrapped" and request.prompt != request.source_prompt:
            prompt = request.prompt.removesuffix("\n\n" + _core.MAIL_PROMPT_SUFFIX)
            request.argv = [prompt if arg == request.prompt else arg for arg in request.argv]
            if request.display_argv is not None:
                request.display_argv = [
                    prompt if arg == request.prompt else arg for arg in request.display_argv
                ]
            if request.stdin_text is not None:
                request.stdin_text = request.stdin_text.removesuffix(
                    "\n\n" + _core.MAIL_PROMPT_SUFFIX
                )
            if request.prompt_file_text is not None:
                request.prompt_file_text = request.prompt_file_text.removesuffix(
                    "\n\n" + _core.MAIL_PROMPT_SUFFIX
                )
            request.prompt = prompt


@dataclass(frozen=True)
class MailLaunchPreparation:
    argv: list[str]
    display_argv: list[str] | None
    provision: MailPushProvision | None
    request_warnings: tuple[str, ...]

    def context_updates(
        self,
        env_overrides: dict[str, str] | None,
        fallback_env_overrides: dict[str, str] | None,
        warnings: tuple[str, ...],
    ) -> dict[str, object]:
        if self.provision is None:
            return {}
        provision = self.provision
        return {
            "env_overrides": env_overrides,
            "fallback_env_overrides": mail_push_fallback_env_overrides(
                provision,
                fallback_env_overrides or {},
            ),
            "warnings": tuple(
                dict.fromkeys((*warnings, *((provision.warning,) if provision.warning else ())))
            ),
        }


def prepare_work_mail_launch(
    *,
    enabled: bool,
    mail_push: bool,
    engine: str,
    argv: list[str],
    display_argv: list[str] | None,
    registry_root: Path,
    run_id: str,
    env_overrides: dict[str, str] | None,
    profile_resolution: profiles.ProfileResolution,
    prompt: str,
    prompt_transport: str,
    stderr: TextIO,
    isolated_workspace: bool,
    warnings: tuple[str, ...],
) -> MailLaunchPreparation:
    if not enabled:
        return MailLaunchPreparation(argv, display_argv, None, warnings)
    launch_warnings = list(warnings)
    argv, display_argv = wire_work_mail_launch(
        engine,
        argv,
        display_argv,
        registry_root,
        prompt=prompt,
        prompt_transport=prompt_transport,
        warnings=launch_warnings,
        isolated_workspace=isolated_workspace,
    )
    warnings = tuple(launch_warnings)
    provision = None
    if mail_push:
        provision = provision_mail_push(
            engine,
            argv,
            display_argv,
            registry_root,
            run_id,
            env_overrides or {},
            profiles.codex_fallback_child_env_overrides(profile_resolution, env_overrides or {}),
        )
        if provision.warning is not None:
            warnings = (*warnings, provision.warning)
            print(f"delegate mail: WARNING: {provision.warning}", file=stderr)
        argv, display_argv = provision.argv, provision.display_argv
    return MailLaunchPreparation(argv, display_argv, provision, warnings)


def send(
    registry_root: Path,
    command: MailCommand,
    *,
    stdin: TextIO | None = None,
    env: Mapping[str, str | None] | None = None,
) -> JsonObject:
    return _core.send(
        registry_root,
        command,
        stdin=stdin,
        env=env,
        _recipient_for_alias_fn=_recipient_for_alias,
        _next_message_id_fn=_next_message_id,
    )


def provision_mail_push(
    engine: str,
    argv: list[str],
    display_argv: list[str] | None,
    registry_root: Path,
    run_id: str,
    env: dict[str, str],
    fallback_env: Mapping[str, str] | None = None,
) -> MailPushProvision:
    return _provision_mail_push(
        engine,
        argv,
        display_argv,
        registry_root,
        run_id,
        env,
        fallback_env,
        _codex_home_factory=_codex_home_for_mail_push,
    )


def hook_pump(
    registry_root: Path,
    *,
    stdout: TextIO,
    stderr: TextIO | None = None,
    env: Mapping[str, str | None] | None = None,
) -> int:
    return _push.hook_pump(
        registry_root,
        stdout=stdout,
        stderr=stderr,
        env=env,
        max_bytes=MAIL_PUSH_MAX_BYTES,
        _mark_emitted_fn=_mark_hook_pending_emitted,
    )


def emit(
    command: MailCommand,
    *,
    workspace: Path,
    stdout: TextIO,
    stderr: TextIO,
    stdin: TextIO | None = None,
) -> int:
    return _core.emit(
        command,
        workspace=workspace,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        _send_fn=send,
        _hook_pump_fn=hook_pump,
    )
