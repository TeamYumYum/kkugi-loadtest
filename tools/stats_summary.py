"""Master가 남긴 Locust 통계 CSV를 측정 기록용 표로 요약한다.

    python tools/stats_summary.py hourly <prefix>_stats_history.csv [--from 'YYYY-mm-dd HH:MM'] [--to ...]
    python tools/stats_summary.py apis <prefix>_stats.csv
    python tools/stats_summary.py apis <prefix>_stats_history.csv [--to 'YYYY-mm-dd HH:MM']

hourly: 시(KST)별 TPS(HTTP 요청/s)와 세션/s. 이름별 누적 요청 수의 증가분을 경과 초로 나눈다.
apis: API별 요청 수, 실패, 평균·p50·p95·p99·최대(ms), 평균 응답 크기. 이어서 세션·일정 통계.
history CSV를 주면 --to 시각 이전 마지막 기록의 누적값을 쓴다. 이 기록에는 누적 백분위가 없어 p50 자리에 누적 중앙값을,
p95·p99 자리에 '-'를 적는다.
측정 방법과 용어는 notes/decisions/2026-11-02-baseline-1000.md를 따른다.
"""

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
NON_HTTP = ("SESSION", "SCHEDULE", "ACCOUNT")


def _kst(text: str | None) -> int | None:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()) if text else None


def hourly(path: str, start: int | None, end: int | None) -> None:
    kinds: dict[str, str] = {}
    by_time: dict[int, dict[str, int]] = defaultdict(dict)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["Name"] == "Aggregated":
                continue
            kinds[row["Name"]] = row["Type"]
            by_time[int(row["Timestamp"])][row["Name"]] = int(row["Total Request Count"])

    def totals(snapshot: dict[str, int]) -> tuple[int, int]:
        http = sum(v for n, v in snapshot.items() if kinds[n] not in NON_HTTP)
        sessions = sum(v for n, v in snapshot.items() if kinds[n] == "SESSION" and "(skipped)" not in n)
        return http, sessions

    hours: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    cumulative: dict[str, int] = {}
    previous = None
    first = last = None
    for t in sorted(by_time):
        cumulative.update(by_time[t])
        now = (t, totals(cumulative))
        inside = (start is None or t > start) and (end is None or t <= end)
        # 60초 간격 기록 사이만 센다. 그보다 긴 틈은 Master가 멈춘 구간이다
        if previous and inside and 0 < t - previous[0] <= 180:
            bucket = hours[datetime.fromtimestamp(t, KST).strftime("%m-%d %H")]
            bucket[0] += now[1][0] - previous[1][0]
            bucket[1] += now[1][1] - previous[1][1]
            bucket[2] += t - previous[0]
            first = first or previous
            last = now
        previous = now
    print("| 시각 (KST) | TPS | 세션/s |")
    print("|---|---:|---:|")
    for hour in sorted(hours):
        http, sessions, seconds = hours[hour]
        print(f"| {hour}시 | {http / seconds:.3f} | {sessions / seconds:.3f} |")
    if first and last:
        seconds = last[0] - first[0]
        print(f"\n구간 평균 ({seconds / 3600:.1f}시간): TPS {(last[1][0] - first[1][0]) / seconds:.3f}, "
              f"세션/s {(last[1][1] - first[1][1]) / seconds:.3f}")


def _history_rows(path: str, end: int | None) -> list[dict[str, str]]:
    """history CSV에서 이름마다 end 이전 마지막 기록의 누적값을 _stats.csv와 같은 열 이름으로 돌려준다."""
    last: dict[str, dict[str, str]] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["Name"] != "Aggregated" and (end is None or int(r["Timestamp"]) <= end):
                last[r["Name"]] = r
    return [{
        "Type": r["Type"], "Name": r["Name"], "Request Count": r["Total Request Count"],
        "Failure Count": r["Total Failure Count"], "Average Response Time": r["Total Average Response Time"],
        "50%": r["Total Median Response Time"], "95%": "-", "99%": "-",
        "Max Response Time": r["Total Max Response Time"], "Average Content Size": r["Total Average Content Size"],
    } for r in last.values()]


def apis(path: str, end: int | None = None) -> None:
    with open(path, encoding="utf-8") as f:
        history = "Timestamp" in (f.readline())
    if history:
        rows = _history_rows(path, end)
    else:
        if end is not None:
            raise SystemExit("--to는 history CSV에만 쓸 수 있다. _stats.csv는 지금까지의 누적값이다")
        with open(path, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["Name"] != "Aggregated"]
    http = sorted((r for r in rows if r["Type"] not in NON_HTTP), key=lambda r: -int(r["Request Count"]))
    print("| API | 요청 수 | 실패 | 평균 | p50 | p95 | p99 | 최대 | 응답 크기(B) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in http:
        print(f"| `{r['Name']}` | {int(r['Request Count']):,} | {r['Failure Count']} | "
              f"{float(r['Average Response Time']):.0f} | {r['50%']} | {r['95%']} | {r['99%']} | "
              f"{float(r['Max Response Time']):.0f} | {float(r['Average Content Size']):.0f} |")
    print("\n| 세션·일정 | 건수 | 실패 | 평균 | p50 | p95 | p99 | 최대 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted((r for r in rows if r["Type"] in NON_HTTP), key=lambda r: (r["Type"], r["Name"])):
        print(f"| `{r['Type']} {r['Name']}` | {int(r['Request Count']):,} | {r['Failure Count']} | "
              f"{float(r['Average Response Time']):.0f} | {r['50%']} | {r['95%']} | {r['99%']} | "
              f"{float(r['Max Response Time']):.0f} |")
    total = sum(int(r["Request Count"]) for r in http)
    sessions = sum(int(r["Request Count"]) for r in rows if r["Type"] == "SESSION" and "(skipped)" not in r["Name"])
    if sessions:
        print(f"\nHTTP 요청 {total:,}건, 세션 {sessions:,}건, 세션당 요청 {total / sessions:.2f}개")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hourly")
    h.add_argument("history")
    h.add_argument("--from", dest="start")
    h.add_argument("--to", dest="end")
    a = sub.add_parser("apis")
    a.add_argument("stats")
    a.add_argument("--to", dest="end")
    args = parser.parse_args()
    if args.command == "hourly":
        hourly(args.history, _kst(args.start), _kst(args.end))
    else:
        apis(args.stats, _kst(args.end))


if __name__ == "__main__":
    main()
