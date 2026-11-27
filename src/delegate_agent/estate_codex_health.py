"""Report definite vendor refresh rejection to estate-harness, without a probe."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from delegate_agent.json_types import JsonObject

# A tracked estate-harness launch prints this generation immediately before
# exec. It names the actual broker pointer, not Delegate's configured CODEX_HOME.
_GENERATION = re.compile(
    r"^estate-harness: codex-credential-generation (work|personal) "
    r"([a-z0-9][a-z0-9-]{0,63}) ((?:[0-9]+:){4}[0-9]+)$",
    re.MULTILINE,
)
_REJECTIONS = ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated")
_REJECTION_MESSAGE = re.compile(
    r"access token could not be refreshed because your refresh token "
    r"(?:has expired|was already used|was revoked)\b",
    re.IGNORECASE,
)


def observe_rejection(
    final_stderr: str, diagnostic_text: str, provider_error: JsonObject | None
) -> bool:
    """Write only evidence tied to the same auth.json generation that launched."""
    code = (provider_error or {}).get("providerCode")
    if code not in _REJECTIONS and not _REJECTION_MESSAGE.search(diagnostic_text):
        return False
    hits = _GENERATION.findall(final_stderr)
    if len(hits) != 1:
        return False
    realm, label, stamp = hits[0]
    if not label.startswith(f"{realm}-"):
        return False
    override = os.environ.get("ESTATE_AI_PROFILES_ROOT") or os.environ.get("AI_PROFILES_ROOT")
    root = Path(override).expanduser() if override else Path.home() / ".ai-profiles"
    pool = root.resolve() / "accounts" / "codex" / realm
    temp_path = None
    try:
        account = (pool / label).resolve()
        if not account.is_relative_to(pool):
            return False
        info = (account / "auth.json").stat()
        generation = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size]
        if ":".join(map(str, generation)) != stamp:
            return False  # Rotated or replaced since the process started.
        fd, temp_path = tempfile.mkstemp(dir=account, prefix=".estate-refresh.")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"generation": generation, "observed_at": time.time()}, stream)
        os.replace(temp_path, account / ".estate-refresh-rejected.json")
        temp_path = None
    except (OSError, RuntimeError):
        return False
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
    return True
