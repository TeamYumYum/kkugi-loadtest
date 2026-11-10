import csv
import json
import urllib.error
import urllib.parse

import pytest

from tools import daily_report as dr

DATE = "2026-11-08"
START, END = dr.day_window(DATE)
HEADER = ["Timestamp", "User Count", "Type", "Name", "Requests/s", "Failures/s", "50%", "95%", "99%",
          "Total Request Count", "Total Failure Count", "Total Median Response Time",
          "Total Average Response Time", "Total Min Response Time", "Total Max Response Time",
          "Total Average Content Size"]


def _history(tmp_path, rows):
    """rows: (시각, Type, Name, 누적 요청, 누적 실패). Aggregated 줄과 Windows식 빈 줄도 섞는다."""
    path = tmp_path / "k_stats_history.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for t, kind, name, count, fail in rows:
            w.writerow([t, 360, kind, name, 0, 0, 0, 0, 0, count, fail, 10, 10.0, 1, 100, 50.0])
            w.writerow([t, 360, "", "Aggregated", 0, 0, 0, 0, 0, 999999, 999, 10, 10.0, 1, 100, 50.0])
            f.write("\r\n")
    return str(path)


def _steady_day(tmp_path, per_minute=60):
    """그날 내내 60초마다 GET /foods가 per_minute건씩 쌓이는 기록."""
    rows = [(t, "GET", "/foods", (i + 1) * per_minute, 0)
            for i, t in enumerate(range(START - 60, END + 61, 60))]
    return _history(tmp_path, rows)


def _vector(value, **labels):
    return {"metric": labels, "value": [END, str(value)]}


def _matrix(points):
    return {"metric": {}, "values": [[t, str(v)] for t, v in points]}


class FakeProm:
    """URL의 쿼리 문자열에 들어 있는 조각으로 응답을 고른다. 맞는 것이 없으면 빈 결과다."""

    def __init__(self, rules=()):
        self.rules = list(rules)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)["query"][0]
        result = []
        for path, needle, value in self.rules:
            if parsed.path.endswith(path) and needle in query:
                result = value
                break
        return json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()


def _healthy_rules(p95_foods=0.05, db_1m=60, pending=0, restart_at=None):
    started = START - 86400 * 3
    starts = [(t, started) for t in range(START, END + 1, 3600)]
    if restart_at is not None:
        starts = [(t, started if t < restart_at else restart_at) for t, _ in starts]
    api = {"method": "GET", "uri": "/foods"}
    return [
        ("/query_range", "[1h]", [_matrix([(START + 3600 * (h + 1), h + 0.5) for h in range(24)])]),
        ("/query_range", "process_start_time_seconds", [_matrix(starts)]),
        ("/query", "histogram_quantile(0.5,", [_vector(0.02, **api)]),
        ("/query", "histogram_quantile(0.95,", [_vector(p95_foods, **api)]),
        ("/query", "histogram_quantile(0.99,", [_vector(0.08, **api)]),
        ("/query", "_count", [_vector(86400, **api)]),
        ("/query", "rate(mysql_global_status_commands_total", [_vector(db_1m)]),
        ("/query", "increase(mysql_global_status_commands_total", [_vector(500000)]),
        ("/query", "hikaricp_connections_active", [_vector(3)]),
        ("/query", "hikaricp_connections_pending", [_vector(pending)]),
        ("/query", "threads_running", [_vector(4)]),
        ("/query", "slow_queries", [_vector(0)]),
        ("/query", "avg_over_time(process_cpu_usage", [_vector(0.024)]),
        ("/query", "max_over_time(process_cpu_usage", [_vector(0.335)]),
        ("/query", "jvm_memory_used_bytes", [_vector(438 * 1024 * 1024)]),
        ("/query", "jvm_gc_pause_seconds_sum", [_vector(45.2)]),
        ("/query", "jvm_gc_pause_seconds_max", [_vector(0.092)]),
    ]


def _build(history, rules=None, previous=None, failures=None):
    prom = dr.Prometheus("http://localhost:19090", FakeProm(_healthy_rules() if rules is None else rules))
    return dr.build(DATE, history, prom, failures, previous)


# ---------------------------------------------------------------- Locust CSV

def test_day_starts_and_ends_at_midnight_kst():
    # 2026-11-07 15:00 UTC → 11-08 15:00 UTC. 기준선 기록의 time=1794184620이 11-09 09:37 KST인 것과 맞다
    assert dr.day_window(DATE) == (1794063600, 1794150000)


