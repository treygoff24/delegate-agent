# Security

Delegate Agent can launch powerful agent runtimes. Treat `work` mode as privileged execution in the target repository.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for this repository. Do not include secrets, API keys, private logs, or unreleased vulnerability details in public issues.

## Secret handling

- Never commit provider API keys, access tokens, private keys, `.env` files, or local runtime configs.
- Use `config.example.json` for documentation and keep real config in `~/.delegate/config.json` or another ignored local path.
- Run a secret scan before publishing commits.
