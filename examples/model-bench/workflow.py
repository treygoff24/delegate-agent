# model-bench collection workflow: one collect -> verify pipeline per benchmark.
#
# Launched by run.py, which reads manifest.jsonl and the brief templates and
# passes everything through args, so this script carries no model roster:
#   args = {
#     "date": "YYYY-MM-DD",
#     "items": [{"benchmark_id": ..., "item_json": "<manifest line, pretty>"}],
#     "collect_template": "<briefs/collect.md text>",
#     "verify_template": "<briefs/verify.md text>",
#     "routes": {"collect": {...}, "verify": {...}},   # agent(**route) kwargs
#   }
# Children run in `work` mode against the real checkout; each owns only
# data/<id>/ and raw/<id>/, so lanes never write the same file.

meta = {
    "name": "model-bench-collect",
    "description": "Collect each benchmark into the shared row format, then spot-verify it.",
}

COLLECT = {
    "type": "object",
    "properties": {
        "benchmark_id": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["ok", "partial", "blocked"]},
        "rows": {"type": "integer"},
        "models": {"type": "integer"},
        "data_date": {"type": ["string", "null"]},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["benchmark_id", "status", "rows", "models", "data_date", "summary"],
    "additionalProperties": False,
}

VERIFY = {
    "type": "object",
    "properties": {
        "benchmark_id": {"type": "string", "minLength": 1},
        "verdict": {"type": "string", "enum": ["pass", "fixed", "fail"]},
        "checked": {"type": "integer"},
        "mismatches": {"type": "integer"},
        "fixed": {"type": "integer"},
        "coverage_ok": {"type": "boolean"},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["benchmark_id", "verdict", "checked", "mismatches", "fixed", "coverage_ok", "summary"],
    "additionalProperties": False,
}

date = args["date"]
items = args["items"]
routes = args["routes"]
collect_template = args["collect_template"]
verify_template = args["verify_template"]


def render(template, item_id, **fields):
    text = template.replace("{DATE}", date).replace("{ID}", item_id)
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text


def collect(_previous, item, index):
    bid = item["benchmark_id"]
    return agent(
        render(collect_template, bid, ITEM=item["item_json"]),
        **routes["collect"],
        mode="work",
        schema=COLLECT,
        label=f"collect:{bid}",
        phase="Collect",
        resumable=True,
    )


def verify(report, item, index):
    bid = item["benchmark_id"]
    if report is None or report.get("status") == "blocked" or not report.get("rows"):
        return {"collect": report, "verify": None}
    summary = (
        f'{{"status": "{report["status"]}", "rows": {report["rows"]}, '
        f'"models": {report["models"]}, "data_date": "{report["data_date"]}"}}'
    )
    checked = agent(
        render(verify_template, bid, REPORT=summary),
        **routes["verify"],
        mode="work",
        schema=VERIFY,
        label=f"verify:{bid}",
        phase="Verify",
    )
    return {"collect": report, "verify": checked}


results = pipeline(items, collect, verify)

by_id = {}
for item, result in zip(items, results):
    by_id[item["benchmark_id"]] = result

failed = [bid for bid, r in by_id.items() if r is None or r.get("collect") is None]
blocked = [bid for bid, r in by_id.items() if r and r.get("collect") and r["collect"].get("status") == "blocked"]
unverified = [
    bid
    for bid, r in by_id.items()
    if r and r.get("collect") and r["collect"].get("status") != "blocked" and r.get("verify") is None
]
verify_failed = [bid for bid, r in by_id.items() if r and r.get("verify") and r["verify"].get("verdict") == "fail"]

return {
    "date": date,
    "benchmarks": len(items),
    "results": by_id,
    "lane_failed": failed,
    "blocked": blocked,
    "unverified": unverified,
    "verify_failed": verify_failed,
}
