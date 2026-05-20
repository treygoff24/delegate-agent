# Live runtime separation

This repository can be used as a development checkout while an already-installed
`delegate` command continues to run from a separate local runtime:

- shim: `~/.local/bin/delegate`
- runtime implementation: `~/.delegate/bin/delegate.py`
- runtime config: `~/.delegate/config.json`

Keep those paths unchanged while doing development here. Promote changes to a
live runtime only through an explicit install/update step after review and tests.
