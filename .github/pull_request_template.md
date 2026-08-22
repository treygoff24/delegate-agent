## What changed

## Why

## Gate

- [ ] `python3 -m compileall -q src tests bin`
- [ ] `python3 -m unittest discover -s tests -t .`
- [ ] `ruff check . && ruff format --check .`
- [ ] Docs updated (`README.md`, `docs/`, `CHANGELOG.md` Unreleased) for any behavior, flag, or config change
- [ ] No local runtime state, credentials, private paths, or private model aliases in the diff
