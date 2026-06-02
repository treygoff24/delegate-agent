# Security policy

Delegate Agent can launch powerful local agent runtimes. Treat `work` mode as privileged execution in the target repository.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for this repository. Do not include secrets, API keys, private logs, or unreleased vulnerability details in public issues.

## Supported versions

This project is in alpha. Security fixes are expected to land on the default branch first. If release artifacts are introduced later, this file should be updated with supported version ranges.

## Security boundaries

Delegate is a launcher and run recorder. It is not a full sandbox.

- `safe` mode is intended for read-only review/investigation, but child runtimes may still have access to files, credentials, tools, and network capabilities allowed by their own configuration.
- Cursor safe and Codex safe use temporary workspace isolation by default.
- Droid safe runs in the real workspace using Droid defaults.
- `work` mode is edit-capable.
- Persistent worktree isolation protects the source checkout from ordinary relative-path edits, but it does not block absolute-path writes, credential use, network use, or external tool side effects.

See [docs/security-model.md](docs/security-model.md) for details.

## Secret handling

- Never commit provider API keys, access tokens, private keys, `.env` files, local runtime configs, `.delegate/` run logs, or private model IDs.
- Use `config.example.json` for documentation and keep real config in `~/.delegate/config.json`, ignored workspace-local config, or another private path referenced by `DELEGATE_CONFIG`.
- Run secret and private-path scans before publishing commits or artifacts.
- Delegate redacts common credential shapes when rendering snapshots and `run-output` by default, but local raw logs and child runtime state can still contain secrets.
- Avoid placing secrets in prompts; tracked run output can be inspected later and may be archived locally.
