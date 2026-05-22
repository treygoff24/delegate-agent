# Publishing checklist

Before publishing this repository publicly:

- [x] Add an open-source license.
- [x] Confirm project name and package name.
- [x] Document simple install and first-run usage in `README.md`.
- [x] Keep `config.example.json` limited to safe placeholder values.
- [ ] Run the test suite.
- [ ] Run Gitleaks and TruffleHog.
- [ ] Run a path/private-artifact scan.
- [ ] Build sdist/wheel from a clean archive and run `twine check`.
- [ ] Confirm GitHub Security Advisories are enabled.
- [ ] Decide whether the first launch is source-only, GitHub release, PyPI, or all three.
