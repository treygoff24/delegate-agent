# Live smoke record — 2026-09-07 overnight run

Every live harness call made during the build, by lane, with the cheapest model available. Costs are
the harness's own accounting where reported. Nothing here was run against devin, droid, kimi, or
opencode (not installed on this cell); those engines are covered by unit tests against vendor-documented
shapes only.

| # | Lane | Command (abridged) | Result |
| --- | --- | --- | --- |
| 1 | audit-claude | `claude -p --output-format json --model haiku …` ×2 (capture result-object shape) | ok, ~$0.01 each |
| 2 | audit-grok | `grok --output-format streaming-json --prompt-file …` (capture multi-response stream) | ok, $0.0137 |
| 3 | audit-cursor | `cursor-agent -p --mode ask …` ×3 (init event, tool_call payload, stdin proof) | ok |
| 4 | E | `cursor-agent -p --trust --mode ask --model composer-2.5 --output-format stream-json` reading one file → `tests/fixtures/cursor/tool_read.jsonl` | exit 0, 13 events |
| 5 | E | `claude -p --output-format stream-json --model haiku --json-schema {ok:boolean}` → `tests/fixtures/claude/structured_output.jsonl` | exit 0, $0.0676 (cold cache) |
| 6 | E | `grok --output-format streaming-json --prompt-file …` reading one file → `tests/fixtures/grok/tool_read_multi_response.jsonl` | exit 0, $0.0123 |
| 7 | S | workflow `wf_f89012be281b`: array-root stage on claude (prompt path) + object-root stage (native) | `{"array":["lane-s"],"object":{"ok":true}}` |
| 8 | S | `delegate claude call --read-only --model haiku --output-schema <object>` | parsed `{"ok": true}` |
| 9 | A | `printf … \| cursor-agent -p --trust --mode ask --model composer-2.5 --output-format stream-json` (no argv prompt) | user event echoes stdin; reply ZQ7X |
| 10 | A | `delegate --json cursor safe --model composer-2.5 "Reply ZQ7X"` (del_20260907T062551Z_bcb564) | succeeded; argv has no prompt, `promptTransport: stdin`; model reports "Shell is blocked in ask mode" |
| 11 | A | `delegate --json omp safe --model kimi-code/k3 "Reply ZQ7X"` (del_20260907T062647Z_ca7c8b) | succeeded; stdin transport; `--cwd` rewritten to the isolated copy |
| 12 | A | `DELEGATE_OMP_BEHAVIOR_TEST=1 DELEGATE_OMP_BEHAVIOR_MODEL=kimi-code/k3 pytest tests/test_omp_read_only_behavior.py` vs omp 18.1.13 | 7 passed in 94 s (write denied, exec denied, hostile project `approvalMode: yolo` beaten). `opencode-go` provider returned 401 "Insufficient balance" — that lane is out of credit |
| 13 | A | `codex … exec --cd . --sandbox read-only -c 'web_search="live"' -c 'approval_policy="never"' --color BOGUS --json -` | fails only on `--color` → both overrides parse in the exec scope (no model call) |
| 14 | A | `claude -p … --permission-mode plan --tools Read,Grep,Glob,Bash --permission-prompts none --model haiku` | success envelope on 2.1.263 |
| 15 | A | `delegate --json capabilities refresh claude` then `dry-run claude safe` | argv carries `--permission-prompts none`. **Side effect:** this refresh (branch code) rewrote the shared discovery cache with a field the installed 0.30.0 reader rejects → live incident, see REPORT.md |
| 16 | coordinator | `printf … \| cursor-agent -p --trust --mode {plan,ask} --model composer-2.5` asking it to run `git log` | both modes: `SHELL_BLOCKED` → basis for the cursor-safe revert (F15) |
| 17 | coordinator | `codex … exec --sandbox workspace-write --json - -c 'sandbox_workspace_write.writable_roots=[…]' --color BOGUS` | fails only on `--color` → `-c` after the stdin `-` parses (Grok G1 not a defect) |
| 18 | reviews | `delegate omp safe --model fireworks/glm-5.3` (×2), `delegate cursor safe --model cursor-grok-4.6-xhigh`, Astra plan review ×2 | first GLM run died on delegate's 16 MiB stdout cap (dlg-o7i); rerun with a narrowed brief succeeded |

Not exercised live, stated plainly: cursor `--resume` with a stdin prompt; any Kimi, Devin, Droid, or
OpenCode child; codex session-file flush timing after `turn.completed` (F5 chose not to arm the exit
kill instead of proving it); the Devin read-only behavioral probe on 3000.6.14.
