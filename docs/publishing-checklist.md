# Publishing checklist

Use this before making a public source release, GitHub release, or package artifact. The project is currently documented as GitHub-source install; do not advertise PyPI until the packaging flow is complete.

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
  python3 -m unittest discover -s tests
  ```

- [ ] Run whitespace check:

  ```bash
  git diff --check
  ```

- [ ] Run secret scanning, for example Gitleaks and/or TruffleHog, against the repository history you plan to publish.

## Package artifact checks

- [ ] Build from a clean tree:

  ```bash
  python3 -m pip install --upgrade build twine
  python3 -m build
  python3 -m twine check dist/*
  ```

- [ ] Inspect sdist contents and confirm no internal plans, prompts, local run logs, or private config are included.
- [ ] Install the built wheel in a temporary environment and run:

  ```bash
  delegate --json describe
  delegate --json dry-run codex safe "Review only."
  ```

## GitHub surface

- [ ] License is present.
- [ ] `CONTRIBUTING.md` and `SECURITY.md` are present.
- [ ] Issue and PR templates are present.
- [ ] GitHub Security Advisories are enabled for private vulnerability reports.
- [ ] CI passes without real child-agent binaries.

## Release decision

- [ ] Decide whether this release is source-only, GitHub release, PyPI, or another distribution path.
- [ ] If PyPI is chosen, add and test the release workflow before advertising PyPI installation.
- [ ] Tag only after docs, package metadata, and artifact checks agree.
