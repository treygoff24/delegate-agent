# Publishing checklist

Before publishing this repository publicly:

- [ ] Choose and add an open-source license.
- [ ] Confirm project name and package name.
- [ ] Run the test suite.
- [ ] Run a dedicated secret scanner such as Gitleaks or TruffleHog.
- [ ] Scan for private local paths, emails, logs, and runtime artifacts.
- [ ] Review `config.example.json` for only safe placeholder values.
- [ ] Decide whether to publish packages, GitHub releases, or source only.
- [ ] Document the installation/promote workflow for replacing a live runtime.
