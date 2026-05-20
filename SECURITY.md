# Security

Delegate Agent can launch powerful agent runtimes. Treat `work` mode as privileged execution in the target repository.

## Reporting a vulnerability

If this repository is public, please report security issues privately through the repository's security advisory flow or the maintainer contact listed by the project. Do not include secrets, API keys, or private logs in public issues.

## Secret handling

- Never commit provider API keys, access tokens, private keys, `.env` files, or local runtime configs.
- Use `config.example.json` for documentation and keep real config in `~/.delegate/config.json` or another ignored local path.
- Run a secret scan before publishing commits.
