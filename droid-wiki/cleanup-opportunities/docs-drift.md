# Docs drift

Docs drift risk is elevated because the repo shipped seven tags from 2026-06-05 through 2026-06-18 while documentation files were high-churn hotspots.

| File | Last 90 day commit touches | Drift risk |
| --- | ---: | --- |
| `README.md` | 24 | Main user-facing guide tracks runtime additions and safety model changes. |
| `docs/cli-reference.md` | 15 | CLI behavior changed across Codex, reasoning effort, Kimi, safe isolation, and Claude releases. |
| `docs/configuration.md` | 13 | Config changed for policy, reasoning, Kimi, and Claude support. |
| `CHANGELOG.md` | 14 | Release notes are active and should stay aligned with code and docs. |

Audit docs after each release tag against behavior in `src/delegate_agent/cli.py`, `src/delegate_agent/config.py`, and `src/delegate_agent/reasoning.py`.
