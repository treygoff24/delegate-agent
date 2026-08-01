"""Launch-scoped mail push adapters built on :mod:`mail_core`."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from delegate_agent import private_io, run_registry
from delegate_agent.constants import KNOWN_ENGINES
from delegate_agent.json_types import JsonObject
from delegate_agent.mail_core import (
    LANE_FRAMING,
    MAIL_PUSH_CODEX_HOME_NAME,
    MailError,
    _box_dir,
    _ensure_box,
    _error,
    _identity,
    _iter_box_messages,
    _legacy_codex_homes,
    _message_view,
    _read_json,
    _remove_directory,
)

MAIL_PUSH_SCHEMA = "delegate.mail-push.v1"
MAIL_PUSH_MAX_MESSAGES = 50
MAIL_PUSH_MAX_BYTES = 512 * 1024
MAIL_PUSH_CURSOR_FILE_NAME = "hook-cursor.json"
MAIL_PUSH_PENDING_FILE_NAME = "hook-pending.json"
MAIL_PUSH_FAILURE_FILE_NAME = "hook-degraded.json"
MAIL_PUSH_NONCE_FILE_NAME = "mail-hook-nonce"
MAIL_PUSH_SETTINGS_FILE_NAME = "settings.json"
MAIL_PUSH_FALLBACK_CODEX_HOME_NAME = "codex-home-fallback"
MAIL_PUSH_WARNING_PREFIX = "mail push degraded to pull"
MAIL_PUSH_EVENT_KIND = "mail_push_degraded"
MAIL_PUSH_FAILURE_SENTINEL = "DELEGATE_MAIL_HOOK_FAILURE:"

MAIL_PUSH_ADAPTER_ROWS: dict[str, str] = {
    engine: "verified" if engine in {"claude", "codex"} else "unverified"
    for engine in KNOWN_ENGINES
}
if set(MAIL_PUSH_ADAPTER_ROWS) != set(KNOWN_ENGINES):
    raise RuntimeError("Every known engine needs an explicit mail push adapter row.")


@dataclass
class MailPushProvision:
    argv: list[str]
    display_argv: list[str] | None
    codex_home: str | None = None
    warning: str | None = None
    original_argv: list[str] | None = None
    original_display_argv: list[str] | None = None
    env: dict[str, str] | None = None
    original_env: dict[str, str] | None = None
    fallback_env: dict[str, str] | None = None


def mail_push_adapter(engine: str) -> str:
    return MAIL_PUSH_ADAPTER_ROWS.get(engine, "unverified")


def mail_push_warning(engine: str, reason: str | None = None) -> str:
    detail = reason or "the stop-hook adapter is not verified; pull mailbox remains active"
    return f"{MAIL_PUSH_WARNING_PREFIX} for {engine}: {detail}."


def _delegate_hook_entry_script() -> Path:
    launched_as = Path(sys.argv[0]).expanduser()
    with suppress(OSError):
        launched_as = launched_as.resolve(strict=True)
    if launched_as.is_file() and launched_as.name in {"delegate", "delegate.py"}:
        return launched_as

    development_entry = Path(__file__).resolve().parents[2] / "bin" / "delegate.py"
    if development_entry.is_file():
        return development_entry

    installed_entry = shutil.which("delegate")
    if installed_entry:
        return Path(installed_entry).resolve(strict=False)
    raise OSError("could not resolve the launching delegate entry script")


def _hook_command(nonce: str, source_root: Path) -> str:
    pump = " ".join(
        (
            shlex.quote(sys.executable),
            shlex.quote(str(_delegate_hook_entry_script())),
            "--cwd",
            shlex.quote(str(source_root.resolve(strict=False))),
            "mail",
            "hook-pump",
        )
    )
    sentinel = shlex.quote(hook_failure_sentinel(nonce, "hook_pump_unreachable"))
    return f"{pump} || printf '%s\\n' {sentinel} >&2"


def _hook_settings(hook_command: str) -> JsonObject:
    hook = {"type": "command", "command": hook_command}
    return {
        "hooks": {
            "Stop": [{"hooks": [hook]}],
        }
    }


def _copy_private_file(source: Path, destination: Path) -> None:
    source_name = source.name
    try:
        source = source.resolve(strict=True)
    except OSError:
        raise OSError(f"required Codex file is unavailable: {source_name}") from None
    if not source.is_file():
        raise OSError(f"required Codex file is unavailable: {source.name}")
    private_io.write_private_bytes(destination, source.read_bytes())


def _remove_legacy_codex_homes(registry_root: Path) -> None:
    for legacy_home in _legacy_codex_homes(registry_root):
        _remove_directory(legacy_home)


def _private_codex_home_path(registry_root: Path, run_id: str, name: str) -> Path:
    return run_registry.run_directory(registry_root, run_id) / name


def _hook_nonce_path(registry_root: Path, run_id: str) -> Path:
    return run_registry.run_directory(registry_root, run_id) / MAIL_PUSH_NONCE_FILE_NAME


def _codex_home_for_mail_push(
    registry_root: Path,
    run_id: str,
    env: Mapping[str, str],
    hook_command: str,
    *,
    name: str = MAIL_PUSH_CODEX_HOME_NAME,
) -> Path:
    codex_home = _private_codex_home_path(registry_root, run_id, name)
    _remove_directory(codex_home)
    private_io.ensure_private_dir(codex_home)
    source_home = Path(
        os.path.expanduser(
            env.get("CODEX_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        )
    ).resolve(strict=False)
    auth_json = source_home / "auth.json"
    if auth_json.is_file() and not auth_json.is_symlink():
        _copy_private_file(auth_json, codex_home / "auth.json")
    config_toml = source_home / "config.toml"
    if config_toml.is_file() and not config_toml.is_symlink():
        _copy_private_file(config_toml, codex_home / "config.toml")
    if source_home.is_dir() and not source_home.is_symlink():
        for source in source_home.iterdir():
            if source.name in {"auth.json", "config.toml", "hooks.json"}:
                continue
            target = codex_home / source.name
            target.symlink_to(source, target_is_directory=source.is_dir())
    private_io.write_json_atomic(codex_home / "hooks.json", _hook_settings(hook_command))
    return codex_home


def mail_push_fallback_env_overrides(
    provision: MailPushProvision | None,
    fallback_env_overrides: Mapping[str, str],
    registry_root: Path,
    run_id: str,
) -> dict[str, str]:
    """Keep a Codex fallback account distinct while preserving the mail hook."""
    del registry_root, run_id
    if provision is not None and provision.fallback_env is not None:
        return dict(provision.fallback_env)
    return dict(fallback_env_overrides)


def cleanup_mail_push_private_homes(registry_root: Path, run_id: str) -> None:
    for name in (MAIL_PUSH_CODEX_HOME_NAME, MAIL_PUSH_FALLBACK_CODEX_HOME_NAME):
        _remove_directory(_private_codex_home_path(registry_root, run_id, name))


def _set_claude_settings(argv: list[str], settings_path: str) -> None:
    try:
        index = argv.index("--settings")
    except ValueError:
        argv.extend(["--settings", settings_path])
        return
    if index + 1 >= len(argv):
        argv.append(settings_path)
    else:
        argv[index + 1] = settings_path


def provision_mail_push(
    engine: str,
    argv: list[str],
    display_argv: list[str] | None,
    registry_root: Path,
    run_id: str,
    env: dict[str, str],
    fallback_env: Mapping[str, str] | None = None,
    _codex_home_factory: object | None = None,
) -> MailPushProvision:
    """Provision an audited stop hook using only run-scoped mail storage."""
    if mail_push_adapter(engine) != "verified":
        return MailPushProvision(
            list(argv),
            None if display_argv is None else list(display_argv),
            warning=mail_push_warning(engine),
        )

    original_env = dict(env)
    updated_env = dict(env)
    updated_fallback = dict(fallback_env or {})
    codex_home_factory = _codex_home_factory or _codex_home_for_mail_push
    try:
        _remove_legacy_codex_homes(registry_root)
        box = _ensure_box(registry_root, run_id)
        private_io.write_json_atomic(
            box / MAIL_PUSH_CURSOR_FILE_NAME,
            {"schema": MAIL_PUSH_SCHEMA, "lastSeq": 0},
        )
        nonce = secrets.token_urlsafe(24)
        private_io.write_private_text_atomic(_hook_nonce_path(registry_root, run_id), nonce)
        hook_command = _hook_command(nonce, registry_root.parent)
        private_io.write_json_atomic(
            box / MAIL_PUSH_SETTINGS_FILE_NAME, _hook_settings(hook_command)
        )
        updated_env["DELEGATE_MAIL_HOOK_HARNESS"] = engine
        updated_env["DELEGATE_MAIL_HOOK_NONCE"] = nonce
        updated = list(argv)
        updated_display = None if display_argv is None else list(display_argv)
        codex_home: str | None = None
        if engine == "claude":
            settings_path = str((box / MAIL_PUSH_SETTINGS_FILE_NAME).resolve(strict=False))
            _set_claude_settings(updated, settings_path)
            if updated_display is not None:
                _set_claude_settings(updated_display, settings_path)
        else:
            codex_home_path = codex_home_factory(registry_root, run_id, updated_env, hook_command)
            codex_home = str(codex_home_path.resolve(strict=False))
            updated_env["CODEX_HOME"] = codex_home
            if updated_fallback:
                fallback_home = codex_home_factory(
                    registry_root,
                    run_id,
                    updated_fallback,
                    hook_command,
                    name=MAIL_PUSH_FALLBACK_CODEX_HOME_NAME,
                )
                updated_fallback["CODEX_HOME"] = str(fallback_home.resolve(strict=False))
                # The fallback attempt must keep the authenticated hook identity:
                # without these keys its hook cannot emit a nonce-verified
                # sentinel and unrecordable failures would go silent.
                updated_fallback["DELEGATE_MAIL_HOOK_HARNESS"] = engine
                updated_fallback["DELEGATE_MAIL_HOOK_NONCE"] = nonce
            for target in (updated, updated_display):
                if target is None:
                    continue
                insert_at = max(len(target) - 1, 0)
                flags: list[str] = []
                if "hooks=true" not in target:
                    flags.extend(["-c", "hooks=true"])
                if "--dangerously-bypass-hook-trust" not in target:
                    flags.append("--dangerously-bypass-hook-trust")
                target[insert_at:insert_at] = flags
        env.clear()
        env.update(updated_env)
        return MailPushProvision(
            updated,
            updated_display,
            codex_home=codex_home,
            original_argv=list(argv),
            original_display_argv=None if display_argv is None else list(display_argv),
            env=env,
            original_env=original_env,
            fallback_env=updated_fallback,
        )
    except Exception:
        env.clear()
        env.update(original_env)
        cleanup_failed = False
        try:
            cleanup_mail_push_private_homes(registry_root, run_id)
            _hook_nonce_path(registry_root, run_id).unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True
        return MailPushProvision(
            list(argv),
            None if display_argv is None else list(display_argv),
            warning=mail_push_warning(
                engine,
                "launch-scoped hook provisioning cleanup failed"
                if cleanup_failed
                else "launch-scoped hook provisioning failed",
            ),
            fallback_env=dict(fallback_env or {}),
        )


def _hook_cursor_path(registry_root: Path, run_id: str) -> Path:
    return _box_dir(registry_root, run_id) / MAIL_PUSH_CURSOR_FILE_NAME


def _hook_pending_path(registry_root: Path, run_id: str) -> Path:
    return _box_dir(registry_root, run_id) / MAIL_PUSH_PENDING_FILE_NAME


def _hook_failure_path(registry_root: Path, run_id: str) -> Path:
    return _box_dir(registry_root, run_id) / MAIL_PUSH_FAILURE_FILE_NAME


def _record_hook_failure(
    registry_root: Path,
    run_id: str | None,
    *,
    harness: str,
    reason: str,
) -> bool:
    if run_id is None or not run_registry.RUN_ID_RE.fullmatch(run_id):
        return False
    try:
        _ensure_box(registry_root, run_id)
        private_io.write_json_atomic_if_absent(
            _hook_failure_path(registry_root, run_id),
            {
                "schema": MAIL_PUSH_SCHEMA,
                "runId": run_id,
                "harness": harness,
                "reason": reason[:200],
                "recordedAt": run_registry.utc_now_iso(),
            },
        )
        return True
    except (OSError, MailError):
        return False


def hook_failure_sentinel(nonce: str, reason: str) -> str:
    return f"{MAIL_PUSH_FAILURE_SENTINEL}{nonce}:{reason[:200]}"


def hook_failure_reason_from_stderr(stderr_text: str, *, nonce: str | None) -> str | None:
    if not nonce:
        return None
    prefix = f"{MAIL_PUSH_FAILURE_SENTINEL}{nonce}:"
    for line in stderr_text.splitlines():
        if line.startswith(prefix):
            reason = line.removeprefix(prefix).strip()
            return reason or "hook failure could not be recorded"
    return None


def read_hook_failure_nonce(registry_root: Path, run_id: str) -> str | None:
    try:
        nonce = private_io.read_private_text_bounded(
            _hook_nonce_path(registry_root, run_id), max_bytes=128
        )
    except private_io.BoundedReadError:
        return None
    nonce = nonce.strip()
    return nonce or None


def read_hook_failure_marker(registry_root: Path, run_id: str) -> str | None:
    try:
        marker = _read_json(_hook_failure_path(registry_root, run_id))
    except MailError:
        return "hook failure marker was unreadable"
    reason = marker.get("reason") if marker is not None else None
    return reason if isinstance(reason, str) and reason else None


def _hook_cursor(registry_root: Path, run_id: str) -> int:
    cursor = _read_json(_hook_cursor_path(registry_root, run_id))
    if cursor is None:
        return 0
    value = cursor.get("lastSeq")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _error("mail_push_cursor_invalid", "The mail push cursor is invalid.")
    return value


def _hook_pending(registry_root: Path, run_id: str) -> JsonObject | None:
    pending = _read_json(_hook_pending_path(registry_root, run_id))
    if pending is None:
        return None
    last_seq = pending.get("lastSeq")
    message_ids = pending.get("messageIds")
    marker_run_id = pending.get("runId")
    if (
        not isinstance(last_seq, int)
        or isinstance(last_seq, bool)
        or last_seq < 0
        or not isinstance(message_ids, list)
        or not message_ids
        or any(not isinstance(value, str) for value in message_ids)
        or marker_run_id != run_id
    ):
        raise _error("mail_push_pending_invalid", "The mail push pending marker is invalid.")
    return pending


def _hook_payload(messages: list[tuple[Path, JsonObject, str]]) -> bytes:
    rows = [
        {
            "framing": dict(LANE_FRAMING),
            "message": _message_view(envelope, body),
        }
        for _path, envelope, body in messages
    ]
    payload: JsonObject = {
        "schema": MAIL_PUSH_SCHEMA,
        "framing": dict(LANE_FRAMING),
        "messages": rows,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bounded_hook_messages(
    registry_root: Path, run_id: str, *, max_bytes: int | None = None
) -> tuple[list[tuple[Path, JsonObject, str]], int]:
    max_payload_bytes = MAIL_PUSH_MAX_BYTES if max_bytes is None else max_bytes
    cursor = _hook_cursor(registry_root, run_id)
    unread = []
    for item in _iter_box_messages(registry_root, run_id, limit=None):
        sequence = item[1].get("seq")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise _error("mail_unreadable", "Unread mail has an invalid sequence number.")
        if sequence > cursor:
            unread.append(item)
    selected: list[tuple[Path, JsonObject, str]] = []
    for item in unread[:MAIL_PUSH_MAX_MESSAGES]:
        candidate = [*selected, item]
        if len(_hook_payload(candidate)) > max_payload_bytes:
            if not selected:
                raise _error(
                    "mail_push_batch_too_large",
                    f"The next mail push batch exceeds {max_payload_bytes} bytes.",
                )
            break
        selected.append(item)
    return selected, cursor


def _pending_messages(
    registry_root: Path, run_id: str, pending: JsonObject
) -> list[tuple[Path, JsonObject, str]]:
    cursor = _hook_cursor(registry_root, run_id)
    last_seq = pending["lastSeq"]
    message_ids = pending["messageIds"]
    assert isinstance(last_seq, int)
    assert isinstance(message_ids, list)
    messages_by_id: dict[str, tuple[Path, JsonObject, str]] = {}
    for directory in ("inbox", "read"):
        for item in _iter_box_messages(registry_root, run_id, directory, limit=None):
            sequence = item[1].get("seq")
            message_id = item[1].get("msgId")
            if (
                isinstance(sequence, int)
                and not isinstance(sequence, bool)
                and cursor < sequence <= last_seq
                and isinstance(message_id, str)
            ):
                messages_by_id[message_id] = item
    messages = [
        messages_by_id[message_id] for message_id in message_ids if message_id in messages_by_id
    ]
    if len(messages) != len(message_ids):
        raise _error(
            "mail_push_pending_unavailable", "The pending mail push batch is no longer available."
        )
    return messages


def _write_hook_pending(
    registry_root: Path, run_id: str, messages: list[tuple[Path, JsonObject, str]]
) -> None:
    last_seq = messages[-1][1].get("seq")
    message_ids = [item[1].get("msgId") for item in messages]
    if (
        not isinstance(last_seq, int)
        or isinstance(last_seq, bool)
        or any(not isinstance(value, str) for value in message_ids)
    ):
        raise _error("mail_unreadable", "Injected mail has an invalid sequence number.")
    private_io.write_json_atomic(
        _hook_pending_path(registry_root, run_id),
        {
            "schema": MAIL_PUSH_SCHEMA,
            "runId": run_id,
            "lastSeq": last_seq,
            "messageIds": message_ids,
            "emitted": False,
        },
    )


def _mark_hook_pending_emitted(registry_root: Path, run_id: str, pending: JsonObject) -> None:
    private_io.write_json_atomic(
        _hook_pending_path(registry_root, run_id),
        {**pending, "emitted": True},
    )


def _promote_emitted_hook_pending(registry_root: Path, run_id: str, pending: JsonObject) -> None:
    last_seq = pending["lastSeq"]
    assert isinstance(last_seq, int)
    private_io.write_json_atomic(
        _hook_cursor_path(registry_root, run_id),
        {"schema": MAIL_PUSH_SCHEMA, "lastSeq": last_seq},
    )
    _hook_pending_path(registry_root, run_id).unlink(missing_ok=True)


def _write_hook_response(stdout: TextIO, payload: bytes) -> None:
    text = payload.decode("utf-8") + "\n"
    if stdout.write(text) != len(text):
        raise OSError("hook response was only partially written")
    stdout.flush()


def hook_pump(
    registry_root: Path,
    *,
    stdout: TextIO,
    stderr: TextIO | None = None,
    env: Mapping[str, str | None] | None = None,
    max_bytes: int | None = None,
    _mark_emitted_fn: object | None = None,
) -> int:
    """Emit one bounded stop-hook batch with at-least-once pending acknowledgement."""
    environ = os.environ if env is None else env
    run_id = environ.get("DELEGATE_RUN_ID")
    harness = environ.get("DELEGATE_MAIL_HOOK_HARNESS") or "unknown"
    response_emitted = False
    response_emission_attempted = False
    mark_emitted = _mark_emitted_fn or _mark_hook_pending_emitted

    def emit_response(payload: bytes) -> None:
        nonlocal response_emission_attempted
        response_emission_attempted = True
        _write_hook_response(stdout, payload)

    try:
        identity = _identity(registry_root, env=environ)
        if identity.is_coordinator or run_id is None:
            raise _error("hook_requires_lane", "mail hook-pump requires a bound work lane.")
        pending = _hook_pending(registry_root, run_id)
        if pending is not None and pending.get("emitted") is True:
            _promote_emitted_hook_pending(registry_root, run_id, pending)
            emit_response(b"{}")
            return 0
        if pending is None:
            messages, _cursor = _bounded_hook_messages(registry_root, run_id, max_bytes=max_bytes)
        else:
            messages = _pending_messages(registry_root, run_id, pending)
        if not messages:
            emit_response(b"{}")
            return 0
        if pending is None:
            _write_hook_pending(registry_root, run_id, messages)
            pending = _hook_pending(registry_root, run_id)
        assert pending is not None
        payload = _hook_payload(messages)
        response_key = "reason" if harness == "claude" else "additionalContext"
        response = json.dumps(
            {"decision": "block", response_key: payload.decode("utf-8")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        emit_response(response)
        response_emitted = True
        try:
            mark_emitted(registry_root, run_id, pending)
        except (MailError, OSError, ValueError):
            return 1
        return 0
    except (MailError, OSError, ValueError) as exc:
        if response_emitted or response_emission_attempted:
            return 1
        _record_hook_failure(
            registry_root,
            run_id,
            harness=harness,
            reason=f"{getattr(exc, 'error', 'hook_runtime_failed')}: {exc}",
        )
        print(f"{getattr(exc, 'error', 'hook_runtime_failed')}: {exc}", file=stderr or sys.stderr)
        return 1
