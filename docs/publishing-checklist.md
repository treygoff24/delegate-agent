# Publishing checklist

Use this before making a public source release, GitHub release, or package artifact.

PyPI packaging is validated and live: `delegate-agent-cli` published 2026-07-06 (PyPI's name-similarity rule blocked the shorter `delegate-agent`; the installed console script is still `delegate`). Re-run the full checklist below for every subsequent release — publishing once does not exempt future versions from these checks.

## Required checks

- [ ] Start from a clean checkout: `git status --short --branch`.
- [ ] Confirm the public README links only public docs.
- [ ] Confirm `config.example.json` contains placeholders only.
- [ ] Confirm internal plans/prompts are excluded from package artifacts.
- [ ] Run the release private-path and private-alias scan across public docs,
      examples, config, metadata, and packaging rules. Treat any hit as either
      something to remove or something to explicitly justify before release.

- [ ] Run Python compile check:

  ```bash
  python3 -m compileall -q src tests bin
  ```

- [ ] Run tests:

  ```bash
  python3 -m pytest -q
  ```

- [ ] Run whitespace check:

  ```bash
  git diff --check
  ```

- [ ] Run the same lint and format checks as CI:

  ```bash
  ruff check .
  ruff format --check .
  ```

- [ ] Run Gitleaks and TruffleHog against both Git history and the exact source
      tree that will be packaged:

  ```bash
  gitleaks detect --source . --redact --no-banner
  trufflehog git file://. --only-verified --fail --fail-on-scan-errors \
    --exclude-detectors Lob --no-update
  LOB_SCAN_RC=0
  git grep -nIE '(^|[^[:alnum:]_])(live|test)_[a-f0-9]{35}([^[:alnum:]_]|$)' \
    $(git rev-list HEAD) || LOB_SCAN_RC=$?
  test "$LOB_SCAN_RC" -eq 1
  SCAN_ROOT="$(mktemp -d)"
  mkdir "$SCAN_ROOT/source"
  git archive HEAD | tar -x -C "$SCAN_ROOT/source"
  trufflehog filesystem "$SCAN_ROOT/source" --only-verified --fail \
    --fail-on-scan-errors --exclude-detectors Lob --no-update
  ```

  TruffleHog 3.96.0's Lob detector accepts ordinary 35-character Python
  `test_*` identifiers as verified credentials because its verification request
  treats every HTTP 422 response as valid. The direct Lob-key grep above uses
  the provider's lowercase hexadecimal key shape while the other detectors
  remain fail-closed.

## Package artifact checks

- [ ] Export `HEAD`, build it with tools installed only in a temporary virtual
      environment, and validate both artifacts:

  ```bash
  BUILD_ROOT="$(mktemp -d)"
  mkdir "$BUILD_ROOT/source"
  git archive HEAD | tar -x -C "$BUILD_ROOT/source"
  python3 -m venv "$BUILD_ROOT/build-venv"
  "$BUILD_ROOT/build-venv/bin/python" -m pip install --upgrade pip build twine
  (cd "$BUILD_ROOT/source" && "$BUILD_ROOT/build-venv/bin/python" -m build)
  "$BUILD_ROOT/build-venv/bin/python" -m twine check "$BUILD_ROOT"/source/dist/*
  ```

- [ ] Inspect sdist contents and confirm no internal plans, prompts, local run logs, or private config are included.
- [ ] Install the built wheel in a second temporary environment and run:

  ```bash
  python3 -m venv "$BUILD_ROOT/smoke-venv"
  "$BUILD_ROOT/smoke-venv/bin/python" -m pip install "$BUILD_ROOT"/source/dist/*.whl
  DELEGATE="$BUILD_ROOT/smoke-venv/bin/delegate"
  "$DELEGATE" --version
  "$DELEGATE" --json describe
  "$DELEGATE" --json dry-run codex safe "Review only."
  "$DELEGATE" --json dry-run claude safe "Review only."
  "$DELEGATE" --json dry-run grok safe "Review only."
  "$DELEGATE" --json dry-run opencode safe "Review only."
  "$DELEGATE" --json dry-run pi safe "Review only."
  "$DELEGATE" --json dry-run omp safe "Review only."
  "$DELEGATE" --json dry-run kimi safe "Review only."
  ```

## GitHub surface

- [ ] License is present.
- [ ] `CONTRIBUTING.md` and `SECURITY.md` are present.
- [ ] Issue and PR templates are present.
- [ ] GitHub Security Advisories are enabled for private vulnerability reports.
- [ ] CI passes without real child-agent binaries.

### Published history is filtered, not mirrored

GitHub `main` and the Forgejo branch `publish/main` carry the same commits with
489 raw audit artifacts (`docs/receipts/**` and the stale evidence files removed
in 0362a90 and 32d01e6) filtered out of every commit after
8e2b0d27aa3acc96b70e0b9e58c5737cf8671f2d. Forgejo `main` still holds those
files in its history, so its commits are not ancestors of GitHub `main`: a
plain `git push github main` is rejected as non-fast-forward, and that
rejection is correct. Never force it.

To publish a newer `main`:

```bash
git clone -q --no-local --branch main --single-branch <repo> /var/tmp/publish-clone
cd /var/tmp/publish-clone
uvx git-filter-repo --invert-paths --paths-from-file <paths.txt> \
  --refs 8e2b0d27aa3acc96b70e0b9e58c5737cf8671f2d..main
git fetch <repo> '+refs/remotes/github/*:refs/remotes/github/*'
git merge-base --is-ancestor github/main main   # must succeed
git log main --name-only --format= | grep -c -F -f <paths.txt>   # must be 0
git rev-parse main^{tree}                        # must equal the source tree
```

`<paths.txt>` is `git show --name-status --format= 0362a90 32d01e6 | awk '$1=="D"{print $2}' | sort -u`
(489 lines). Push the filtered `main` to GitHub `main` and to Forgejo
`publish/main`, then discard the clone. The filter is deterministic for the
same range and path list, so successive pushes stay fast-forward.

## Release decision

- [ ] Decide whether this release is source-only, GitHub release, PyPI, or another distribution path.
- [ ] If PyPI is chosen, confirm the release workflow still passes before publishing (PyPI itself is already validated as a distribution path as of the 2026-07-06 `delegate-agent-cli` publish).
- [ ] Tag only after docs, package metadata, and artifact checks agree.
