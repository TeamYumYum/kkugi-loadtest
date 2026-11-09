"""실시간 경보 감시. 상태가 바뀔 때만 보내고, 조용히 꺼지지 않는지 본다."""

import json
import urllib.parse
from datetime import datetime, timedelta

import pytest

from tools import alert_watch as aw

T0 = datetime(2026, 11, 9, 20, 0, tzinfo=aw.KST)
HEALTHY = {"backend_up": 1.0, "errors_5xx": 0.0, "new_5xx_series": None, "p95_s": 0.08, "rps": 1.0, "db_commits": 60.0,
           "pending": 0.0, "restarts": 0.0}
CONTROL = {"requested_scale": 1.0, "scale": 1.0,
           "workers": {"aaa_1": {"alive": True}, "bbb_2": {"alive": True}},
           "governor": {"last_window": {"seconds": 300.0, "requests": 900, "failures": 0}, "last_change": None}}


def _snap(prom=None, control=None, **changes):
    values = {**HEALTHY, **changes}
    return aw.Snapshot(values if prom is None else prom, CONTROL if control is None else control)


def _watch():
    sent = []
    return aw.Watch(sent.append), sent


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_healthy_sends_nothing():
    watch, sent = _watch()
    for m in range(10):
        watch.evaluate(_snap(), _at(m))
    assert sent == []


def test_fires_once_after_for_minutes_and_resolves_once():
    watch, sent = _watch()
    for m in range(5):  # DB 커밋 급증은 5분 이어져야 경보다
        watch.evaluate(_snap(db_commits=200.0), _at(m))
    assert sent == []
    watch.evaluate(_snap(db_commits=200.0), _at(5))
    watch.evaluate(_snap(db_commits=210.0), _at(6))
    assert sent == ["[경보] DB 커밋 급증: DB 커밋 200/s(기준 150/s)"]
    watch.evaluate(_snap(), _at(9))
    assert sent[-1] == "[해소] DB 커밋 급증 (9분 이어짐)"
    assert len(sent) == 2


def test_short_blip_does_not_fire():
    watch, sent = _watch()
    watch.evaluate(_snap(db_commits=200.0), _at(0))
    watch.evaluate(_snap(), _at(1))
    watch.evaluate(_snap(db_commits=200.0), _at(2))
    assert sent == []


def test_immediate_rules():
    watch, sent = _watch()
    watch.evaluate(_snap(errors_5xx=1.0, restarts=1.0), _at(0))
    assert "[경보] 5xx 응답: 최근 5분 5xx 응답 1건(인공·실제 요청 모두)" in sent
    assert "[경보] backend 재시작: backend가 다시 시작했다(최근 5분)" in sent


def test_first_5xx_of_a_new_series_still_fires():
    watch, sent = _watch()
    watch.evaluate(_snap(errors_5xx=None, new_5xx_series=1.0), _at(0))
    assert sent == ["[경보] 5xx 응답: 최근 5분 5xx 응답 0건(인공·실제 요청 모두), 처음 나타난 5xx API·코드 1개"]


def test_slow_is_not_judged_at_night_with_few_requests():
    watch, sent = _watch()
    for m in range(10):
        watch.evaluate(_snap(p95_s=3.0, rps=0.05), _at(m))
    assert sent == []
    for m in range(10, 16):
        watch.evaluate(_snap(p95_s=3.0, rps=1.0), _at(m))
    assert sent == ["[경보] 응답 느림: 인공 요청 p95 3000ms(최근 5분, 기준 1000ms)"]


def test_backend_target_missing_is_an_alert():
    watch, sent = _watch()
    for m in range(3):
        watch.evaluate(_snap(backend_up=None), _at(m))
    assert sent == ["[경보] backend 지표 수집 안 됨: Prometheus가 backend 지표를 수집하지 못한다(up=0 또는 대상 없음)"]


def test_generator_rules_from_master_control():
    control = {**CONTROL, "scale": 0.7,
               "workers": {"aaa_1": {"alive": True}, "bbb_2": {"alive": False}},
               "governor": {"last_window": {"seconds": 300.0, "requests": 800, "failures": 3},
                            "last_change": "실패율 2% 초과"}}
    watch, sent = _watch()
    watch.evaluate(_snap(control=control), _at(0))
    watch.evaluate(_snap(control=control), _at(2))
    assert "[경보] 발생기 실패: 발생기 HTTP 실패 3건(최근 300초, 요청 800건)" in sent
    assert "[경보] 거버너 배율 낮춤: 거버너가 배율을 1.0 → 0.7로 낮췄다: 실패율 2% 초과" in sent
    assert "[경보] Worker 응답 없음: 응답 없는 Worker 1개: bbb" in sent


def test_fetch_failures_alert_after_three_and_resolve():
    watch, sent = _watch()
    for m in range(2):
        watch.evaluate(aw.Snapshot(None, CONTROL), _at(m))
    assert sent == []
    watch.evaluate(aw.Snapshot(None, CONTROL), _at(2))
    assert sent == ["[경보] Prometheus 조회 실패 3회 연속. 이 쪽의 경보는 판정하지 못한다"]
    watch.evaluate(_snap(), _at(3))
    assert sent[-1] == "[해소] Prometheus 조회가 다시 된다"


