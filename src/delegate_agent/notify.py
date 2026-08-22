"""Completion notification over the ``post`` CLI (``--notify``).

Delegate is an OSS tool; ``post`` is an optional sibling. Everything here is
probe-and-degrade: a missing binary, a refused send, or a timeout becomes a
``notifyDegraded`` record in the run manifest and never changes the run's own
result. The message is metadata only (never prompt or output text), and the
sender identity is whatever ``post`` resolves for the CALLER — the run's
source workspace is the cwd and the inherited environment carries any
``POST_FROM`` pin; Delegate never synthesizes a sender.
"""

from __future__ import annotations

import contextlib
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
_MESSAGE_ID_RE = re.compile(r"\b\d{8}-\d{6}-\d{6}-[0-9a-f]{6}\b")
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

    def payload(self) -> JsonObject:
        result: JsonObject = {"target": self.target, "ok": self.ok}
        if self.message_id is not None:
            result["messageId"] = self.message_id
        if self.reason is not None:
            result["reason"] = self.reason
        if self.detail is not None:
            result["detail"] = self.detail
        return result


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:DETAIL_LIMIT]
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
            "--body",
            message,
        ]
    return ["post", "chat", target.name, "--send", "--anyway", "--body", message]


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
            detail=_first_line(stderr or "") or f"post exited {process.returncode}",
        )
    found = _MESSAGE_ID_RE.search(stdout or "")
    return NotifyOutcome(ok=True, target=target.spec, message_id=found.group(0) if found else None)
