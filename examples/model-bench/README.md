# model-bench: a benchmark sweep run as a Delegate workflow

Which model should get which job? This example answers it from public benchmarks,
refreshed on demand, using Delegate to fan the collection out across many cheap agent
lanes.

It runs in four steps:

1. **Scout.** One agent maps the benchmark landscape as of today and writes
   `manifest.jsonl`: every benchmark worth collecting, with its data source,
   categories, and whether it reports cost, time, and reasoning effort.
2. **Collect.** `workflow.py` runs one agent per benchmark. Each pulls the
   freshest results and writes them in one shared row format (`SCHEMA.md`), so
   nothing needs cleanup afterwards.
3. **Verify.** In the same pipeline, a second agent spot-checks each benchmark's
   rows against the saved source capture and fixes what it can prove wrong.
4. **Combine and analyze.** `bench.py combine` validates and concatenates every
   benchmark into `combined.jsonl`, and the analysis turns it into per-category
   rankings and cost/time/quality Pareto frontiers under `reports/`.

## Refresh

```bash
# 1. Scout (writes manifest.jsonl and data/_scout/NOTES.md)
delegate codex work --model luna --reasoning-effort xhigh \
  --prompt-file <(sed "s/{DATE}/$(date +%F)/" examples/model-bench/briefs/scout.md)

# 2-3. Collect and verify every benchmark (preview the run tree with --dry-run)
python3 examples/model-bench/bench.py collect --dry-run
python3 examples/model-bench/bench.py collect

# 4. Combine
python3 examples/model-bench/bench.py combine
```

Routes default to a low-cost model for both collection and verification; pass
`--routes '{"collect": {...}, "verify": {...}}'` to use others. The workflow is
resumable: `delegate workflow resume <wf_id>` picks up lanes that died mid-run.

## What is and is not published

- `data/<id>/rows.jsonl` and `NOTES.md` carry numbers with their source URL and
  retrieval time. Check a benchmark's license (recorded in its manifest line and
  notes) before redistributing its rows.
- `raw/` holds the source captures used for verification. It is gitignored:
  those are other people's pages.
- Scores are only as good as their sources. Vendor-reported rows are marked
  `reporter: vendor`; weight them accordingly.
