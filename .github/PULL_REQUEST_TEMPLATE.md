## Summary

-

## Scope

- [ ] CLI behavior
- [ ] Config/model aliases
- [ ] Run registry / output
- [ ] Worktree isolation / cleanup
- [ ] Docs / packaging
- [ ] Tests only

## Verification

Commands run:

```bash
# paste checks here
```

## Safety notes

- [ ] Does not commit, push, merge, deploy, publish, or promote an installed runtime.
- [ ] Does not add secrets, private model IDs, local run logs, or machine-specific paths.
- [ ] Keeps `safe` / `work` behavior and isolation boundaries documented.

## Gate

- [ ] `python3 -m compileall -q src tests bin`
- [ ] `python3 -m pytest -q`
- [ ] `ruff check . && ruff format --check .`
- [ ] `README.md`, `docs/`, and the `CHANGELOG.md` Unreleased section updated for any behavior, flag, or config change
