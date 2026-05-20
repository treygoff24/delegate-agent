# Delegate Agent repository instructions

This repository contains the development copy of the `delegate` CLI.

Do not mutate a user's live machine runtime at `~/.delegate` or any installed `delegate` shim unless the user explicitly asks to install or promote a repository change. Other agents may be actively using that live runtime.

Use repo-local tests before proposing promotion:

```bash
python3 -m unittest discover -s tests
```
