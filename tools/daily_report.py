"""하루(한국 시간 00:00-24:00)의 트래픽·서버 응답 시간·서버 자원을 마크다운 요약으로 쓴다.

    python tools/daily_report.py --date 2026-11-08 --history <prefix>_stats_history.csv \\
        --prometheus http://localhost:19090 [--failures <prefix>_failures.csv] [--json out.json] [--compare prev.json]

Prometheus는 관측 서버 안의 localhost:9090이다. 노트북에서는 `ssh -L 19090:localhost:9090 kkugi-obs` 터널로 연다.
기준을 넘은 항목은 맨 위 "주의" 목록에 모은다. 기준값은 아래 상수이고 모두 가정이다.
--json으로 저장한 파일을 다음 날 --compare에 주면 API별 p95가 크게 늘어난 것도 주의에 올린다.
--mattermost를 주면 주의 목록과 핵심 숫자를 Mattermost 웹훅(환경 변수 KKUGI_MM_WEBHOOK_URL)으로 보낸다. 주소가 비어 있으면
보내지 않고 실패한다. 주소는 비밀값이라 저장소에 두지 않는다.

트래픽 숫자는 Locust history CSV의 누적값 증가분이다(tools/stats_summary.py hourly와 같은 방식). 그날 구간의 기록만 세고,
180초 넘게 벌어진 기록 사이는 Master가 멈춘 구간으로 보고 뺀다.
--failures의 실패 내역은 Locust가 시각 없이 Master 시작부터 누적한 것이라 그날 것만 고를 수 없다. 참고로만 붙인다.
용어는 docs/glossary.md를 따른다(TPS = 초당 HTTP 요청, 세션/s, DB TPS = 초당 DB 커밋).
"""

import argparse
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

try:
    from tools.stats_summary import KST, NON_HTTP
except ImportError:  # python tools/daily_report.py로 실행하면 tools/가 경로의 맨 앞이다
    from stats_summary import KST, NON_HTTP

# ---- 주의 기준. 모두 가정이다. 기준선 기록(notes/decisions/)이 쌓이면 다시 정한다 ----
MAX_HTTP_FAILURES = 0          # 가정: HTTP 실패가 이 수를 넘으면 주의
MAX_MISSED_SESSIONS = 0        # 가정: 놓친 세션(SCHEDULE missed)이 이 수를 넘으면 주의
MAX_API_P95_MS = 300.0         # 가정: 서버 기준 API p95(ms)가 이 값을 넘으면 주의
MAX_DB_TPS_1M = 100.0          # 가정: 1분 DB TPS 최대가 이 값을 넘으면 주의
MAX_PENDING_CONNECTIONS = 0    # 가정: Hikari 커넥션 대기 최대가 이 값을 넘으면 주의
P95_GROWTH_RATIO = 1.5         # 가정: --compare의 이전 p95보다 이 배수를 넘게 늘면 주의
MIN_LOCUST_COVERAGE_HOURS = 23.0  # 가정: 그날 Locust 기록이 이 시간보다 짧으면 주의(Master가 멈췄거나 CSV가 다른 것)

MAX_GAP_SECONDS = 180  # stats_summary.py와 같다. 60초 간격 기록 사이만 센다
DAY_SECONDS = 86400
NO_VALUE = "값 없음"

Fetch = Callable[[str], bytes]
# (주소, JSON 본문) -> None. 바꿔 끼우면 네트워크 없이 시험할 수 있다
Post = Callable[[str, bytes], None]
WEBHOOK_ENV = "KKUGI_MM_WEBHOOK_URL"


# ---------------------------------------------------------------- Locust CSV

def day_window(date: str) -> tuple[int, int]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=KST)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _hhmm(t: int) -> str:
    return datetime.fromtimestamp(t, KST).strftime("%H:%M")


