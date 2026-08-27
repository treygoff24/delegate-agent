"""Shared filesystem fixtures for mail tests.

Mail tests deliberately keep their registry outside the checkout so a broken
repo-relative path cannot make the assertion pass accidentally.  The old
``Path(__file__).parents[3]`` trick depended on checkout depth and resolved to
``/`` in a shallow worktree.  This helper chooses one explicit base directory,
then verifies the resulting temporary directory is outside both the checkout
and Git's common directory.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_common_dir(repository_root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        check=True,
        text=True,
    )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = repository_root / common
    return common.resolve()


def _outside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return True
    return False


def _valid_base(candidate: str | None) -> Path | None:
    if not candidate:
        return None
    base = Path(candidate).expanduser()
    try:
        resolved = base.resolve()
    except OSError:
        return None
    if not resolved.is_dir() or not os.access(resolved, os.W_OK):
        return None
    return resolved


def mail_temporary_directory(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """Return a temporary directory outside this checkout and its Git common dir.

    ``DELEGATE_TEST_TMPDIR`` is used only when it names an existing writable
    directory.  Invalid overrides are ignored in favor of the system temp
    directory; there is intentionally no ancestor-writability search.
    """

    repository_root = _repository_root()
    common_dir = _git_common_dir(repository_root)
    candidates = [
        _valid_base(os.environ.get("DELEGATE_TEST_TMPDIR")),
        _valid_base(tempfile.gettempdir()),
    ]
    for base in candidates:
        if base is None:
            continue
        try:
            temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=str(base))
        except OSError:
            continue
        chosen = Path(temporary.name).resolve()
        if _outside(chosen, repository_root) and _outside(chosen, common_dir):
            return temporary
        temporary.cleanup()
    raise RuntimeError(
        "could not allocate a mail-test tempdir outside the repository and Git common directory"
    )