def test_only_intervals_ending_inside_the_day_are_counted(tmp_path):
    path = _history(tmp_path, [
        (START - 120, "GET", "/foods", 0, 0),
        (START - 60, "GET", "/foods", 100, 0),
        (START, "GET", "/foods", 200, 0),            # 전날 23:59 → 00:00. 전날 몫이다
        (START + 60, "GET", "/foods", 260, 0),       # 그날 00시
        (START + 120, "GET", "/foods", 380, 0),      # 그날 00시. 1분 최대 2.0 TPS
        (END - 60, "GET", "/foods", 5000, 0),        # 3분 넘는 틈. Master가 멈춘 구간이라 뺀다
        (END, "GET", "/foods", 5030, 0),             # 23:59 → 24:00. 그날 23시
        (END + 60, "GET", "/foods", 9000, 0),        # 다음 날 몫이다
    ])
    t = dr.traffic(path, START, END)
    assert t["http_requests"] == 60 + 120 + 30
    assert t["covered_hours"] == pytest.approx(180 / 3600)
    assert t["avg_tps"] == pytest.approx(210 / 180)
    assert t["peak_minute_tps"] == pytest.approx(2.0)
    assert t["peak_minute_at"] == "00:02"
    assert t["peak_hour"] == "00시"
    assert t["peak_hour_tps"] == pytest.approx(1.5)
    assert set(t["hourly"]) == {0, 23}
    assert t["hourly"][23]["tps"] == pytest.approx(0.5)


def test_failures_sessions_and_missed_sessions(tmp_path):
    rows = []
    for i, t in enumerate((START - 60, START + 60, START + 120)):
        rows += [
            (t, "GET", "/foods", 10 * i, i),                          # HTTP 실패 2
            (t, "POST", "/meals", 5 * i, 0),
            (t, "SESSION", "dieter_lunch", 4 * i, i),                 # 세션 8, 세션 실패 2
            (t, "SESSION", "dieter_lunch (skipped)", 100 * i, 0),     # 이미 기록한 끼니. 세지 않는다
            (t, "SCHEDULE", "missed", 3 * i, 3 * i),                  # 놓친 세션 6. 세션 실패가 아니다
            (t, "SCHEDULE", "scaled_out", 50 * i, 0),                 # 배율로 거른 세션. 세지 않는다
            (t, "SCHEDULE", "start_delay", 7 * i, 0),
            (t, "ACCOUNT", "signup", 2 * i, i),                       # HTTP가 아니다
        ]
    t = dr.traffic(_history(tmp_path, rows), START, END)
    assert t["http_requests"] == 20 + 10
    assert t["http_failures"] == 2
    assert t["sessions"] == 8
    assert t["session_failures"] == 2
    assert t["missed_sessions"] == 6


def test_name_that_appears_mid_day_counts_from_zero(tmp_path):
    path = _history(tmp_path, [
        (START + 60, "GET", "/foods", 10, 0),
        (START + 120, "GET", "/foods", 20, 0),
        (START + 120, "GET", "/growth/me", 5, 0),
    ])
    assert dr.traffic(path, START, END)["http_requests"] == 15


def test_failures_csv_is_sorted_by_occurrences(tmp_path):
    path = tmp_path / "k_failures.csv"
    path.write_text("Method,Name,Error,Occurrences\n"
                    "POST,/workout-sessions,HTTPError('500 Server Error'),1\n"
                    "GET,/foods,ConnectionError(),4\n", encoding="utf-8")
    rows = dr.failures(str(path))
    assert [r["occurrences"] for r in rows] == [4, 1]
    assert rows[1]["name"] == "/workout-sessions"


# ---------------------------------------------------------------- Prometheus와 판정

def test_healthy_day_has_no_cautions(tmp_path):
    report = _build(_steady_day(tmp_path))
    assert report["cautions"] == []
    assert report["traffic"]["http_requests"] == 60 * 1440
    assert report["traffic"]["avg_tps"] == pytest.approx(1.0)
    assert report["apis"] == [{"api": "GET /foods", "count": 86400, "p50_ms": pytest.approx(20),
                               "p95_ms": pytest.approx(50), "p99_ms": pytest.approx(80)}]
    assert report["hourly"][0]["db_tps"] == pytest.approx(0.5)   # 01:00 시각의 [1h] 값이 00시대다
    assert report["hourly"][23]["db_tps"] == pytest.approx(23.5)
    assert report["restarts"] == []
    res = report["resources"]
    assert res["heap_max_mb"] == pytest.approx(438)
    assert res["gc_pause_max_ms"] == pytest.approx(92)
    text = dr.markdown(report)
    assert "## 주의\n\n- 없음" in text
    assert "| `GET /foods` | 86,400 | 20.0 | 50.0 | 80.0 |" in text
    assert "| JVM CPU | 평균 2.4%, 최대 33.5% |" in text
    assert "| backend 재시작 | 없음 |" in text


