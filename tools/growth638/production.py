"""Aggregate the existing production traffic with read-only Prometheus and Locust GETs."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
import math
from pathlib import Path
from urllib.request import urlopen

from tools.daily_report import KST, MAX_GAP_SECONDS, NON_HTTP, Prometheus, day_window
from tools.growth638.history import APIS

HOME = ("GET /growth/me", "GET /daily-goals/{goalDate}", "GET /daily-summaries/{date}")
HTTP = 'http_server_requests_seconds_%s{job="backend",synthetic="true"}'


def finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


def percentiles(prom, start, end):
    seconds = end - start
    if seconds <= 0:
        return {}
    suffix = f"[{seconds}s]"
    count = f"sum by (method,uri)(increase({HTTP % 'count'}{suffix}))"
    buckets = f"sum by (le,method,uri)(increase({HTTP % 'bucket'}{suffix}))"
    rows = {}
    for key, query in (("n", count), ("p95_ms", f"histogram_quantile(0.95,{buckets})")):
        for entry in prom.query(query, end):
            val = finite(entry["value"][1])
            labels = entry["metric"]
            uri = labels["uri"].removeprefix("/api/v1")
            name = f"{labels['method']} {uri}"
            rows.setdefault(name, {})[key] = val * 1000 if val is not None and key == "p95_ms" else val
    return rows


def raw(prom, selector, start, end):
    return prom.query(f"{selector}[{end - start}s]", end)


def samples(series):
    return [(float(t), v) for entry in series for t, value in entry["values"] if (v := finite(value)) is not None]


def connection_wait(prom, start, end, master_restarts):
    process = samples(raw(prom, 'process_start_time_seconds{job="backend"}', start, end))
    restarts = sorted({round(value) for _, value in process if start - 300 <= value < end})
    exemptions = sorted(set(master_restarts) | set(restarts))
    pending_series = raw(prom, 'hikaricp_connections_pending{job="backend"}', start, end)
    pending = samples(pending_series)
    usable = [(stamp, val) for stamp, val in pending if not any(0 <= stamp - r < 300 for r in exemptions)]
    positive = [(stamp, value) for stamp, value in usable if value > 0]
    up_series = raw(prom, 'up{job="backend"}', start, end)
    up_by_target = []
    for item in up_series:
        points = samples([item])
        stamps = [t for t, value in points if value == 1]
        boundaries = [start, *stamps, end]
        up_by_target.append({"target": item["metric"].get("instance"),
                             "samples": len(points), "up": len(stamps),
                             "max_gap_s": round(max(b - a for a, b in zip(boundaries, boundaries[1:])), 3)})
    return {
        "backend_restarts": [stamp_iso(t) for t in restarts],
        "master_restarts": [stamp_iso(t) for t in master_restarts],
        "pending_samples": len(pending), "pending_excluded_samples": len(pending) - len(usable),
        "pending_max_all": max((v for _, v in pending), default=None),
        "pending_max_outside_restarts": max((v for _, v in usable), default=None),
        "pending_positive_timestamps": [{"at": stamp_iso(t), "n": v} for t, v in positive],
        "pending_targets": len(pending_series), "up_by_target": up_by_target,
    }


def stamp_iso(stamp):
    return datetime.fromtimestamp(stamp, KST).isoformat()


def client_segment(path, start, end):
    rows = defaultdict(dict)
    users = set()
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            ts = int(row["Timestamp"])
            if start < ts <= end:
                users.add(int(row["User Count"]))
            if row["Name"] != "Aggregated":
                rows[ts][row["Type"], row["Name"]] = int(row["Total Request Count"]), int(row["Total Failure Count"])
            else:
                rows.setdefault(ts, {})
    previous = None
    cumulative = {}
    totals = Counter()
    schedule = Counter()
    apis = Counter()
    resets = []
    hourly = defaultdict(Counter)
    dates = sorted(rows)
    window = [ts for ts in dates if start < ts <= end]
    for ts in dates:
        snapshot = rows[ts]
        if previous is not None and start < ts <= end and 0 < ts - previous <= MAX_GAP_SECONDS:
            hour = datetime.fromtimestamp(ts - 1, KST).hour
            duration = ts - max(start, previous)
            totals["coverage_seconds"] += duration
            hourly[hour]["coverage_seconds"] += duration
            lowered = 0
            for key, (count, failures) in snapshot.items():
                count_prev, failures_prev = cumulative.get(key, (0, 0))
                if count < count_prev or failures < failures_prev:
                    lowered += 1
                    continue
                delta = count - count_prev
                method, name = key
                if method not in NON_HTTP:
                    totals["http_requests"] += delta
                    totals["http_failures"] += failures - failures_prev
                    hourly[hour]["http_requests"] += delta
                    route = name.removeprefix("[public] ").removeprefix("[private] ")
                    if route in APIS.values():
                        apis[route] += delta
                elif method == "SESSION" and "(skipped)" not in name:
                    totals["sessions"] += delta
                    totals["session_failures"] += failures - failures_prev
                elif method in {"SCHEDULE", "ACCOUNT"}:
                    schedule[f"{method} {name}"] += delta
            if lowered:
                resets.append({"at": stamp_iso(ts), "series": lowered})
        cumulative.update(snapshot)
        previous = ts
    boundaries = [start, *window, end]
    first = dates[0] if dates else None
    try:
        started = int(datetime.strptime(path.name, "kkugi-%Y%m%d-%H%M%S_stats_history.csv")
                      .replace(tzinfo=KST).timestamp())
    except ValueError:
        started = first
    return {"path": path.name, "first": stamp_iso(first) if first is not None else None,
            "last": stamp_iso(dates[-1]) if dates else None, "master_start_ts": started,
            "runner_counts": sorted(users), "schedule_counts": schedule, "requests": totals,
            "target_apis": apis, "hourly": hourly, "counter_decreases": resets,
            "in_window_first": stamp_iso(window[0]) if window else None,
            "in_window_last": stamp_iso(window[-1]) if window else None,
            "largest_sample_gap_s": max(b - a for a, b in zip(boundaries, boundaries[1:]))}


def get_json(url):
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def live(locust):
    control = get_json(f"{locust}/kkugi/control")
    stats = get_json(f"{locust}/stats/requests")
    return {"observed_at": datetime.now(KST).isoformat(), "control": control,
            "stats": {key: stats.get(key) for key in
                      ("state", "user_count", "worker_count", "total_rps", "fail_ratio")}}


def report(prom, paths, date, start, end, complete_day):
    midnight, _ = day_window(date)
    segments = [client_segment(path, start, end) for path in paths]
    restarts = [item["master_start_ts"] for item in segments
                if item["master_start_ts"] is not None and start - 300 <= item["master_start_ts"] < end]
    wait = connection_wait(prom, start, end, restarts)
    day_apis = percentiles(prom, start, end)
    api_breaches = {api: row for api, row in day_apis.items() if (row.get("p95_ms") or 0) > 300}
    api_unmeasurable = [api for api, row in day_apis.items() if (row.get("n") or 0) > 0 and row.get("p95_ms") is None]
    dawn = percentiles(prom, max(start, midnight + 3600), min(end, midnight + 6 * 3600))
    peaks = {}
    for hour in (7, 12, 18):
        at = midnight + hour * 3600
        peak = percentiles(prom, max(start, at), min(end, at + 3600))
        peaks[hour] = {}
        for home in HOME:
            night = dawn.get(home, {})
            day = peak.get(home, {})
            baseline, measured = night.get("p95_ms"), day.get("p95_ms")
            limit = max(50, 2 * baseline) if baseline is not None else None
            peaks[hour][home] = {"dawn": night, "peak": day, "limit_ms": limit,
                                 "within_limit": measured <= limit if measured is not None and limit is not None else None}
    event_selector = 'kkugi_event_publication_latency_seconds_%s{job="backend"}'
    win = f"[{end - start}s]"
    return {
        "day": date, "start": stamp_iso(start), "end": stamp_iso(end),
        "elapsed_full_day": complete_day, "client_segments": segments, "api_day": day_apis,
        "api_breaches": api_breaches, "api_unmeasurable": api_unmeasurable, "home_hour_comparisons": peaks,
        "connection_wait": wait,
        "event_completed": prom.query(f"sum by (handler)(increase({event_selector % 'count'}{win}))", end),
        "event_total_seconds": prom.query(f"sum by (handler)(increase({event_selector % 'sum'}{win}))", end),
        "event_max_seconds": prom.query(f"max by (handler)(max_over_time({event_selector % 'max'}{win}))", end),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date")
    parser.add_argument("--csv", type=Path, action="append", required=True)
    parser.add_argument("--prom", default="http://127.0.0.1:16391")
    parser.add_argument("--locust", default="http://127.0.0.1:16392")
    parser.add_argument("--start-at", help="KST time HH:MM[:SS] for a partial observation")
    parser.add_argument("--end-at", help="KST time HH:MM[:SS] for a reproducible partial observation")
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    start, end = day_window(args.date)
    now = int(datetime.now(KST).timestamp())
    partial_bounds = args.start_at or args.end_at
    if (now < end or partial_bounds) and not args.partial:
        parser.error("The full KST day has not elapsed or a partial window was supplied. Use --partial for a snapshot.")
    if args.start_at:
        start = int(datetime.fromisoformat(f"{args.date}T{args.start_at}").replace(tzinfo=KST).timestamp())
    if args.end_at:
        end = int(datetime.fromisoformat(f"{args.date}T{args.end_at}").replace(tzinfo=KST).timestamp())
    end = min(end, now)
    if end <= start:
        parser.error("No elapsed time in this observation window")
    result = report(Prometheus(args.prom), args.csv, args.date, start, end,
                    now >= day_window(args.date)[1] and not partial_bounds)
    result["live_at_collection"] = live(args.locust)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