def traffic(path: str, start: int, end: int) -> dict:
    """history CSV에서 (start, end] 구간의 트래픽 요약과 시(KST)별 TPS·세션/s를 구한다."""
    kinds: dict[str, str] = {}
    by_time: dict[int, dict[str, tuple[int, int]]] = defaultdict(dict)
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["Name"] == "Aggregated":
                continue
            key = f"{row['Type']} {row['Name']}"
            kinds[key] = row["Type"]
            by_time[int(row["Timestamp"])][key] = (int(row["Total Request Count"]), int(row["Total Failure Count"]))

    def totals(snapshot: dict[str, tuple[int, int]]) -> dict[str, int]:
        out = dict.fromkeys(("http", "http_fail", "sessions", "session_fail", "missed"), 0)
        for key, (count, failures) in snapshot.items():
            kind = kinds[key]
            if kind not in NON_HTTP:
                out["http"] += count
                out["http_fail"] += failures
            elif kind == "SESSION" and "(skipped)" not in key:
                out["sessions"] += count
                out["session_fail"] += failures
            elif key == "SCHEDULE missed":
                out["missed"] += count
        return out

    sums = dict.fromkeys(("http", "http_fail", "sessions", "session_fail", "missed"), 0)
    seconds = 0
    hours: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])  # 시 → [HTTP, 세션, 초]
    peak_minute: tuple[float, int] | None = None
    cumulative: dict[str, tuple[int, int]] = {}
    previous = None
    for t in sorted(by_time):
        cumulative.update(by_time[t])
        now = (t, totals(cumulative))
        if previous and start < t <= end and 0 < t - previous[0] <= MAX_GAP_SECONDS:
            dt = t - previous[0]
            delta = {k: now[1][k] - previous[1][k] for k in sums}
            for k in sums:
                sums[k] += delta[k]
            seconds += dt
            # 끝 시각 1초 전의 시로 묶는다. 23:59 → 24:00 기록이 그날 00시가 아닌 23시로 간다
            bucket = hours[datetime.fromtimestamp(t - 1, KST).hour]
            bucket[0] += delta["http"]
            bucket[1] += delta["sessions"]
            bucket[2] += dt
            rate = delta["http"] / dt
            if peak_minute is None or rate > peak_minute[0]:
                peak_minute = (rate, t)
        previous = now

    hourly = {h: {"tps": b[0] / b[2], "sessions_per_s": b[1] / b[2]} for h, b in hours.items() if b[2]}
    peak_hour = max(hourly.items(), key=lambda item: item[1]["tps"]) if hourly else None
    return {
        "http_requests": sums["http"],
        "avg_tps": sums["http"] / seconds if seconds else None,
        "peak_hour_tps": peak_hour[1]["tps"] if peak_hour else None,
        "peak_hour": f"{peak_hour[0]:02d}시" if peak_hour else None,
        "peak_minute_tps": peak_minute[0] if peak_minute else None,
        "peak_minute_at": _hhmm(peak_minute[1]) if peak_minute else None,
        "sessions": sums["sessions"],
        "http_failures": sums["http_fail"],
        "session_failures": sums["session_fail"],
        "missed_sessions": sums["missed"],
        "covered_hours": seconds / 3600,
        "hourly": hourly,
    }


