from __future__ import annotations

WORKSPACE_FLAG_BY_ENGINE: dict[str, str | None] = {
    "cursor": "--workspace",
    "codex": "--cd",
    "droid": "--cwd",
    "kimi": None,
}


def replace_argv_after_flag(argv: list[str], flag: str, value: str) -> list[str]:
    updated = list(argv)
    for index, token in enumerate(updated):
        if token == flag and index + 1 < len(updated):
            updated[index + 1] = value
            return updated
    return updated


def replace_workspace_arg_in_argv(engine: str, argv: list[str], value: str) -> list[str]:
    flag = WORKSPACE_FLAG_BY_ENGINE.get(engine)
    if flag is None:
        return list(argv)
    return replace_argv_after_flag(argv, flag, value)
