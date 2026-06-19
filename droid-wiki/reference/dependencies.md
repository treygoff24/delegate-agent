# Dependencies

Delegate keeps runtime dependencies small and delegates provider-specific work to external CLIs.

## Runtime Python dependencies

`pyproject.toml` declares `dependencies = []`. The runtime uses Python stdlib modules through the source files under `src/delegate_agent/`.

## Development dependencies

| Dependency | Purpose |
| --- | --- |
| `ruff==0.15.15` | Linting and formatting. |
| `build>=1.0` | Source and wheel distribution builds. |
| `twine>=5.0` | Distribution metadata checks. |

## External CLIs

| Tool | Used by | Notes |
| --- | --- | --- |
| `agent` | Cursor harness | Configured by `cursor.argvPrefix` in `config.example.json`. |
| `droid` | Factory Droid harness | Configured by `droid.binary` and `droid.models`. |
| `codex` | OpenAI Codex harness | Configured by `codex.binary`. |
| `claude` | Claude Code harness | Configured by `claude.binary`. |
| `kimi` | Kimi Code harness | Configured by `kimi.binary`. |
| `git` | Isolation and worktrees | Used by `src/delegate_agent/git_utils.py`, `src/delegate_agent/isolation.py`, and worktree modules. |

Dry-run and tests do not require these child binaries because tests use fake runtimes.

See [tooling](../how-to-contribute/tooling.md).
