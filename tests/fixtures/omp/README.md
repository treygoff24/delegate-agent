# OMP fixtures

`thinking_deltas.sanitized.jsonl` contains representative records extracted from
a local September 4, 2026 run that reached the 16 MiB stdout cap. The original
key structure, content indexes, and delta character counts are retained; delta
text is replaced with `x` characters. The harness version was not recorded.

Tests repeat these captured shapes to cross the old byte cap. Any completion
appended by a test is synthetic: the original capped run did not finish.
