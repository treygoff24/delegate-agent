<p align="center">
  <img src="docs/assets/delegate-agent-header.png" alt="Delegate Agent" width="100%">
</p>

# Delegate Agent

Delegate Agent is a small command-line tool for handing a scoped task to another coding agent.

Use it when you want to say: "Cursor, review this diff", "Codex, investigate this bug in a sandbox", or "Droid, make this bounded change", without remembering each tool's flags every time.

Delegate does not commit, push, merge, deploy, or run a daemon. It builds the right command, adds a little safety framing, launches the agent, and records a local run log you can inspect later.

## Why you might want it

Agent CLIs are powerful, but their interfaces do not match. Cursor, Droid, and Codex use different flags for workspaces, models, output modes, safety settings, and non-interactive runs.

Delegate gives them one shape. For Droid, `my-model` means an alias you added to your config:

```bash
delegate cursor safe "Review this repo and report risks. Do not edit."
delegate codex safe "Investigate this bug in an isolated copy."
delegate droid my-model work "Implement the small fix and run the named test."
```

It is most useful if you already use coding agents and want a predictable way to send them small jobs from your terminal, scripts, or another agent.

## Install

Delegate is a Python package. It requires Python 3.11 or newer.

For normal use from the public repo:

```bash
python3 -m pip install git+https://github.com/treygoff24/delegate-agent.git
```

For local development:

```bash
git clone https://github.com/treygoff24/delegate-agent.git
cd delegate-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

You can also run the checkout without installing it:

```bash
python3 bin/delegate.py --json describe
```

## Prerequisites

Delegate wraps other tools. Install and authenticate the runtimes you plan to use:

```bash
agent --help    # Cursor Agent CLI, for delegate cursor ...
droid --help    # Factory Droid CLI, for delegate droid ...
codex --help    # OpenAI Codex CLI, for delegate codex ...
```

You do not need all three. If you only use Codex, only Codex needs to be installed.

After installing Delegate, check what it sees:

```bash
delegate --json describe
delegate --json models
```

If a runtime is missing, Delegate exits with code `3` and tells you which binary it could not find.

## Quick start

Run a read-only review with Codex in an isolated temporary copy:

```bash
delegate codex safe "Read this project and tell me what looks risky. Do not edit files."
```

Run a read-only review with Cursor in an isolated temporary copy:

```bash
delegate cursor safe "Review the current diff for bugs and regressions."
```

Run a Droid investigation after adding a real model alias to `droid.models`:

```bash
delegate droid my-model safe "Investigate this failure and report likely causes. Do not edit."
```

Run a bounded edit when you trust the workspace:

```bash
delegate cursor work "Fix the parser bug. Run python3 -m unittest tests.test_delegate_parser. Report changed files."
```

Normal runs return a short summary with an alias:

```text
delegate run cursor completed in 1m12s
alias: cursor
status: succeeded
snapshot: delegate snapshot cursor
completion report: delegate run-output cursor --completion-report
```

Use the alias to inspect the run:

```bash
delegate snapshot cursor
delegate run-output cursor --completion-report
delegate run-output cursor --stderr --tail 100
```

## Safe mode and work mode

Delegate has two modes.

`safe` is for review and investigation. Cursor safe and Codex safe run against a temporary isolated workspace, not your source tree. Droid safe uses Droid's default read-only behavior in the real workspace.

`work` is for file edits. It runs in the real workspace and uses the agent runtime's edit-capable flags. Treat it like giving another developer access to your checkout. Keep the prompt narrow and review the diff afterward.

| Command | Where it runs | What it can do |
| --- | --- | --- |
| `delegate cursor safe` | isolated temporary copy | read-only review intent |
| `delegate codex safe` | isolated temporary copy plus Codex read-only sandbox | read-only review intent |
| `delegate droid MODEL safe` | real workspace | Droid default read-only posture |
| `delegate cursor work` | real workspace | can edit files |
| `delegate codex work` | real workspace with Codex workspace-write sandbox | can edit files |
| `delegate droid MODEL work` | real workspace | can edit files |

Delegate may write its own local run metadata under `.delegate/` in the source workspace for tracked runs. That metadata is ignored by Git. The child agent in Cursor safe or Codex safe still receives the isolated copy, not your source tree.

## Configuration

Delegate reads config from:

```bash
~/.delegate/config.json
```

You can point at another file for one command:

```bash
DELEGATE_CONFIG=/path/to/config.json delegate --json models
```

Start from `config.example.json`. The most common things to configure are:

```json
{
  "cursor": {
    "argvPrefix": ["agent"],
    "defaultModel": "composer-2.5"
  },
  "droid": {
    "binary": "droid",
    "models": {
      "my-model": "your-droid-model-id"
    }
  },
  "codex": {
    "binary": "codex",
    "defaultModel": null
  }
}
```

Codex model selection is optional. If `codex.defaultModel` is `null`, Delegate lets the Codex CLI choose its default.

Droid needs model aliases because Droid model IDs are usually long. Your aliases can be whatever you want. Replace the placeholder in `config.example.json` before running Droid commands.

## JSON input

For longer jobs, put the request in a file:

```json
{
  "engine": "codex",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Review this project for release blockers. Do not edit files."
}
```

Then run:

```bash
delegate run --input-json task.json
```

For Droid, include `model`:

```json
{
  "engine": "droid",
  "model": "my-model",
  "mode": "safe",
  "cwd": "/path/to/workspace",
  "prompt": "Investigate the failing test. Do not edit files."
}
```

See `examples/` for starter files.

## Output and logs

By default, Delegate keeps parent-facing output short. Raw runtime output is stored in the workspace-local `.delegate/` registry.

Use these commands instead of tailing log files directly:

```bash
delegate runs --active
delegate runs --recent
delegate snapshot <alias-or-run-id>
delegate run-output <alias-or-run-id> --completion-report
delegate run-output <alias-or-run-id> --stdout --tail 100
```

Raw stdout and stderr are redacted by default when printed through Delegate. Use `--no-redact` only when you really need exact output and you are sure it is safe to display.

If you need the old behavior where the child runtime streams directly to your terminal, use:

```bash
delegate --pass-through cursor safe "Review this repo."
```

`--pass-through` is incompatible with `--json`.

## Development

Run the test suite from the repo root:

```bash
python3 -m unittest discover -s tests
```

Useful local checks before a release:

```bash
python3 -m unittest discover -s tests
gitleaks detect --source . --redact
trufflehog filesystem --no-update --only-verified .
```

Build smoke:

```bash
python3 -m pip install build twine
python3 -m build --sdist --wheel
python3 -m twine check dist/*
```

Do not copy this checkout into `~/.delegate` or overwrite an installed `delegate` shim unless you explicitly mean to promote a local build. Other agents may be using the installed runtime.

## Security

Delegate can launch tools that edit files and run commands. Use `work` mode only in repositories you trust.

Never commit provider tokens, API keys, `.env` files, local run logs, or machine-specific config. Report security issues through GitHub Security Advisories for this repository.

## Contributing

Small, well-tested changes are welcome. Please include tests for parser, command-building, execution, or registry behavior when you change those paths.

See `CONTRIBUTING.md` and `SECURITY.md`.

## License

MIT. See `LICENSE`.