def failures(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = [{"method": r["Method"], "name": r["Name"], "error": r["Error"], "occurrences": int(r["Occurrences"])}
                for r in csv.DictReader(f)]
    return sorted(rows, key=lambda r: -r["occurrences"])


# ---------------------------------------------------------------- Prometheus

def _urlopen(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


class Prometheus:
    """HTTP API 조회. fetch(url) -> 응답 본문을 바꿔 끼우면 네트워크 없이 시험할 수 있다."""

    def __init__(self, base: str, fetch: Fetch = _urlopen):
        self.base = base.rstrip("/")
        self.fetch = fetch

    def _get(self, path: str, params: dict) -> list:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        try:
            body = json.loads(self.fetch(url))
        except (urllib.error.URLError, OSError) as e:
            # 조용히 넘어가면 자원 검사가 통째로 사라진다. 터널이 닫혔으면 멈춘다
            raise SystemExit(f"Prometheus에 접속하지 못했다({self.base}): {e}") from e
        if body.get("status") != "success":
            raise SystemExit(f"Prometheus 조회 실패: {params.get('query')}: {body.get('error')}")
        return body["data"]["result"]

    def query(self, query: str, time: int) -> list:
        return self._get("/api/v1/query", {"query": query, "time": time})

    def query_range(self, query: str, start: int, end: int, step: int) -> list:
        return self._get("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})

    def scalar(self, query: str, time: int) -> float | None:
        """결과의 첫 값. 결과가 없거나 NaN이면 None."""
        result = self.query(query, time)
        return _number(result[0]["value"][1]) if result else None


def _number(text: str) -> float | None:
    value = float(text)
    return None if math.isnan(value) or math.isinf(value) else value


COMMITS = 'mysql_global_status_commands_total{command="commit"}'
SYNTHETIC = 'http_server_requests_seconds{suffix}{{synthetic="true"}}'


def server(prom: Prometheus, start: int, end: int) -> dict:
    """그날 구간의 서버 기준 값. 구간 끝 시각에 [하루] 범위로 조회한다."""
    window = f"[{end - start}s]"
    sub = f"[{end - start}s:1m]"

    db_hourly = {}
    for series in prom.query_range(f"sum(rate({COMMITS}[1h]))", start + 3600, end, 3600)[:1]:
        for t, v in series["values"]:
            # t 시각의 [1h] 값은 직전 한 시간이다. 01:00의 값이 00시대다
            db_hourly[datetime.fromtimestamp(int(float(t)) - 3600, KST).hour] = _number(v)

    apis: dict[str, dict] = {}
    count = SYNTHETIC.format(suffix="_count")
    for r in prom.query(f"sum by (method,uri)(increase({count}{window}))", end):
        api = f"{r['metric'].get('method', '?')} {r['metric'].get('uri', '?')}"
        apis[api] = {"api": api, "count": _number(r["value"][1]), "p50_ms": None, "p95_ms": None, "p99_ms": None}
    bucket = SYNTHETIC.format(suffix="_bucket")
    for q in (0.5, 0.95, 0.99):
        query = f"histogram_quantile({q}, sum by (le,method,uri)(increase({bucket}{window})))"
        for r in prom.query(query, end):
            api = f"{r['metric'].get('method', '?')} {r['metric'].get('uri', '?')}"
            entry = apis.setdefault(api, {"api": api, "count": None, "p50_ms": None, "p95_ms": None, "p99_ms": None})
            value = _number(r["value"][1])
            entry[f"p{round(q * 100)}_ms"] = value * 1000 if value is not None else None
    api_rows = sorted(apis.values(), key=lambda a: -(a["count"] or 0))
    for a in api_rows:
        if a["count"] is not None:
            a["count"] = round(a["count"])

    heap = prom.scalar(f'max_over_time(sum(jvm_memory_used_bytes{{job="backend",area="heap"}}){sub})', end)
    resources = {
        "db_commits": prom.scalar(f"sum(increase({COMMITS}{window}))", end),
        "db_tps_1m_max": prom.scalar(f"max_over_time(sum(rate({COMMITS}[1m])){sub})", end),
        "hikari_active_max": prom.scalar(f"max(max_over_time(hikaricp_connections_active{window}))", end),
        "hikari_pending_max": prom.scalar(f"max(max_over_time(hikaricp_connections_pending{window}))", end),
        "mysql_threads_running_max": prom.scalar(
            f"max(max_over_time(mysql_global_status_threads_running{window}))", end),
        "mysql_slow_queries": prom.scalar(f"sum(increase(mysql_global_status_slow_queries{window}))", end),
        "jvm_cpu_avg": prom.scalar(f'avg(avg_over_time(process_cpu_usage{{job="backend"}}{window}))', end),
        "jvm_cpu_max": prom.scalar(f'max(max_over_time(process_cpu_usage{{job="backend"}}{window}))', end),
        "heap_max_mb": heap / 1024 / 1024 if heap is not None else None,
        "gc_pause_total_s": prom.scalar(f'sum(increase(jvm_gc_pause_seconds_sum{{job="backend"}}{window}))', end),
        "gc_pause_max_ms": None,
    }
    gc_max = prom.scalar(f'max(max_over_time(jvm_gc_pause_seconds_max{{job="backend"}}{window}))', end)
    if gc_max is not None:
        resources["gc_pause_max_ms"] = gc_max * 1000

    # 시작 시각 값 가운데 그날 안에 있는 것이 재시작이다. 범위 조회 결과가 아예 없으면 확인하지 못한 것이다
    starts = prom.query_range('max(process_start_time_seconds{job="backend"})', start, end, 60)
    restarts = None
    if starts:
        seen = {int(float(v)) for series in starts for _, v in series["values"] if _number(v) is not None}
        restarts = [datetime.fromtimestamp(s, KST).strftime("%H:%M:%S") for s in sorted(seen) if start <= s < end]
    return {"db_hourly": db_hourly, "apis": api_rows, "resources": resources, "restarts": restarts}


# ---------------------------------------------------------------- 판정과 출력

def cautions(report: dict, previous: dict | None = None) -> list[str]:
    out = []
    t, res = report["traffic"], report["resources"]
    if t["covered_hours"] < MIN_LOCUST_COVERAGE_HOURS:
        out.append(f"Locust 기록이 {t['covered_hours']:.1f}시간 분량뿐이다(기준 {MIN_LOCUST_COVERAGE_HOURS:g}시간). "
                   "Master가 멈췄거나 다른 CSV일 수 있다")
    if t["http_failures"] > MAX_HTTP_FAILURES:
        out.append(f"HTTP 실패 {t['http_failures']:,}건")
    if t["missed_sessions"] > MAX_MISSED_SESSIONS:
        out.append(f"놓친 세션(`SCHEDULE missed`) {t['missed_sessions']:,}건")
    if not report["apis"]:
        out.append("API별 서버 응답 시간을 확인하지 못했다(Prometheus 값 없음)")
    for a in report["apis"]:
        if a["p95_ms"] is not None and a["p95_ms"] > MAX_API_P95_MS:
            out.append(f"`{a['api']}` p95 {a['p95_ms']:.1f}ms (기준 {MAX_API_P95_MS:g}ms)")
    db = res["db_tps_1m_max"]
    if db is None:
        out.append("1분 DB TPS 최대를 확인하지 못했다(Prometheus 값 없음)")
    elif db > MAX_DB_TPS_1M:
        out.append(f"1분 DB TPS 최대 {db:.1f} (기준 {MAX_DB_TPS_1M:g})")
    pending = res["hikari_pending_max"]
    if pending is None:
        out.append("커넥션 대기 최대를 확인하지 못했다(Prometheus 값 없음)")
    elif pending > MAX_PENDING_CONNECTIONS:
        out.append(f"커넥션 대기 최대 {pending:g}")
    if report["restarts"] is None:
        out.append("backend 재시작 여부를 확인하지 못했다(Prometheus 값 없음)")
    elif report["restarts"]:
        out.append(f"backend 재시작: {', '.join(report['restarts'])}")
    if previous:
        before = {a["api"]: a.get("p95_ms") for a in previous.get("apis", [])}
        for a in report["apis"]:
            old = before.get(a["api"])
            if a["p95_ms"] is not None and old and a["p95_ms"] > old * P95_GROWTH_RATIO:
                out.append(f"`{a['api']}` p95 {old:.1f} → {a['p95_ms']:.1f}ms "
                           f"({a['p95_ms'] / old:.2f}배, {previous.get('date', '이전')} 대비)")
    return out


def build(date: str, history: str, prom: Prometheus, failures_path: str | None = None,
          previous: dict | None = None) -> dict:
    start, end = day_window(date)
    t = traffic(history, start, end)
    s = server(prom, start, end)
    hourly = [{"hour": h, "tps": t["hourly"].get(h, {}).get("tps"),
               "sessions_per_s": t["hourly"].get(h, {}).get("sessions_per_s"), "db_tps": s["db_hourly"].get(h)}
              for h in range(24)]
    report = {
        "date": date,
        "traffic": {k: v for k, v in t.items() if k != "hourly"},
        "hourly": hourly,
        "apis": s["apis"],
        "resources": s["resources"],
        "restarts": s["restarts"],
        "failures": failures(failures_path) if failures_path else None,
        "thresholds": {
            "max_http_failures": MAX_HTTP_FAILURES, "max_missed_sessions": MAX_MISSED_SESSIONS,
            "max_api_p95_ms": MAX_API_P95_MS, "max_db_tps_1m": MAX_DB_TPS_1M,
            "max_pending_connections": MAX_PENDING_CONNECTIONS, "p95_growth_ratio": P95_GROWTH_RATIO,
            "min_locust_coverage_hours": MIN_LOCUST_COVERAGE_HOURS,
        },
    }
    report["cautions"] = cautions(report, previous)
    return report


def _f(value, fmt: str = ".3f", suffix: str = "") -> str:
    return NO_VALUE if value is None else f"{value:{fmt}}{suffix}"


def markdown(report: dict) -> str:
    t, res = report["traffic"], report["resources"]
    lines = [f"# 하루 요약: {report['date']} (한국 시간 00:00-24:00)", "", "## 주의", ""]
    lines += [f"- {c}" for c in report["cautions"]] or ["- 없음"]
    peak_hour = NO_VALUE if t["peak_hour"] is None else f"{t['peak_hour_tps']:.3f} ({t['peak_hour']})"
    peak_min = NO_VALUE if t["peak_minute_at"] is None else f"{t['peak_minute_tps']:.2f} ({t['peak_minute_at']})"
    lines += [
        "", "## 트래픽", "",
        f"Locust 기록 {t['covered_hours']:.1f}시간 분량의 누적값 증가분이다.", "",
        "| 항목 | 값 |", "|---|---:|",
        f"| HTTP 요청 | {t['http_requests']:,} |",
        f"| 평균 TPS | {_f(t['avg_tps'])} |",
        f"| 1시간 최대 TPS | {peak_hour} |",
        f"| 1분 최대 TPS | {peak_min} |",
        f"| 세션 | {t['sessions']:,} |",
        f"| HTTP 실패 | {t['http_failures']:,} |",
        f"| 세션 실패 | {t['session_failures']:,} |",
        f"| 놓친 세션 (`SCHEDULE missed`) | {t['missed_sessions']:,} |",
        "", "## 시간대별", "",
        "DB TPS는 그 시간대(예: 17시는 17:00-18:00)의 평균 커밋/s다.", "",
        "| 시 | TPS | 세션/s | DB TPS |", "|---|---:|---:|---:|",
    ]
    lines += [f"| {h['hour']:02d} | {_f(h['tps'])} | {_f(h['sessions_per_s'])} | {_f(h['db_tps'], '.1f')} |"
              for h in report["hourly"]]
    lines += ["", "## API별 응답 시간 (서버 기준)", "",
              "인공 요청(`synthetic=\"true\"`)만 센다. 백분위는 히스토그램 구간으로 추정한 값이고 단위는 ms다.", ""]
    if report["apis"]:
        lines += ["| API | 요청 수 | p50 | p95 | p99 |", "|---|---:|---:|---:|---:|"]
        lines += [f"| `{a['api']}` | {_f(a['count'], ',')} | {_f(a['p50_ms'], '.1f')} | {_f(a['p95_ms'], '.1f')} | "
                  f"{_f(a['p99_ms'], '.1f')} |" for a in report["apis"]]
    else:
        lines.append(NO_VALUE)
    restarts = NO_VALUE if report["restarts"] is None else (", ".join(report["restarts"]) or "없음")
    lines += [
        "", "## 서버 자원", "",
        "| 지표 | 값 | 보는 곳 |", "|---|---|---|",
        f"| Hikari 커넥션 최대 사용 | {_f(res['hikari_active_max'], 'g')} | `hikaricp_connections_active` |",
        f"| Hikari 커넥션 대기 최대 | {_f(res['hikari_pending_max'], 'g')} | `hikaricp_connections_pending` |",
        f"| MySQL 실행 중 스레드 최대 | {_f(res['mysql_threads_running_max'], 'g')} | "
        "`mysql_global_status_threads_running` |",
        f"| MySQL 느린 쿼리 증가 | {_f(res['mysql_slow_queries'], '.0f', '건')} | `mysql_global_status_slow_queries` |",
        f"| DB 커밋 | {_f(res['db_commits'], ',.0f', '건')}, 1분 최대 {_f(res['db_tps_1m_max'], '.1f', '/s')} | "
        f"`{COMMITS}` |",
        f"| JVM CPU | 평균 {_f(None if res['jvm_cpu_avg'] is None else res['jvm_cpu_avg'] * 100, '.1f', '%')}, "
        f"최대 {_f(None if res['jvm_cpu_max'] is None else res['jvm_cpu_max'] * 100, '.1f', '%')} | "
        "`process_cpu_usage` |",
        f"| 힙 최대 | {_f(res['heap_max_mb'], '.0f', 'MB')} | `jvm_memory_used_bytes{{area=\"heap\"}}` |",
        f"| GC 정지 | 합계 {_f(res['gc_pause_total_s'], '.1f', '초')}, 1회 최대 {_f(res['gc_pause_max_ms'], '.0f', 'ms')} | "
        "`jvm_gc_pause_seconds` |",
        f"| backend 재시작 | {restarts} | `process_start_time_seconds` |",
    ]
    if report["failures"] is not None:
        lines += ["", "## 실패 내역 (참고)", "",
                  "Locust 실패 CSV는 Master 시작부터의 누적이다. 그날 것만 고를 수 없다.", ""]
        if report["failures"]:
            lines += ["| 요청 | 오류 | 횟수 |", "|---|---|---:|"]
            lines += [f"| `{r['method']} {r['name']}` | {r['error'].replace('|', '/')} | {r['occurrences']:,} |"
                      for r in report["failures"]]
        else:
            lines.append("없음")
    return "\n".join(lines) + "\n"


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def mattermost_text(report: dict) -> str:
    """Mattermost로 보낼 짧은 요약. 주의 목록이 먼저고, 그 뒤에 하루의 핵심 숫자를 한 줄로 붙인다."""
    t = report["traffic"]
    lines = [f"#### 인공 트래픽 하루 요약 {report['date']}"]
    if report["cautions"]:
        lines.append(f"**주의 {len(report['cautions'])}건**")
        lines += [f"- {c}" for c in report["cautions"]]
    else:
        lines.append("주의 없음")
    lines.append(
        f"HTTP 요청 {_f(t.get('http_requests'), ',')}, 평균 {_f(t.get('avg_tps'), '.3f')} TPS, "
        f"HTTP 실패 {_f(t.get('http_failures'), ',')}, 놓친 세션 {_f(t.get('missed_sessions'), ',')}"
    )
    return "\n".join(lines)


def send_mattermost(report: dict, post: Post = _post, env: dict | None = None) -> None:
    url = (os.environ if env is None else env).get(WEBHOOK_ENV, "").strip()
    if not url:
        raise SystemExit(f"--mattermost를 주었지만 {WEBHOOK_ENV}가 비어 있다. 보내지 않았다")
    post(url, json.dumps({"text": mattermost_text(report)}, ensure_ascii=False).encode("utf-8"))


def main(argv: list[str] | None = None, fetch: Fetch = _urlopen, post: Post = _post,
         env: dict | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):  # cp949 콘솔에서 한글(--help 포함)이 깨지지 않게 한다
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="YYYY-mm-dd (한국 시간)")
    parser.add_argument("--history", required=True, help="Locust <prefix>_stats_history.csv")
    parser.add_argument("--prometheus", required=True, help="예: http://localhost:19090")
    parser.add_argument("--failures", help="Locust <prefix>_failures.csv")
    parser.add_argument("--json", dest="json_path", help="같은 내용을 JSON으로도 저장할 경로")
    parser.add_argument("--compare", help="이전 보고서의 JSON. API별 p95 증가를 판정한다")
    parser.add_argument("--mattermost", action="store_true",
                        help=f"주의 목록과 핵심 숫자를 Mattermost 웹훅({WEBHOOK_ENV})으로 보낸다")
    args = parser.parse_args(argv)
    previous = None
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            previous = json.load(f)
    report = build(args.date, args.history, Prometheus(args.prometheus, fetch), args.failures, previous)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    sys.stdout.write(markdown(report))
    if args.mattermost:
        send_mattermost(report, post, env)


if __name__ == "__main__":
    main()
