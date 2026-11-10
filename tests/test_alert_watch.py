"""발생기 경보 감시. 상태가 바뀔 때만 보내고, 조용히 꺼지지 않는지 본다."""

import json
from datetime import datetime, timedelta

import pytest

from tools import alert_watch as aw

T0 = datetime(2026, 11, 9, 20, 0, tzinfo=aw.KST)
CONTROL = {"requested_scale": 1.0, "scale": 1.0,
           "workers": {"aaa_1": {"alive": True}, "bbb_2": {"alive": True}},
           "governor": {"last_window": {"seconds": 300.0, "requests": 900, "failures": 0}, "last_change": None}}


def _control(**changes):
    return {**CONTROL, **changes}


def _watch():
    sent = []
    return aw.Watch(sent.append), sent


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_healthy_sends_nothing():
    watch, sent = _watch()
    for m in range(10):
        watch.evaluate(CONTROL, _at(m))
    assert sent == []


def test_governor_fires_once_and_resolves_once():
    lowered = _control(scale=0.7)
    watch, sent = _watch()
    watch.evaluate(lowered, _at(0))
    watch.evaluate(lowered, _at(1))
    assert sent == ["[경보] 거버너 배율 낮춤: 거버너가 배율을 1.0 → 0.7로 낮췄다"]
    watch.evaluate(CONTROL, _at(6))
    assert sent[-1] == "[해소] 거버너 배율 낮춤 (6분 이어짐)"
    assert len(sent) == 2


def test_generator_http_failures_alone_do_not_alert():
    """발생기의 HTTP 실패는 대부분 backend 오류라 Grafana 5xx 경보가 맡는다. 하루 요약에만 남긴다."""
    watch, sent = _watch()
    for m in range(5):
        watch.evaluate(_control(governor={"last_window": {"seconds": 300.0, "requests": 800, "failures": 3}}), _at(m))
    assert sent == []


def test_governor_lowering_scale():
    watch, sent = _watch()
    change = {"at": "2026-11-09T08:57:14+00:00", "from": 1.0, "to": 0.7, "reason": "기준 초과로 낮춤: p99 3200ms > 3000ms"}
    watch.evaluate(_control(scale=0.7, governor={"last_change": change}), _at(0))
    assert sent == ["[경보] 거버너 배율 낮춤: 거버너가 배율을 1.0 → 0.7로 낮췄다. 기준 초과로 낮춤: p99 3200ms > 3000ms"]


def test_dead_worker_needs_two_minutes():
    dead = _control(workers={"aaa_1": {"alive": True}, "bbb_2": {"alive": False}})
    watch, sent = _watch()
    watch.evaluate(dead, _at(0))
    watch.evaluate(_control(), _at(1))
    watch.evaluate(dead, _at(2))
    watch.evaluate(dead, _at(3))
    assert sent == []
    watch.evaluate(dead, _at(4))
    assert sent == ["[경보] Worker 응답 없음: 응답 없는 Worker 1개: bbb"]


def test_master_fetch_failures_alert_after_three_and_resolve():
    watch, sent = _watch()
    watch.evaluate(None, _at(0))
    watch.evaluate(None, _at(1))
    assert sent == []
    watch.evaluate(None, _at(2))
    assert sent == ["[경보] 발생기 Master 조회 실패 3회 연속. 발생기 경보를 판정하지 못한다"]
    watch.evaluate(None, _at(3))
    watch.evaluate(CONTROL, _at(4))
    assert sent[-1] == "[해소] 발생기 Master 조회가 다시 된다"
    assert len(sent) == 2


def test_heartbeat_once_a_day_at_nine():
    watch, sent = _watch()
    assert not watch.heartbeat(datetime(2026, 11, 10, 8, 59, tzinfo=aw.KST))
    assert watch.heartbeat(datetime(2026, 11, 10, 9, 0, tzinfo=aw.KST))
    assert not watch.heartbeat(datetime(2026, 11, 10, 9, 1, tzinfo=aw.KST))
    assert sent == ["발생기 경보 감시 동작 중 (11-10 09:00). 이어지는 경보: 없음"]


def test_daily_report_and_status_are_one_message():
    watch, sent = _watch()
    assert watch.heartbeat(datetime(2026, 11, 10, 9, 0, tzinfo=aw.KST), lambda: "하루 요약 11-09")
    assert sent == ["하루 요약 11-09\n\n발생기 경보 감시 동작 중 (11-10 09:00). 이어지는 경보: 없음"]


def _fake_fetch(control, fail=False):
    def fetch(url):
        assert url.endswith("/kkugi/control")
        if fail:
            raise OSError("connection refused")
        return json.dumps(control).encode()
    return fetch


def test_main_requires_webhook():
    with pytest.raises(SystemExit, match=aw.WEBHOOK_ENV):
        aw.main(["--master", "http://m:8089"], env={}, rounds=1)


def test_main_requires_prometheus_for_daily_report():
    with pytest.raises(SystemExit):
        aw.main(["--master", "http://m:8089", "--stats-dir", "/x"], env={aw.WEBHOOK_ENV: "https://h"}, rounds=1)


def test_main_posts_start_and_alerts(capsys):
    posts = []
    failing = _control(scale=0.5)
    aw.main(["--master", "http://m:8089"], env={aw.WEBHOOK_ENV: "https://mm.example/hooks/x"},
            fetch=_fake_fetch(failing), post=lambda u, b: posts.append((u, b)),
            sleep=lambda _s: None, now=lambda: T0, rounds=2)
    texts = [json.loads(b.decode("utf-8"))["text"] for _u, b in posts]
    # 시작 알림은 보내지 않고 로그에만 남긴다
    assert "발생기 경보 감시 시작" in capsys.readouterr().out
    assert texts == ["[경보] 거버너 배율 낮춤: 거버너가 배율을 1.0 → 0.5로 낮췄다"]
    assert all(u == "https://mm.example/hooks/x" for u, _b in posts)


def test_send_failure_does_not_stop_watching(capsys):
    def broken(_url, _body):
        raise OSError("timeout")
    aw.main(["--master", "http://m:8089"], env={aw.WEBHOOK_ENV: "https://mm.example/hooks/x"},
            fetch=_fake_fetch(CONTROL, fail=True), post=broken, sleep=lambda _s: None, now=lambda: T0, rounds=3)
    out = capsys.readouterr().out
    assert "Mattermost 전송 실패" in out
    assert "발생기 Master 조회 실패 3회 연속" in out


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
    text = aw.daily_report_text("2026-11-09", str(stats), str(tmp_path), "http://p:9090", lambda u: b"")
    assert seen["day"] == "2026-11-09" and seen["history"].endswith("kkugi-x_stats_history.csv")
    assert seen["previous"] == {"apis": []}
    assert text == "요약 2026-11-09: HTTP 실패 1건"
    assert json.loads((tmp_path / "2026-11-09.json").read_text(encoding="utf-8"))["date"] == "2026-11-09"


def test_daily_report_failure_is_sent_not_swallowed(tmp_path):
    text = aw.daily_report_text("2026-11-09", str(tmp_path), str(tmp_path), "http://p:9090", lambda u: b"")
    assert text.startswith("[경보] 2026-11-09 하루 요약을 만들지 못했다")


def test_posted_text_has_no_emoji_like_codes():
    posts = []
    send = aw.webhook_poster("https://h", lambda u, b: posts.append(b), lambda _t: None)
    send("backend 재시작: 09:49:51, 10:14:44")
    assert json.loads(posts[0].decode("utf-8"))["text"] == "backend 재시작: `09:49:51`, `10:14:44`"
