# Live runtime separation

This repository can be used as a development checkout while an already-installed
`delegate` command continues to run from a separate local runtime:

- shim: `~/.local/bin/delegate`
- runtime implementation: `~/.delegate/bin/delegate.py`
- runtime config: `~/.delegate/config.json`

Keep those paths unchanged while doing development here. Promote changes to a
live runtime only through an explicit install/update step after review and tests.

The development checkout may add workspace-local `.delegate/` registries, bounded
default output, `snapshot` / `runs` / `run-output` commands, and archive-only
retention. None of that affects the live runtime until promotion. Orchestrating
agents should launch Delegate normally and use `delegate snapshot` plus related
commands instead of piping launches through `tail` or tailing raw log files under
`.delegate/runs/`.
