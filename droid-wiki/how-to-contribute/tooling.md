# Tooling

Tooling is intentionally simple: Python stdlib runtime, `unittest`, Ruff, build, twine, and Git.

## Python package config

`pyproject.toml` declares package name `delegate-agent`, Python `>=3.11`, console script `delegate = "delegate_agent.cli:main"`, no runtime dependencies, and optional dev dependencies.

## CI

`.github/workflows/ci.yml` runs on push and pull request with Python 3.11, 3.12, 3.13, and 3.14. The workflow runs compileall, unittest, Ruff, build, twine, and a wheel install smoke test.

## Lint and format

Ruff config lives in `pyproject.toml`. Run:

```bash
ruff check .
ruff format --check .
```

To apply formatting, run `ruff format .`.

See [dependencies](../reference/dependencies.md) for runtime and development dependencies.
