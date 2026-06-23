from __future__ import annotations

import shlex
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

from delegate_agent import command_errors, harness_events, log_output, redaction, run_registry
from delegate_agent import rendering as delegate_rendering
from delegate_agent import retention as delegate_retention
from delegate_agent.json_types import JsonObject
from delegate_agent.log_output import RUN_OUTPUT_DEFAULT_MAX_CHARS

RECOVERY_STDOUT_TAIL_LINES = 2000
RECOVERY_STDOUT_TAIL_BYTES = 1_000_000
RUN_OUTPUT_DEFAULT_TAIL_LINES = 80
STREAM_READ_CHUNK_KIB = 64
STREAM_READ_CHUNK_BYTES = STREAM_READ_CHUNK_KIB * run_registry.BYTES_PER_KIB


@dataclass(frozen=True)
class RunOutputCommand:
    handle: str
    json_mode: bool = False
    completion_report: bool = False
    stdout: bool = False
    stderr: bool = False
    tail: int | None = None
    max_chars: int | None = None
    raw: bool = False
    no_redact: bool = False
    default: bool = False


class RunOutputError(command_errors.CommandError):
    def __init__(
        self,
        error: str,
        message: str,
        *,
        diagnostics: JsonObject | None = None,
        next_actions: list[str] | None = None,
    ) -> None:
        super().__init__(error, message)
        self.diagnostics = diagnostics
        self.next_actions = next_actions


def _effective_max_chars(command: RunOutputCommand) -> int | None:
    if command.raw:
        return None
    return command.max_chars if command.max_chars is not None else RUN_OUTPUT_DEFAULT_MAX_CHARS


def _add_log_output_section(
    *,
    registry_root: Path,
    run_id: str,
    log_name: str,
    section_name: str,
    tail: int | None,
    raw: bool,
    max_chars: int | None,
    sections: JsonObject,
    text_sections: dict[str, str],
) -> None:
    output = delegate_retention.read_log_output(
        registry_root,
        run_id,
        log_name,
        tail=tail,
        raw=raw,
    )
    content = output.content
    meta: JsonObject = {
        "bytes": delegate_retention.log_file_byte_size(registry_root, run_id, log_name),
        "truncated": output.truncated,
        "archived": delegate_retention.raw_logs_archived(registry_root, run_id),
    }
    if tail is not None and not raw:
        meta["tailLines"] = tail
    if max_chars is not None:
        capped = log_output.cap_content_by_chars(content, max_chars)
        content = capped.content
        meta["maxChars"] = max_chars
        meta["charTruncated"] = capped.char_truncated
        meta["returnedChars"] = capped.returned_chars
        meta["omittedChars"] = capped.omitted_chars
    sections[section_name] = meta
    text_sections[section_name] = content


def _decode_recovery_tail(data: bytes, *, truncated: bool) -> tuple[str, bool]:
    if truncated:
        newline = data.find(b"\n")
        if newline < 0:
            return "", True
        data = data[newline + 1 :]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return "", truncated
    trimmed = lines[-RECOVERY_STDOUT_TAIL_LINES:]
    return "\n".join(trimmed) + "\n", truncated or len(lines) > len(trimmed)


