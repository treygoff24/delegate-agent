# Testing

The test suite uses Python `unittest` and fake child runtimes. Required CI does not need real Cursor, Droid, Codex, Claude, Grok, Devin, OpenCode, or Kimi binaries.

## Test layout

| Test file | Focus |
| --- | --- |
| `tests/test_delegate_parser.py` | CLI parsing and command shape validation. |
| `tests/test_delegate_commands.py` | Command construction and output payloads. |
| `tests/test_delegate_execution.py` | Child execution behavior and tracked output. |
| `tests/test_delegate_validation.py` | Config and request validation. |
| `tests/test_delegate_isolation.py` | Isolation helpers and safe workspace behavior. |
| `tests/test_delegate_worktree_mgmt.py` | Persistent worktree lifecycle commands. |
| `tests/test_run_registry.py` | Run registry indexing, aliases, and status helpers. |
| `tests/test_snapshot_commands.py` | Snapshot command behavior. |
| `tests/test_harness_events.py` | Stream event parsing and completion extraction. |
| `tests/test_reasoning_capabilities.py` | Reasoning-effort validation and capability reporting. |
| `tests/test_command_help.py` | Text and JSON help contracts. |

Run a focused file while iterating, then run `python3 -m unittest discover -s tests -t .` before handoff. See [tracked execution](../systems/tracked-execution.md) for the runtime capture model that many tests exercise.
