"""Step-key derivations are pinned so a change cannot ship without a key-version bump.

``workflow resume --repin`` moves a workflow onto newer code and trusts that a
journal's step keys still mean the same thing. Its only mechanical guarantee is
``WORKFLOW_KEY_VERSION``: resume refuses a workflow saved under a different
version. If one of these vectors changes, the derivation changed; bump
``WORKFLOW_KEY_VERSION`` (which retires the old journals on purpose) and update
the expected values here, never one without the other.
"""

from __future__ import annotations

import unittest

from delegate_agent.workflows import WORKFLOW_KEY_VERSION
from delegate_agent.workflows import runtime as workflow_runtime


class WorkflowKeyStabilityTests(unittest.TestCase):
    def test_key_version_and_derivations_move_together(self) -> None:
        self.assertEqual(
            WORKFLOW_KEY_VERSION,
            2,
            "WORKFLOW_KEY_VERSION changed: update the vectors below for the new derivation",
        )
        vectors = {
            "positional agent": (
                workflow_runtime._agent_key(
                    "root/seq#0",
                    "parity prompt",
                    {"engine": "codex", "mode": "safe", "timeout": None},
                ),
                "d2f121a75e5ffcc3b02c778085af2ca165fd0749d3ea78d7fa9c3279fc29b395",
            ),
            "positional followup": (
                workflow_runtime._followup_key(
                    "root/parallel@1/thunk#0/seq#0",
                    "impl",
                    "fix it",
                    {"schema": None, "timeout": 30},
                ),
                "b3220ec9e86ab35f6c85f3c6332a61838af2ccaa01c701b3978007034775de7f",
            ),
            "caller-keyed agent": (
                workflow_runtime._caller_agent_key("root", "impl-task-7"),
                "4f938fa1b63d209cc3aa06d4de5bd3659f98d06615cfaec0c69636341649f464",
            ),
            "caller-keyed agent retry": (
                workflow_runtime._caller_agent_key("root/parallel:batch", "impl-task-7", 2),
                "f91febc1b40b99e2e97ed0e4180a111bf1471de5fd9d62592a89a3fc4945ffce",
            ),
            "caller-keyed gate": (
                workflow_runtime._caller_gate_key("root", "task-7-park"),
                "46c8f03aeeb79860f2db62ccd1c7a3548ee794cd3b0a8730ed94b015eaa85c78",
            ),
        }
        for name, (actual, expected) in vectors.items():
            with self.subTest(name):
                self.assertEqual(
                    actual,
                    expected,
                    f"{name} key derivation changed: bump WORKFLOW_KEY_VERSION, then update "
                    "this vector; a repin would otherwise replay old journals under new keys",
                )


if __name__ == "__main__":
    unittest.main()