def _read_stream_tail_bytes(stream: BinaryIO, byte_limit: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = stream.read(STREAM_READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > byte_limit:
            del buffer[: len(buffer) - byte_limit]
    return bytes(buffer)


def _read_archived_recovery_stdout_tail(registry_root: Path, run_id: str) -> tuple[str, bool]:
    archive_file = delegate_retention.archive_path(registry_root, run_id)
    if not archive_file.exists():
        return "", False
    with tarfile.open(archive_file, "r:gz") as archive:
        try:
            member = archive.getmember(run_registry.STDOUT_LOG)
        except KeyError:
            return "", False
        extracted = archive.extractfile(member)
        if extracted is None:
            return "", False
        data = _read_stream_tail_bytes(extracted, RECOVERY_STDOUT_TAIL_BYTES)
    return _decode_recovery_tail(data, truncated=member.size > len(data))


def _read_recovery_stdout_tail(registry_root: Path, run_id: str) -> tuple[str, bool]:
    run_path = run_registry.run_directory(registry_root, run_id)
    log_path = run_path / run_registry.STDOUT_LOG
    if not log_path.exists():
        return _read_archived_recovery_stdout_tail(registry_root, run_id)
    with log_path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return "", False
        start = max(0, end - RECOVERY_STDOUT_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(end - start)
    return _decode_recovery_tail(data, truncated=start > 0)


def _recover_completion_report_from_stdout(
    registry_root: Path,
    run_id: str,
    *,
    allow_last_assistant: bool = False,
) -> tuple[str, bool, harness_events.RecoveryQuality | None]:
    # The completion is the final turn's closing message, which lives at the end of
    # the stream, so a bounded tail is sufficient and avoids loading a possibly
    # huge stdout.log into memory on a routine run-output call. The byte bound is
    # as important as the line bound because Codex JSONL can encode large command
    # output inside a single physical line.
    stdout_text, truncated = _read_recovery_stdout_tail(registry_root, run_id)
    if not stdout_text:
        return "", truncated, None
    accumulator = harness_events.StreamAccumulator()
    for line in stdout_text.split("\n"):
        accumulator.ingest_line(line)
    if accumulator.completion_text:
        return accumulator.completion_text.strip(), truncated, "explicit_completion"
    if allow_last_assistant and accumulator.recoverable_assistant_text:
        quality = accumulator.assistant_recovery_quality()
        return accumulator.recoverable_assistant_text.strip(), truncated, quality
    return "", truncated, None


def _run_output_next_actions(handle: str) -> list[str]:
    quoted = shlex.quote(handle)
    return [
        f"delegate run-output {quoted} --stdout --tail {RUN_OUTPUT_DEFAULT_TAIL_LINES}",
        f"delegate run-output {quoted} --stderr --tail {RUN_OUTPUT_DEFAULT_TAIL_LINES}",
        f"delegate run-output {quoted} --raw",
    ]


def _log_section_info(registry_root: Path, run_id: str, log_name: str) -> JsonObject:
    """Probe a stream's presence and byte size once (live file first, then archive)."""
    live_path = run_registry.run_directory(registry_root, run_id) / log_name
    if live_path.exists():
        return {"present": True, "bytes": live_path.stat().st_size}
    size = delegate_retention.log_file_byte_size(registry_root, run_id, log_name)
    return {"present": size > 0, "bytes": size}


def _run_output_diagnostics(
    registry_root: Path,
    run_id: str,
    *,
    recovery_attempted: bool = False,
    recovery_truncated: bool = False,
    recovery_quality: harness_events.RecoveryQuality | None = None,
) -> JsonObject:
    run_path = run_registry.run_directory(registry_root, run_id)
    state = run_registry.load_run_state(registry_root, run_id)
    report_path = run_path / run_registry.COMPLETION_REPORT_FILE
    diagnostics: JsonObject = {
        "status": run_registry.effective_status(state),
        "rawLogsArchived": delegate_retention.raw_logs_archived(registry_root, run_id),
        "completionReport": {
            "present": report_path.exists(),
            "bytes": report_path.stat().st_size if report_path.exists() else 0,
        },
        "stdout": _log_section_info(registry_root, run_id, run_registry.STDOUT_LOG),
        "stderr": _log_section_info(registry_root, run_id, run_registry.STDERR_LOG),
    }
    if recovery_attempted:
        recovery_meta: JsonObject = {
            "attempted": True,
            "source": run_registry.STDOUT_LOG,
            "tailLines": RECOVERY_STDOUT_TAIL_LINES,
            "tailBytes": RECOVERY_STDOUT_TAIL_BYTES,
            "truncated": recovery_truncated,
        }
        if recovery_quality is not None:
            recovery_meta["quality"] = recovery_quality
        diagnostics["recovery"] = recovery_meta
    return diagnostics


def _format_run_output_diagnostics(diagnostics: JsonObject, next_actions: list[str]) -> str:
    stdout_info = diagnostics.get("stdout") if isinstance(diagnostics.get("stdout"), dict) else {}
    stderr_info = diagnostics.get("stderr") if isinstance(diagnostics.get("stderr"), dict) else {}
    recovery = diagnostics.get("recovery") if isinstance(diagnostics.get("recovery"), dict) else {}
    lines = [
        "No completion report or recoverable final message found.",
        f"status: {diagnostics.get('status', 'unknown')}",
        f"stdout: present={stdout_info.get('present', False)} bytes={stdout_info.get('bytes', 0)}",
        f"stderr: present={stderr_info.get('present', False)} bytes={stderr_info.get('bytes', 0)}",
    ]
    if recovery:
        lines.append(f"recovery: truncated={recovery.get('truncated', False)}")
        if recovery.get("quality"):
            lines.append(f"recovery quality: {recovery.get('quality')}")
    lines.append("next actions:")
    lines.extend(f"  - {action}" for action in next_actions)
    return "\n".join(lines) + "\n"


def _add_default_run_output_fallback(
    *,
    registry_root: Path,
    run_id: str,
    alias: str | None,
    sections: JsonObject,
    text_sections: dict[str, str],
    recovery_attempted: bool,
    recovery_truncated: bool,
    recovery_quality: harness_events.RecoveryQuality | None = None,
) -> None:
    diagnostics = _run_output_diagnostics(
        registry_root,
        run_id,
        recovery_attempted=recovery_attempted,
        recovery_truncated=recovery_truncated,
        recovery_quality=recovery_quality,
    )
    for log_name, section_name in (
        (run_registry.STDOUT_LOG, "stdout"),
        (run_registry.STDERR_LOG, "stderr"),
    ):
        stream_info = diagnostics.get(section_name)
        if not (isinstance(stream_info, dict) and stream_info.get("present")):
            continue
        _add_log_output_section(
            registry_root=registry_root,
            run_id=run_id,
            log_name=log_name,
            section_name=section_name,
            tail=RUN_OUTPUT_DEFAULT_TAIL_LINES,
            raw=False,
            max_chars=RUN_OUTPUT_DEFAULT_MAX_CHARS,
            sections=sections,
            text_sections=text_sections,
        )
    handle = alias or run_id
    next_actions = _run_output_next_actions(handle)
    diagnostics["nextActions"] = next_actions
    content = _format_run_output_diagnostics(diagnostics, next_actions)
    sections["diagnostics"] = {"bytes": len(content.encode("utf-8"))}
    text_sections["diagnostics"] = content


def _add_completion_report_section(
    command: RunOutputCommand,
    *,
    registry_root: Path,
    run_id: str,
    alias: str | None,
    sections: JsonObject,
    text_sections: dict[str, str],
) -> None:
    if not command.completion_report:
        return
    run_path = run_registry.run_directory(registry_root, run_id)
    report_path = run_path / run_registry.COMPLETION_REPORT_FILE
    if report_path.exists():
        text = report_path.read_text(encoding="utf-8", errors="replace")
        sections["completionReport"] = {"bytes": len(text.encode("utf-8"))}
        text_sections["completionReport"] = text
        return

    status = run_registry.effective_status(run_registry.load_run_state(registry_root, run_id))
    text = ""
    recovery_truncated = False
    recovery_attempted = False
    recovery_quality: harness_events.RecoveryQuality | None = None
    if status != run_registry.STATUS_RUNNING:
        manifest = run_registry.load_run_manifest(registry_root, run_id)
        harness = manifest.get("harness") if isinstance(manifest, dict) else None
        allow_last_assistant = harness in harness_events.ASSISTANT_RECOVERY_HARNESSES
        recovery_attempted = True
        text, recovery_truncated, recovery_quality = _recover_completion_report_from_stdout(
            registry_root,
            run_id,
            allow_last_assistant=allow_last_assistant,
        )
    if text:
        report_meta: JsonObject = {
            "bytes": len(text.encode("utf-8")),
            "source": run_registry.STDOUT_LOG,
            "synthetic": True,
            "tailLines": RECOVERY_STDOUT_TAIL_LINES,
            "tailBytes": RECOVERY_STDOUT_TAIL_BYTES,
            "truncated": recovery_truncated,
        }
        if recovery_quality is not None:
            report_meta["recoveryQuality"] = recovery_quality
        sections["completionReport"] = report_meta
        text_sections["completionReport"] = text
        return

    if command.default:
        _add_default_run_output_fallback(
            registry_root=registry_root,
            run_id=run_id,
            alias=alias,
            sections=sections,
            text_sections=text_sections,
            recovery_attempted=recovery_attempted,
            recovery_truncated=recovery_truncated,
            recovery_quality=recovery_quality,
        )
        return

    handle = alias or run_id
    diagnostics = _run_output_diagnostics(
        registry_root,
        run_id,
        recovery_attempted=recovery_attempted,
        recovery_truncated=recovery_truncated,
        recovery_quality=recovery_quality,
    )
    raise RunOutputError(
        "missing_completion_report",
        f"Completion report not found for run: {handle}",
        diagnostics=diagnostics,
        next_actions=_run_output_next_actions(handle),
    )


def _add_default_fallback_if_requested(
    command: RunOutputCommand,
    *,
    registry_root: Path,
    run_id: str,
    alias: str | None,
    sections: JsonObject,
    text_sections: dict[str, str],
) -> None:
    if not (command.default and not sections):
        return
    _add_default_run_output_fallback(
        registry_root=registry_root,
        run_id=run_id,
        alias=alias,
        sections=sections,
        text_sections=text_sections,
        recovery_attempted=False,
        recovery_truncated=False,
    )


def _redact_text_sections(
    text_sections: dict[str, str],
    *,
    no_redact: bool,
) -> dict[str, str]:
    if no_redact:
        return text_sections
    return {key: redaction.redact_string(text) for key, text in text_sections.items()}


def _merge_json_sections(sections: JsonObject, text_sections: dict[str, str]) -> JsonObject:
    merged_sections: JsonObject = {}
    for key, meta in sections.items():
        entry = dict(meta)
        if key in text_sections:
            entry["content"] = text_sections[key]
        merged_sections[key] = entry
    return merged_sections


def _emit_run_output_sections(
    command: RunOutputCommand,
    *,
    alias: str | None,
    run_id: str,
    sections: JsonObject,
    text_sections: dict[str, str],
    stdout: TextIO,
) -> None:
    if command.json_mode:
        payload = delegate_rendering.run_output_json_payload(
            alias=alias,
            run_id=run_id,
            sections=_merge_json_sections(sections, text_sections),
        )
        delegate_rendering.print_json(payload, stdout)
        return
    delegate_rendering.render_run_output_text(text_sections, stdout, section_meta=sections)


def emit(command: RunOutputCommand, *, workspace_path: str, stdout: TextIO) -> int:
    workspace = Path(workspace_path)
    registry_root = run_registry.registry_root_if_exists(workspace)
    if registry_root is None:
        registry_root = run_registry.registry_root(workspace)
    run_id, alias = command_errors.resolve_run_target(
        registry_root,
        handle=command.handle,
        latest_harness=None,
        error_cls=RunOutputError,
    )
    sections: JsonObject = {}
    text_sections: dict[str, str] = {}
    _add_completion_report_section(
        command,
        registry_root=registry_root,
        run_id=run_id,
        alias=alias,
        sections=sections,
        text_sections=text_sections,
    )
    _add_default_fallback_if_requested(
        command,
        registry_root=registry_root,
        run_id=run_id,
        alias=alias,
        sections=sections,
        text_sections=text_sections,
    )
    max_chars = _effective_max_chars(command)
    try:
        if command.stdout or command.raw:
            _add_log_output_section(
                registry_root=registry_root,
                run_id=run_id,
                log_name=run_registry.STDOUT_LOG,
                section_name="stdout",
                tail=command.tail,
                raw=command.raw,
                max_chars=None if command.raw else max_chars,
                sections=sections,
                text_sections=text_sections,
            )
        if command.stderr or command.raw:
            _add_log_output_section(
                registry_root=registry_root,
                run_id=run_id,
                log_name=run_registry.STDERR_LOG,
                section_name="stderr",
                tail=command.tail,
                raw=command.raw,
                max_chars=None if command.raw else max_chars,
                sections=sections,
                text_sections=text_sections,
            )
    except ValueError as exc:
        raise RunOutputError("missing_tail", str(exc)) from exc
    text_sections = _redact_text_sections(text_sections, no_redact=command.no_redact)
    _emit_run_output_sections(
        command,
        alias=alias,
        run_id=run_id,
        sections=sections,
        text_sections=text_sections,
        stdout=stdout,
    )
    return 0
