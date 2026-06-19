# Development workflow

Use the repo-local CLI while developing. The installed `delegate` command may point to a different version and should not be overwritten as a side effect of normal work.

## Local loop

```bash
python3 bin/delegate.py --json describe
python3 bin/delegate.py --json dry-run codex safe "Review only."
python3 -m unittest discover -s tests
```

For deterministic config output:

```bash
clean_home="$(mktemp -d)"
HOME="$clean_home" DELEGATE_CONFIG="$PWD/config.example.json" python3 bin/delegate.py --json models
```

## Recommended checks

```bash
python3 -m compileall -q src tests bin
git diff --check
python3 -m unittest discover -s tests
ruff check .
ruff format --check .
```

Packaging changes should also run `python3 -m build --sdist --wheel` and `twine check dist/*`. CI config lives in `.github/workflows/ci.yml`.

See [tooling](tooling.md) and [testing](testing.md).
