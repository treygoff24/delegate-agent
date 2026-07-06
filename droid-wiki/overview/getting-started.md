# Getting started

Use the checkout-local entry point when working in this repository. Do not overwrite an installed `delegate` shim or a live `~/.delegate/config.json` unless the operator explicitly asks for promotion.

## Prerequisites

Delegate requires Python 3.11 or newer. The package has no runtime Python dependencies, but development checks use the optional `dev` group from `pyproject.toml`. Install child runtimes only if you plan to run them for real: `agent`, `droid`, `codex`, `claude`, `grok`, or `kimi`.

Dry-runs and unit tests do not require the real child binaries.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python3 bin/delegate.py --json describe
python3 -m unittest discover -s tests
```

`CONTRIBUTING.md` and `AGENTS.md` both prefer `python3 bin/delegate.py` while developing in this checkout.

## Basic commands

```bash
python3 bin/delegate.py --json dry-run codex safe "Review this repository. Do not edit files."
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json models
python3 bin/delegate.py --json capabilities
python3 bin/delegate.py --json help
```

Run a tracked child task only when the target child CLI is installed and authenticated:

```bash
python3 bin/delegate.py codex safe "Review this repository for correctness risks. Do not edit files."
```

## Configuration

Copy `config.example.json` to a private location before real Droid runs because public examples contain placeholder model IDs:

```bash
mkdir -p ~/.delegate
cp config.example.json ~/.delegate/config.json
$EDITOR ~/.delegate/config.json
```

For deterministic local development independent of user config, use a temporary home and explicit config:

```bash
clean_home="$(mktemp -d)"
HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json describe
```

## Validation commands

```bash
python3 -m compileall -q src tests bin
git diff --check
python3 -m unittest discover -s tests
ruff check .
ruff format --check .
```

Packaging changes should also run `python3 -m build --sdist --wheel` and `twine check dist/*`. The CI workflow in `.github/workflows/ci.yml` runs compileall, unittest, Ruff, build, twine, and a clean wheel install smoke test on Python 3.11 through 3.14.

See [testing](../how-to-contribute/testing.md) and [tooling](../how-to-contribute/tooling.md).