def test_each_threshold_raises_a_caution(tmp_path):
    rows = [(START + 60, "GET", "/foods", 0, 0), (START + 120, "GET", "/foods", 10, 1),
            (START + 60, "SCHEDULE", "missed", 0, 0), (START + 120, "SCHEDULE", "missed", 2, 2)]
    restart = START + 19 * 3600 + 28 * 60
    rules = _healthy_rules(p95_foods=0.3005, db_1m=100.5, pending=1, restart_at=restart)
    cautions = _build(_history(tmp_path, rows), rules)["cautions"]
    text = "\n".join(cautions)
    assert "Locust 기록이 0.0시간 분량뿐" in text
    assert "HTTP 실패 1건" in text
    assert "놓친 세션(`SCHEDULE missed`) 2건" in text
    assert "`GET /foods` p95 300.5ms" in text
    assert "1분 DB TPS 최대 100.5" in text
    assert "커넥션 대기 최대 1" in text
    assert "backend 재시작: 19:28:00" in text
    assert len(cautions) == 7


def test_values_at_the_threshold_are_not_cautions(tmp_path):
    rules = _healthy_rules(p95_foods=dr.MAX_API_P95_MS / 1000, db_1m=dr.MAX_DB_TPS_1M, pending=0)
    assert _build(_steady_day(tmp_path), rules)["cautions"] == []


@pytest.mark.parametrize("p95, flagged", [(0.150, False), (0.1501, True)])
def test_compare_flags_p95_growth_over_one_and_a_half(tmp_path, p95, flagged):
    previous = {"date": "2026-11-07", "apis": [{"api": "GET /foods", "p95_ms": 100.0},
                                                {"api": "GET /gone", "p95_ms": 1.0}]}
    cautions = _build(_steady_day(tmp_path), _healthy_rules(p95_foods=p95), previous)["cautions"]
    growth = [c for c in cautions if "→" in c]
    if flagged:
        assert growth == ["`GET /foods` p95 100.0 → 150.1ms (1.50배, 2026-11-07 대비)"]
    else:
        assert growth == []


def test_empty_prometheus_shows_no_value_and_does_not_crash(tmp_path):
    report = _build(_steady_day(tmp_path), rules=[])
    assert report["apis"] == []
    assert report["restarts"] is None
    assert all(h["db_tps"] is None for h in report["hourly"])
    assert all(v is None for v in report["resources"].values())
    cautions = "\n".join(report["cautions"])
    # 기준을 판정하지 못한 것은 조용히 넘어가지 않고 주의에 남는다
    assert "API별 서버 응답 시간을 확인하지 못했다" in cautions
    assert "1분 DB TPS 최대를 확인하지 못했다" in cautions
    assert "커넥션 대기 최대를 확인하지 못했다" in cautions
    assert "backend 재시작 여부를 확인하지 못했다" in cautions
    text = dr.markdown(report)
    assert "| 00 | 1.000 | 0.000 | 값 없음 |" in text
    assert "| backend 재시작 | 값 없음 |" in text
    assert "| JVM CPU | 평균 값 없음, 최대 값 없음 |" in text


def test_nan_quantile_is_no_value(tmp_path):
    rules = [("/query", "histogram_quantile", [_vector("NaN", method="GET", uri="/foods")]),
             ("/query", "_count", [_vector(0, method="GET", uri="/foods")])]
    report = _build(_steady_day(tmp_path), rules)
    assert report["apis"] == [{"api": "GET /foods", "count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}]


def test_empty_locust_day_shows_no_value(tmp_path):
    path = _history(tmp_path, [(START - 120, "GET", "/foods", 0, 0), (START - 60, "GET", "/foods", 10, 0)])
    report = _build(path)
    assert report["traffic"]["http_requests"] == 0
    text = dr.markdown(report)
    assert "| 평균 TPS | 값 없음 |" in text
    assert "| 1분 최대 TPS | 값 없음 |" in text


def test_unreachable_prometheus_stops_instead_of_skipping_checks(tmp_path):
    def refused(url):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(SystemExit, match="Prometheus에 접속하지 못했다"):
        dr.build(DATE, _steady_day(tmp_path), dr.Prometheus("http://localhost:19090", refused))


def test_queries_use_the_day_window(tmp_path):
    fake = FakeProm(_healthy_rules())
    dr.build(DATE, _steady_day(tmp_path), dr.Prometheus("http://localhost:19090/", fake))
    params = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query) for u in fake.urls]
    assert all(u.startswith("http://localhost:19090/api/v1/") for u in fake.urls)
    instant = [p for p in params if "time" in p]
    assert {p["time"][0] for p in instant} == {str(END)}
    assert all("[86400s" in p["query"][0] for p in instant)
    histogram = [p["query"][0] for p in instant if "histogram_quantile(0.95" in p["query"][0]]
    assert histogram == ['histogram_quantile(0.95, sum by (le,method,uri)'
                         '(increase(http_server_requests_seconds_bucket{synthetic="true"}[86400s])))']


