"""Cross-cutting CLI mode constants.

A dependency leaf shared by the parser, request builder, and execution layers
so they agree on the safe/work mode vocabulary without importing ``cli``.
"""

from __future__ import annotations

MODE_SAFE = "safe"
MODE_WORK = "work"
VALID_MODES = {MODE_SAFE, MODE_WORK}
