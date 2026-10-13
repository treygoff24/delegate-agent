# Muse stream fixtures

Raw stdout/stderr captured 2026-10-12 from Muse 1.4.6
(`Muse 1.4.6 (1.4.6-R6000.1)`), macOS. Real-provider runs used
`--model muse-spark-1.3 --reasoning-effort low --json --disable-approval`
in a temp git repo containing notes.txt ("alpha\nbeta").

- echo-simple: `--provider echo`, prompt "Say hello". Exit 0.
- real-tool-read: safe flags (`--disable-write --disable-shell
  --disable-web-tools`), prompt "Read notes.txt and tell me both lines."
  Exit 0.
- real-terminal-failed-schema: `--output-schema` with a schema lacking
  `additionalProperties: false`. Exit 1, run.terminal.failed.
- real-structured: `--output-schema` with `additionalProperties: false`.
  Exit 0; terminal text is the JSON answer.
No auth-failure fixture: a bogus META_API_KEY still completed on the capture
machine (account auth appears server-side), and capturing a logged-out run
would mean disturbing the operator's real login. Provider-error handling
rests on the run.terminal.failed reason text instead.
