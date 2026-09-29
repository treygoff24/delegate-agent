"""Completion notification over the ``post`` CLI (``--notify``).

Delegate is an OSS tool; ``post`` is an optional sibling. Everything here is
probe-and-degrade: a missing binary, a refused send, or a timeout becomes
``notify: {ok: false, reason, detail?}`` in the run manifest plus a
``notify_degraded`` warning, and never changes the run's own result. The message is metadata only (never prompt or output text), and the
sender identity is whatever ``post`` resolves for the CALLER — the run's
source workspace is the cwd and the inherited environment carries any
``POST_FROM`` pin; Delegate never synthesizes a sender.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404 - fixed post argv, shell=False.
from collections.abc import Mapping
from dataclasses import dataclass

from delegate_agent.errors import DelegateError
from delegate_agent.json_types import JsonObject

NOTIFY_KINDS = ("room", "channel")
_TARGET_RE = re.compile(r"^(room|channel):([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
# post 0.9.0 ids: room sends print YYYYMMDD-HHMMSS-<6 hex>, channel sends add a
# microsecond field (YYYYMMDD-HHMMSS-NNNNNN-<6 hex>).
_MESSAGE_ID_RE = re.compile(r"\b\d{8}-\d{6}(?:-\d{6})?-[0-9a-f]{6}\b")
NOTIFY_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class NotifyTarget:
    kind: str
    name: str

    @property
    def spec(self) -> str:
        return f"{self.kind}:{self.name}"


REASON_NOT_FOUND = "post_not_found"
REASON_TIMEOUT = "post_timeout"
REASON_LAUNCH_FAILED = "post_launch_failed"
REASON_FAILED = "post_failed"
REASON_HOOK_FAILED = "notify_hook_failed"
DETAIL_LIMIT = 200


@dataclass(frozen=True)
class NotifyOutcome:
    ok: bool
    target: str
    reason: str | None = None
    detail: str | None = None
    message_id: str | None = None
    # post said crossed_send (an older post's refusal); drives one retry only.
    crossed_send: bool = False

    def payload(self) -> JsonObject:
        result: JsonObject = {"target": self.target, "ok": self.ok}
        if self.message_id is not None:
            result["messageId"] = self.message_id
        if self.reason is not None:
            result["reason"] = self.reason
        if self.detail is not None:
            result["detail"] = self.detail
        return result


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _error_detail(stdout: str, stderr: str) -> str | None:
    """post's structured error message when it printed one, else stderr."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            text = f"{code}: {message}" if code and message else (message or code)
            if isinstance(text, str) and text:
                return _CONTROL_RE.sub("", text)[:DETAIL_LIMIT]
    return _first_line(stderr)


def _first_line(text: str) -> str | None:
    """post's first non-empty stderr line, control characters stripped, capped."""
    for line in text.splitlines():
        cleaned = _CONTROL_RE.sub("", line).strip()
        if cleaned:
            return cleaned[:DETAIL_LIMIT]
    return None


def parse_notify_target(value: str) -> NotifyTarget:
    match = _TARGET_RE.match(value)
    if match is None:
        raise DelegateError(
            "invalid_notify_target",
            "--notify must be room:<name> or channel:<name> "
            "(name: [A-Za-z0-9][A-Za-z0-9._-]{0,63}).",
        )
    return NotifyTarget(kind=match.group(1), name=match.group(2))


def notify_message(
    *,
    run_id: str,
    status: str,
    engine: str,
    model: str | None,
    elapsed_sec: float | None,
    workspace: str,
) -> str:
    lane = f"{engine}/{model}" if model else engine
    elapsed = f"{elapsed_sec:.0f}s" if elapsed_sec is not None else "-"
    basename = workspace.rstrip("/").rsplit("/", 1)[-1] or workspace
    return f"delegate {run_id} {status} {lane} {elapsed} — {basename}"


def notify_argv(target: NotifyTarget, message: str) -> list[str]:
    if target.kind == "room":
        return [
            "post",
            "send",
            "--to",
            target.name,
            "--kind",
            "signal",
            "--subject",
            "delegate",
            # No self-delivery flag: post 0.9.0 dropped `--allow-self` (it
            # answers `unexpected argument`) and no longer refuses a send whose
            # sender room equals the target. A workspace-room fan-out never
            # includes the sending participant itself, so the ping reaches the
            # room's other participants.
            "--body",
            message,
        ]
    # post 0.9.0 always delivers a channel send and lists what it crossed, and
    # `--anyway` is no longer on its documented surface. An older post refuses a
    # send that crossed an unseen mention with `crossed_send`; send_notification
    # retries that one case with CHANNEL_CROSSED_SEND_FLAG.
    return ["post", "chat", target.name, "--send", "--body", message]


CHANNEL_CROSSED_SEND_FLAG = "--anyway"
_CROSSED_SEND_RE = re.compile(r"\bcrossed_send\b")


def send_notification(
    target: NotifyTarget,
    message: str,
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    timeout: float = NOTIFY_TIMEOUT_SEC,
) -> NotifyOutcome:
    """Send one metadata line; degrade (never raise) on every failure.

    ``reason`` is a stable code; ``detail`` is post's first non-empty stderr
    line, truncated. post runs in its own session so a timeout kills the whole
    process group rather than only the direct child.
    """
    binary = shutil.which("post", path=(env or {}).get("PATH") if env else None)
    if binary is None:
        return NotifyOutcome(ok=False, target=target.spec, reason=REASON_NOT_FOUND)
    argv = [binary, *notify_argv(target, message)[1:]]
    outcome = _run_post(argv, target, cwd=cwd, env=env, timeout=timeout)
    if (
        target.kind == "channel"
        and not outcome.ok
        and outcome.reason == REASON_FAILED
        and outcome.crossed_send
    ):
        # An older post refused because the send crossed an unseen mention; a
        # status ping is not a reply to that conversation, so send it anyway.
        retry = [*argv[:4], CHANNEL_CROSSED_SEND_FLAG, *argv[4:]]
        outcome = _run_post(retry, target, cwd=cwd, env=env, timeout=timeout)
    return outcome


def _run_post(
    argv: list[str],
    target: NotifyTarget,
    *,
    cwd: str,
    env: Mapping[str, str] | None,
    timeout: float,
) -> NotifyOutcome:
    try:
        process = subprocess.Popen(  # nosec B603 - fixed argv, shell=False.
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return NotifyOutcome(
            ok=False,
            target=target.spec,
            reason=REASON_LAUNCH_FAILED,
            detail=str(exc)[:DETAIL_LIMIT],
        )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            process.communicate(timeout=5)
        return NotifyOutcome(ok=False, target=target.spec, reason=REASON_TIMEOUT)
    if process.returncode != 0:
        return NotifyOutcome(
            ok=False,
            target=target.spec,
            reason=REASON_FAILED,
            detail=_error_detail(stdout or "", stderr or "") or f"post exited {process.returncode}",
            crossed_send=bool(_CROSSED_SEND_RE.search(f"{stdout or ''}\n{stderr or ''}")),
        )
    found = _MESSAGE_ID_RE.search(stdout or "")
    return NotifyOutcome(ok=True, target=target.spec, message_id=found.group(0) if found else None)
