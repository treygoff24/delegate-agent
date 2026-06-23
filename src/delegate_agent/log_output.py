from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RUN_OUTPUT_DEFAULT_MAX_CHARS = 60_000


@dataclass(frozen=True)
class LogOutput:
    content: str
    truncated: bool


@dataclass(frozen=True)
class CharCapResult:
    content: str
    char_truncated: bool
    returned_chars: int
    omitted_chars: int


def cap_content_by_chars(content: str, max_chars: int) -> CharCapResult:
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if len(content) <= max_chars:
        return CharCapResult(
            content=content,
            char_truncated=False,
            returned_chars=len(content),
            omitted_chars=0,
        )
    omitted = len(content) - max_chars
    return CharCapResult(
        content=content[-max_chars:],
        char_truncated=True,
        returned_chars=max_chars,
        omitted_chars=omitted,
    )


def tail_file_output(path: Path, lines: int) -> LogOutput:
    if lines < 1:
        raise ValueError("tail lines must be at least 1")
    if not path.exists():
        return LogOutput(content="", truncated=False)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return LogOutput(content="", truncated=False)
        block = 4096
        chunks: list[bytes] = []
        position = end
        newline_count = 0
        while position > 0 and newline_count <= lines:
            read_size = min(block, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.insert(0, chunk)
            newline_count += chunk.count(b"\n")
        text = b"".join(chunks).decode("utf-8", errors="replace")
    split = text.splitlines()
    content = "\n".join(split[-lines:]) + ("\n" if split else "")
    return LogOutput(content=content, truncated=position > 0 or len(split) > lines)


def read_log_output(path: Path, *, tail: int | None, raw: bool) -> LogOutput:
    if not path.exists():
        return LogOutput(content="", truncated=False)
    if raw:
        return LogOutput(
            content=path.read_text(encoding="utf-8", errors="replace"), truncated=False
        )
    if tail is None:
        raise ValueError("add --tail N or --raw to read stdout/stderr log output")
    return tail_file_output(path, tail)
