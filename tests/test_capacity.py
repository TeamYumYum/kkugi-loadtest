from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from kkugi_load import config as config_module
from kkugi_load.capacity import Capacity, estimate, session_seconds, shortage_warning
from kkugi_load.personas import PERSONAS, build_account
from kkugi_load.personas.base import PlannedSession
from kkugi_load.runner import SESSIONS
from kkugi_load.scheduler import REALTIME, Scheduler

CONFIG = config_module.load({})
TZ = CONFIG.tz
DAY = date(2026, 10, 28)


def _session(hour: int, kind: str, minute: int = 0) -> PlannedSession:
    return PlannedSession(0, f"test_{kind}", datetime.combine(DAY, time(hour, minute), TZ), kind, None)


def test_meal_log_length_follows_think_time_and_items():
    items = int(CONFIG.personas["dieter"]["items_per_meal"])
    search = (CONFIG.search_think[0] + CONFIG.search_think[1]) / 2
    select = (CONFIG.select_think[0] + CONFIG.select_think[1]) / 2
    assert session_seconds(CONFIG, "meal_log") == pytest.approx(items * (search + select) + 1)
    # 기본 설정: 2 × (4 + 2.5) + 1
    assert session_seconds(CONFIG, "meal_log") == pytest.approx(14)


def test_view_length_is_one_second():
    assert session_seconds(CONFIG, "view") == 1


def test_every_session_kind_has_a_length():
    # 새 세션 종류를 더하고 capacity.py를 고치지 않으면 여기서 걸린다
    for kind in SESSIONS:
        assert session_seconds(CONFIG, kind) > 0


def test_unknown_session_kind_is_an_error():
    with pytest.raises(ValueError, match="nap"):
        session_seconds(CONFIG, "nap")
    with pytest.raises(ValueError):
        estimate(CONFIG, [_session(12, "nap")])


def test_estimate_picks_busiest_hour_by_session_seconds():
    meal = session_seconds(CONFIG, "meal_log")
    planned = (
        # 7시: 식사 기록 900개 → 900 × 14 / 3600 = 3.5
        [_session(7, "meal_log", m % 60) for m in range(900)]
        # 12시: 식사 기록 600개 + 조회 3,600개 → 600 × 14 / 3600 + 3600 × 1 / 3600 = 2.333 + 1 = 3.333
        + [_session(12, "meal_log") for _ in range(600)]
        + [_session(12, "view", m % 60) for m in range(3600)]
    )
    # 12시는 세션이 더 많아도 조회가 짧아 7시보다 덜 바쁘다
    capacity = estimate(CONFIG, planned)
    assert capacity.busiest_hour == 7
    assert capacity.busiest_concurrency == pytest.approx(900 * meal / 3600)
    assert capacity.busiest_concurrency == pytest.approx(3.5)
    # 3.5 × 2 = 7
    assert capacity.required_users == 7
    assert capacity.sessions == 900 + 600 + 3600


def test_estimate_rounds_up_after_margin():
    # 18시 식사 기록 100개 → 100 × 14 / 3600 = 0.389, × 2 = 0.778 → 1
    capacity = estimate(CONFIG, [_session(18, "meal_log") for _ in range(100)])
    assert capacity.busiest_hour == 18
    assert capacity.required_users == 1


def test_estimate_uses_local_hour():
    # UTC 3시는 한국 시간 12시
    utc = PlannedSession(0, "test", datetime(2026, 10, 28, 3, 0, tzinfo=ZoneInfo("UTC")), "view", None)
    assert estimate(CONFIG, [utc]).busiest_hour == 12


def test_empty_plan_needs_no_users():
    capacity = estimate(CONFIG, [])
    assert capacity == Capacity(required_users=0, busiest_hour=None, busiest_concurrency=0.0, sessions=0)
    assert shortage_warning(capacity, 0) is None


def test_shortage_warning_only_when_users_are_fewer():
    capacity = Capacity(required_users=7, busiest_hour=7, busiest_concurrency=3.5, sessions=900)
    message = shortage_warning(capacity, 6)
    assert message is not None
    assert "6" in message and "7" in message and "7시" in message
    assert shortage_warning(capacity, 7) is None
    assert shortage_warning(capacity, 100) is None


def test_scheduler_keeps_capacity_of_remaining_plan_and_calls_back():
    seen = []
    accounts = [build_account(CONFIG, i) for i in range(200)]
    # 13시에 켰으므로 그 전 세션은 제외되고 남은 일정만 센다
    start = datetime.combine(DAY, time(13, 0), TZ)
    scheduler = Scheduler(
        CONFIG, accounts, REALTIME, timedelta(minutes=30), lambda: start, on_plan=seen.append,
    )
    assert scheduler.capacity is None
    assert scheduler.plan_if_needed() is True
    assert scheduler.plan_if_needed() is False
    assert seen == [scheduler.capacity]
    remaining = [
        p for a in accounts for p in PERSONAS[a.persona].plan_day(CONFIG, a, DAY)
        if p.at >= start - timedelta(minutes=30)
    ]
    assert scheduler.capacity == estimate(CONFIG, remaining)
    assert scheduler.capacity.sessions == scheduler.pending()
