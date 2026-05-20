# Contributing

Thanks for your interest in Delegate Agent. This project is early, so small, well-scoped changes are easiest to review.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python3 -m unittest discover -s tests
```

## Development guidelines

- Keep CLI behavior explicit and predictable.
- Add or update tests for parser, validation, and execution-shape changes.
- Do not add commands that commit, push, merge, deploy, or publish repositories from Delegate Agent itself.
- Do not commit local runtime state, provider credentials, API keys, or machine-specific logs.
- Preserve clear separation between this development checkout and any user's installed live runtime.

## Reporting issues

When filing an issue, include:

- the command you ran,
- the expected behavior,
- the actual behavior,
- sanitized config/model aliases if relevant,
- whether the issue affects `safe`, `work`, or both.
