"""Summarize matching server spans in a restored local Tempo block."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import statistics
from urllib.parse import urlencode
from urllib.request import urlopen

from tools.daily_report import KST


def search(base, start, end):
    args = urlencode({"q": '{ kind = server && span.http.route =~ ".*growth/me" }',
                      "start": start, "end": end, "limit": 500})
    with urlopen(f"{base}/api/search?{args}", timeout=30) as source:
        return json.load(source)["traces"]


def summarize(traces):
    timestamps = Counter()
    services = Counter()
    durations = []
    for trace in traces:
        services[trace.get("rootServiceName", "")] += 1
        stamp = int(trace["startTimeUnixNano"]) / 1e9
        timestamps[datetime.fromtimestamp(stamp, KST).strftime("%H:%M:%S")] += 1
        durations.append(trace.get("durationMs", 0))
    return {"matches": len(traces), "root_starts_per_second_kst": dict(sorted(timestamps.items())),
            "root_services": dict(services), "root_duration_median_ms": statistics.median(durations) if durations else None}


def trace_db(base, trace):
    with urlopen(f"{base}/api/traces/{trace['traceID']}", timeout=20) as source:
        spans = [span for batch in json.load(source)["batches"]
                 for scope in batch["scopeSpans"] for span in scope["spans"]]
    counts = Counter()
    totals = Counter()
    for span in spans:
        name = span["name"]
        if name == "HikariDataSource.getConnection":
            metric = "acquire"
        elif name == "Execute prepared statement":
            metric = "execute"
        elif name.startswith(("Prepare statement", "Set ", "Get ")) or name in {"Commit", "Rollback"}:
            metric = "other_wire"
        else:
            continue
        counts[metric] += 1
        totals[metric] += (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6
    return {"acquisitions": counts["acquire"], "execute_spans": counts["execute"],
            "other_wire_spans": counts["other_wire"], "acquire_ms_total": totals["acquire"],
            "execute_ms_total": totals["execute"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:16393")
    parser.add_argument("--start", default="2026-11-11T07:14:50+09:00")
    parser.add_argument("--end", default="2026-11-11T07:17:10+09:00")
    parser.add_argument("--db-sample", type=int, default=40)
    args = parser.parse_args()
    start, end = (int(datetime.fromisoformat(t).timestamp()) for t in (args.start, args.end))
    traces = search(args.base, start, end)
    result = summarize(traces)
    ordered = sorted(traces, key=lambda t: int(t["startTimeUnixNano"]))
    n = min(args.db_sample, len(ordered))
    if n > 0:
        selected = [ordered[i * len(ordered) // n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            db = list(pool.map(lambda trace: trace_db(args.base, trace), selected))
        result["db_trace_sample"] = {key: {"min": min(row[key] for row in db),
                                           "median": statistics.median(row[key] for row in db),
                                           "max": max(row[key] for row in db)}
                                     for key in db[0]}
        result["db_trace_n"] = n
    print(json.dumps(result, indent=2))
