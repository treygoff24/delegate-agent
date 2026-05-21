from __future__ import annotations

from pathlib import Path


def tail_file_lines(path: Path, lines: int) -> str:
    if lines < 1:
        raise ValueError("tail lines must be at least 1")
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return ""
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
    return "\n".join(split[-lines:]) + ("\n" if split else "")


def read_log_output(path: Path, *, tail: int | None, raw: bool) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    if raw:
        return path.read_text(encoding="utf-8", errors="replace"), False
    if tail is None:
        raise ValueError("add --tail N or --raw to read stdout/stderr log output")
    return tail_file_lines(path, tail), True