# ---------------------------------------------------------------- 명령줄

def test_main_writes_markdown_and_json_for_the_next_compare(tmp_path, capsys):
    history = _steady_day(tmp_path)
    failures = tmp_path / "k_failures.csv"
    failures.write_text("Method,Name,Error,Occurrences\nPOST,/workout-sessions,500 | 데드락,1\n", encoding="utf-8")
    first = tmp_path / "2026-11-08.json"
    dr.main(["--date", DATE, "--history", history, "--prometheus", "http://localhost:19090",
             "--failures", str(failures), "--json", str(first)], fetch=FakeProm(_healthy_rules()))
    out = capsys.readouterr().out
    assert out.startswith("# 하루 요약: 2026-11-08 (한국 시간 00:00-24:00)")
    assert "| `POST /workout-sessions` | 500 / 데드락 | 1 |" in out
    saved = json.loads(first.read_text(encoding="utf-8"))
    assert saved["date"] == DATE
    assert saved["apis"][0]["p95_ms"] == pytest.approx(50)
    assert saved["thresholds"]["max_api_p95_ms"] == dr.MAX_API_P95_MS

    dr.main(["--date", DATE, "--history", history, "--prometheus", "http://localhost:19090",
             "--compare", str(first)], fetch=FakeProm(_healthy_rules(p95_foods=0.08)))
    out = capsys.readouterr().out
    assert "- `GET /foods` p95 50.0 → 80.0ms (1.60배, 2026-11-08 대비)" in out


# ---------------------------------------------------------------- Mattermost

def test_mattermost_sends_cautions_and_key_numbers(tmp_path, capsys):
    history = _steady_day(tmp_path)
    sent = []
    dr.main(["--date", DATE, "--history", history, "--prometheus", "http://localhost:19090", "--mattermost"],
            fetch=FakeProm(_healthy_rules(p95_foods=0.5)), post=lambda url, body: sent.append((url, body)),
            env={dr.WEBHOOK_ENV: "https://mm.example/hooks/abc"})
    capsys.readouterr()
    assert len(sent) == 1
    url, body = sent[0]
    assert url == "https://mm.example/hooks/abc"
    text = json.loads(body.decode("utf-8"))["text"]
    assert text.startswith(f"#### :chiikawa_candy: 인공 트래픽 하루 요약 {DATE}")
    assert "**주의" in text and "GET /foods" in text
    assert "HTTP 요청" in text


def test_mattermost_without_webhook_fails_instead_of_skipping(tmp_path, capsys):
    history = _steady_day(tmp_path)
    sent = []
    with pytest.raises(SystemExit, match=dr.WEBHOOK_ENV):
        dr.main(["--date", DATE, "--history", history, "--prometheus", "http://localhost:19090", "--mattermost"],
                fetch=FakeProm(_healthy_rules()), post=lambda url, body: sent.append(url), env={})
    assert sent == []


def test_no_mattermost_flag_sends_nothing(tmp_path, capsys):
    history = _steady_day(tmp_path)
    sent = []
    dr.main(["--date", DATE, "--history", history, "--prometheus", "http://localhost:19090"],
            fetch=FakeProm(_healthy_rules()), post=lambda url, body: sent.append(url),
            env={dr.WEBHOOK_ENV: "https://mm.example/hooks/abc"})
    assert sent == []


@pytest.mark.parametrize("text, safe", [
    ("backend 재시작: 09:49:51, 10:14:44", "backend 재시작: `09:49:51`, `10:14:44`"),
    ("p99 3200ms (11-10 09:00).", "p99 3200ms (11-10 09:00)."),
    ("이미 `10:14:44` 감쌈", "이미 `10:14:44` 감쌈"),
    ("문장 :smile: 끝", "문장 `:smile:` 끝"),
    ("주의: 없음", "주의: 없음"),
    (":usagi_chainsaw: [경보] 10:14:44", ":usagi_chainsaw: [경보] `10:14:44`"),
    (":dancing_usagi: [해소]", ":dancing_usagi: [해소]"),
])
def test_mattermost_safe_keeps_emoji_codes_from_rendering(text, safe):
    assert dr.mattermost_safe(text) == safe
