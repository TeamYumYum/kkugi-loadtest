"""Derive one-minute client and hourly server traffic for the three benchmark APIs."""
import argparse
import csv
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path

from tools.daily_report import KST, Prometheus, day_window, traffic

APIS = {"growth": "GET /growth/me", "meal": "POST /meals", "workout": "POST /workout-sessions"}


def csv_traffic(path, start, end):
    snapshots = defaultdict(dict)
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            name = row["Name"].removeprefix("[public] ").removeprefix("[private] ")
            if name in APIS.values():
                snapshots[int(row["Timestamp"])][row["Name"]] = int(row["Total Request Count"])
    total = defaultdict(int)
    minutes = []
    counts = {}
    previous = None
    for ts, snapshot in sorted(snapshots.items()):
        if previous is not None and start < ts <= end and 0 < ts - previous <= 180:
            delta = defaultdict(int)
            for key, value in snapshot.items():
                name = key.removeprefix("[public] ").removeprefix("[private] ")
                delta[name] += max(0, value - counts.get(key, 0))
                total[name] += max(0, value - counts.get(key, 0))
            minutes.append({"end": datetime.fromtimestamp(ts, KST).isoformat(),
                            "seconds": ts - previous, "counts": dict(delta)})
        counts.update(snapshot)
        previous = ts
    peaks = sorted(minutes, key=lambda m: sum(m["counts"].get(a, 0) for a in APIS.values())
                   / m["seconds"], reverse=True)[:10]
    return {"total": dict(total), "coverage_seconds": sum(m["seconds"] for m in minutes), "top_minutes": peaks}


def historical(path, base, date):
    start, end = day_window(date)
    prom = Prometheus(base)
    rates = {}
    counts = {}
    for key, name in APIS.items():
        method, route = name.split()
        selector = f'http_server_requests_seconds_count{{synthetic="true",method="{method}",uri=~"(/api/v1)?{route}"}}'
        rates[key] = prom.query_range(f"sum(increase({selector}[1h]))", start + 3600, end, 3600)
        counts[key] = prom.scalar(f"sum(increase({selector}[1d]))", end)
    return {"day": date, "client": csv_traffic(path, start, end),
            "client_http": traffic(str(path), start, end), "server_day_counts": counts, "server_hour_counts": rates}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--prom", default="http://127.0.0.1:16390")
    parser.add_argument("--date", default="2026-11-10")
    args = parser.parse_args()
    print(json.dumps(historical(args.csv, args.prom, args.date), indent=2))
