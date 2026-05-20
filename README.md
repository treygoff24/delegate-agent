# Delegate Agent

Delegate Agent is a small local CLI for handing bounded tasks to agent runtimes such as Cursor Agent and Factory Droid. It gives operators a consistent command shape for read-only analysis (`safe`) and file-editing execution (`work`) while keeping prompts scoped and auditable.

> Status: early development. The CLI is useful today, but the public API and installation workflow may change before a stable release.

## Why this exists

Agent runtimes have different flags, model names, and safety postures. Delegate Agent wraps those differences so a human or orchestrating agent can say:

```bash
delegate cursor safe "Analyze this workspace and report findings only."
delegate cursor work "Implement the scoped fix, run the named check, and report changed files."

delegate droid minimax safe "Investigate this issue. Do not edit files."
delegate droid minimax work "Implement this bounded change and run the specified tests."
```

The CLI does **not** commit, push, merge, deploy, run a daemon, or create background jobs. It launches the selected runtime with a bounded prompt and returns the child command's result.

## Safety model

Delegate Agent has two modes:

| Mode | Intent | Cursor flags | Droid flags |
| --- | --- | --- | --- |
| `safe` | Read-only review/analysis | `-p --trust` (no plan/ask/force/MCP auto-approve) | default read-only (no unsafe skip) |
| `work` | File-writing execution in a trusted workspace | `-p --trust --approve-mcps --force` | `--skip-permissions-unsafe` |

### Cursor safe

Optimized for code review and investigation. Uses **default Cursor Agent**, not `--mode=plan` or `--mode=ask`.

**Hard boundary — workspace isolation**

- Delegate creates a temporary isolated copy (detached git worktree or directory copy) and passes that path as `--workspace`.
- The original resolved workspace is not modified.
- Isolation is the guarantee that protects your tree; everything below is defense-in-depth inside the copy only.

**Defense-in-depth (isolated copy only)**

- Prepends read-only review instructions to the prompt.
- Writes `.cursor/cli.json` in the isolated workspace: allow `Read(**)` and read-oriented shell helpers (`rg`, `grep`, `cat`, etc.); deny `Write(**)`, destructive shell, and reads of common secret paths.

**Excluded Cursor flags:** `--mode=plan`, `--mode=ask`, `--force`, `--approve-mcps`.

### Cursor work

Runs in the real workspace with `--approve-mcps --force`. Review diffs afterward in Git workspaces; outside Git, manually inspect changed files.

### Droid safe

Runs in the real workspace on Droid's default read-only posture. Delegate does not pass `--auto`, `--use-spec`, or `--skip-permissions-unsafe`, so review prompts are not pushed into Droid's implementation-spec workflow.

### Droid work

Uses `--skip-permissions-unsafe` in the real workspace. Treat as intentionally powerful; use only for bounded tasks you trust.

## Installation for development

From a checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
delegate --json describe
```

You can also run the checkout without installing:

```bash
python3 bin/delegate.py --json describe
PYTHONPATH=src python3 -m delegate_agent.cli --json describe
```

## Configuration

By default, the CLI reads:

```bash
~/.delegate/config.json
```

Override this with:

```bash
DELEGATE_CONFIG=/path/to/config.json delegate --json models
```

Start from [`config.example.json`](config.example.json). Do not commit machine-local `config.json`, `cursor-prereq.json`, API keys, or provider credentials.

## Commands

```bash
delegate [--cwd PATH] [--json] cursor {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] droid MODEL_ALIAS {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] dry-run cursor {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] dry-run droid MODEL_ALIAS {safe,work} [--prompt-file PATH] [prompt...]
delegate [--cwd PATH] [--json] run --input-json FILE
delegate [--json] models
delegate [--json] describe
delegate agent-help
```

Global flags must come before the subcommand:

```bash
delegate --json --cwd /path/to/workspace dry-run droid minimax work "hello"
```

Trailing global flags such as `delegate dry-run ... --json` are rejected. Use `--prompt-file` or `run --input-json` when prompt text needs literal flag-looking tokens.

`--cwd` accepts either a Git checkout or an ordinary directory. If the directory is inside a Git repository, Delegate Agent normalizes it to the repository root. If no Git repository is present, Delegate Agent uses the directory directly, which supports document, policy, and shared-folder workspaces.

## JSON input

```json
{
  "engine": "cursor",
  "mode": "work",
  "cwd": "/path/to/workspace",
  "prompt": "Implement the scoped task. Verify with tests. Report changed files."
}
```

For Droid, include a model alias:

```json
{
  "engine": "droid",
  "model": "minimax",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Investigate this issue. Do not edit files."
}
```

Unknown JSON keys are rejected. See [`examples/`](examples/) for starting points.

With `--json`, `cursor safe` reports the original workspace as `cwd`, the temporary isolated copy as `executionCwd`, and `isolatedWorkspace: true`. Other engines and modes keep `cwd` as the execution directory.

## Development

Run tests from the repository root:

```bash
python3 -m unittest discover -s tests
```

Before publishing changes, also run a secret scan and inspect for machine-local paths or credentials.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | success |
| 2 | usage or validation error |
| 3 | missing dependency/binary |
| child code | Cursor/Droid launched but failed |

## Contributing

Contributions are welcome once this project is public. Please keep changes small, include tests for CLI behavior, and preserve the core invariant: Delegate Agent should launch bounded tasks, not perform repository publishing or deployment itself.

## License

MIT. See [`LICENSE`](LICENSE).
