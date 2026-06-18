# Contributing

Thanks for your interest in Delegate Agent. The project is early, so small, well-scoped changes are easiest to review.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python3 bin/delegate.py --json describe
python3 -m unittest discover -s tests
```

Use `python3 bin/delegate.py` from this repository when validating development changes. Do not overwrite an installed `delegate` shim, `~/.delegate/config.json`, or any live runtime unless an operator explicitly asks for promotion.

## Supported platforms

Required CI currently runs on Linux for Python 3.11, 3.12, 3.13, and 3.14. Contributions for macOS or Windows compatibility are welcome, but do not claim support for a platform until tests cover it.

## Development guidelines

- Keep CLI behavior explicit and predictable.
- Add or update tests for parser, validation, execution-shape, run-registry, and worktree-management changes.
- Do not add commands that commit, push, merge, deploy, or publish repositories from Delegate Agent itself.
- Keep `safe` versus `work` mode boundaries clear in code and docs.
- Treat worktree isolation as checkout isolation, not a complete security sandbox.
- Do not commit local runtime state, provider credentials, API keys, machine-specific logs, `.delegate/` run state, or private model aliases.
- Keep public examples provider-neutral. Prefer local alias names such as `reviewer` and `implementer`.

## Verification

Run the narrowest useful checks for your change, then the full suite before proposing a release or broad merge:

```bash
python3 -m compileall -q src tests bin
git diff --check
python3 -m unittest discover -s tests
ruff check .
ruff format --check .
```

`ruff` ships in the `dev` optional-dependencies group; run `ruff format .` to apply formatting. Required CI does not need real Cursor, Droid, Codex, Claude, or Kimi binaries.

## Reporting issues

When filing an issue, include:

- the command you ran,
- whether you used installed `delegate` or `python3 bin/delegate.py`,
- sanitized config/model aliases if relevant,
- expected behavior,
- actual behavior,
- whether the issue affects `safe`, `work`, or both,
- whether the workspace was Git, non-Git, temporary isolation, or persistent worktree isolation.