def test_heartbeat_once_a_day_at_nine():
    watch, sent = _watch()
    watch.heartbeat(datetime(2026, 11, 10, 8, 59, tzinfo=aw.KST))
    watch.heartbeat(datetime(2026, 11, 10, 9, 0, tzinfo=aw.KST))
    watch.heartbeat(datetime(2026, 11, 10, 9, 1, tzinfo=aw.KST))
    assert sent == ["경보 감시 동작 중 (11-10 09:00). 이어지는 경보: 없음"]


def _fake_fetch(values, control, fail_prometheus=False):
    reverse = {expr: key for key, expr in aw.QUERIES.items()}

    def fetch(url):
        if url.endswith("/kkugi/control"):
            return json.dumps(control).encode()
        if fail_prometheus:
            raise OSError("connection refused")
        expr = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        value = values[reverse[expr]]
        result = [] if value is None else [{"metric": {}, "value": [0, str(value)]}]
        return json.dumps({"status": "success", "data": {"result": result}}).encode()
    return fetch


def test_query_prometheus_maps_empty_and_nan_to_none():
    fetch = _fake_fetch({**HEALTHY, "p95_s": None, "pending": float("nan")}, CONTROL)
    values = aw.query_prometheus("http://p:9090", fetch)
    assert values["p95_s"] is None and values["pending"] is None
    assert values["db_commits"] == 60.0


def test_main_requires_webhook():
    with pytest.raises(SystemExit, match=aw.WEBHOOK_ENV):
        aw.main(["--prometheus", "http://p:9090", "--master", "http://m:8089"], env={}, rounds=1)


def test_main_posts_start_and_alerts(capsys):
    posts = []
    aw.main(["--prometheus", "http://p:9090", "--master", "http://m:8089"],
            env={aw.WEBHOOK_ENV: "https://mm.example/hooks/x"},
            fetch=_fake_fetch({**HEALTHY, "errors_5xx": 2.0}, CONTROL), post=lambda u, b: posts.append((u, b)),
            sleep=lambda _s: None, now=lambda: T0, rounds=2)
    texts = [json.loads(b.decode("utf-8"))["text"] for _u, b in posts]
    assert texts[0].startswith("경보 감시 시작")
    assert texts[1:] == ["[경보] 5xx 응답: 최근 5분 5xx 응답 2건(인공·실제 요청 모두)"]
    assert all(u == "https://mm.example/hooks/x" for u, _b in posts)


def test_send_failure_does_not_stop_watching(capsys):
    def broken(_url, _body):
        raise OSError("timeout")
    aw.main(["--prometheus", "http://p:9090", "--master", "http://m:8089"],
            env={aw.WEBHOOK_ENV: "https://mm.example/hooks/x"},
            fetch=_fake_fetch(HEALTHY, CONTROL, fail_prometheus=True), post=broken,
            sleep=lambda _s: None, now=lambda: T0, rounds=3)
    out = capsys.readouterr().out
    assert "Mattermost 전송 실패" in out
    assert "Prometheus 조회 실패 3회 연속" in out


def test_heartbeat_also_posts_yesterdays_daily_report(tmp_path, monkeypatch):
    stats = tmp_path / "stats"
    stats.mkdir()
    (stats / "kkugi-x_stats_history.csv").write_text("x", encoding="utf-8")
    (tmp_path / "2026-11-08.json").write_text(json.dumps({"apis": []}), encoding="utf-8")
    seen = {}

    def fake_build(day, history, prom, failures, previous):
        seen.update(day=day, history=history, previous=previous)
        return {"date": day, "cautions": ["HTTP 실패 1건"], "traffic": {}}

    from tools import daily_report as dr
    monkeypatch.setattr(dr, "build", fake_build)
    monkeypatch.setattr(dr, "mattermost_text", lambda r: f"요약 {r['date']}: {r['cautions'][0]}")
    sent = []
    aw.post_daily_report("2026-11-09", str(stats), str(tmp_path), "http://p:9090", lambda u: b"", sent.append)
    assert seen["day"] == "2026-11-09" and seen["history"].endswith("kkugi-x_stats_history.csv")
    assert seen["previous"] == {"apis": []}
    assert sent == ["요약 2026-11-09: HTTP 실패 1건"]
    assert json.loads((tmp_path / "2026-11-09.json").read_text(encoding="utf-8"))["date"] == "2026-11-09"


def test_daily_report_failure_is_sent_not_swallowed(tmp_path):
    sent = []
    aw.post_daily_report("2026-11-09", str(tmp_path), str(tmp_path), "http://p:9090", lambda u: b"", sent.append)
    assert len(sent) == 1 and sent[0].startswith("[경보] 2026-11-09 하루 요약을 만들지 못했다")
