"""Create a token-free aggregate from the per-run benchmark results."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path


def structure(windows):
    isolated = [w for w in windows if not w["background_detected"]]
    n = sum(w["requests"] for w in isolated)
    counts = Counter()
    times = Counter()
    hikari = Counter()
    for w in isolated:
        for statement, row in w["sql"].items():
            verb = statement.split(" ", maxsplit=1)[0].upper()
            counts[verb] += row["count"]
            times[verb] += row["server_ms"]
        hikari.update(w["hikari"])
    acquisitions = hikari["hikaricp_connections_acquire_seconds_count"]
    return {
        "isolated_requests": n, "excluded_background_requests": sum(w["requests"] for w in windows) - n,
        "sql_statements_per_request": sum(counts.values()) / n if n else None,
        "sql_selects_per_request": counts["SELECT"] / n if n else None,
        "sql_statements_by_verb": counts,
        "sql_server_ms_per_request": sum(times.values()) / n if n else None,
        "select_server_ms_per_request": times["SELECT"] / n if n else None,
        "hikari_acquisitions_per_request": acquisitions / n if n else None,
        "hikari_avg_acquire_ms": hikari["hikaricp_connections_acquire_seconds_sum"] * 1000 / acquisitions
        if acquisitions else None,
        "hikari_usage_ms_per_request": hikari["hikaricp_connections_usage_seconds_sum"] * 1000 / n if n else None,
    }


def events(mixed):
    snapshots = mixed["event_samples"]
    if snapshots is None:
        return None
    initial = {r["id"]: r for r in snapshots[0]["handlers"]}
    after = {r["id"]: r for r in mixed["event_after_drain"]["handlers"]}
    per_handler = {}
    for key, end in after.items():
        begin = initial.get(key, {})
        per_handler[key] = {metric: end[metric] - begin.get(metric, 0) for metric in ("published", "processed")}
    frames = sorted([*snapshots, mixed["event_at_stop"], mixed["event_after_drain"]], key=lambda f: f["at"])

    def backlog(frame):
        return sum(r["pending"] for r in frame["handlers"])

    return {"handlers": per_handler, "samples": len(frames),
            "sampling_max_gap_s": max((datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds()
                                      for a, b in zip(frames, frames[1:])),
            "sampling_errors": mixed["event_sampling_errors"],
            "peak_pending_sampled": max(map(backlog, frames)),
            "oldest_pending_sampled_ms": max((r["oldest_ms"] for f in frames for r in f["handlers"]), default=0),
            "pending_at_stop": backlog(mixed["event_at_stop"]),
            "pending_after_drain": backlog(mixed["event_after_drain"])}


def summarize(data):
    http = data["mixed"]["http"]
    for row in http.values():
        if row["arrival_span_ms"]:
            row["observed_start_rps"] = (row["started"] - 1) / (row["arrival_span_ms"] / 1000)
        if row["completion_span_ms"]:
            row["observed_completion_rps"] = row["completed"] / (row["completion_span_ms"] / 1000)
    return {"run": data["tag"], "latency_ms_per_direction": data["latency_per_direction_ms"],
            "sql_rtt": data["sql_rtt"], "single_http": data["single"]["http"],
            "single_db": structure(data["single"]["structure"]), "burst_http": data["burst"]["http"]["growth"],
            "mixed_http": http, "events": events(data["mixed"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    expected = [args.raw_dir / f"r{rep}-{version}-{latency}ms.json"
                for latency in (0, 1, 2) for rep in (1, 2) for version in ("before", "after")]
    runs, missing = [], []
    for path in expected:
        if not path.is_file():
            missing.append(path.name)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if "mixed" in data:
            runs.append(summarize(data))
        else:
            missing.append(path.name)
    if missing and not args.allow_partial:
        parser.error("Missing complete measurements: " + ", ".join(missing))
    args.out.write_text(json.dumps(runs, indent=2), encoding="utf-8")
