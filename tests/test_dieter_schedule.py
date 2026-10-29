"""다이어터 하루 일정이 시간대별 표를 따르는지 확인한다."""

import math
from collections import Counter
from dataclasses import replace
from datetime import date

import pytest

from kkugi_load import config as config_module
from kkugi_load.config import hhmm
from kkugi_load.personas import PERSONAS, build_account

CONFIG = config_module.load({})
DAY = date(2026, 10, 28)
DIETER = PERSONAS["dieter"]
# 분포 확인에 쓰는 계정 수. 계정 번호 0 ~ ACCOUNTS-1
ACCOUNTS = 20_000


def _plan(index: int, config=CONFIG, day: date = DAY):
    return DIETER.plan_day(config, build_account(config, index), day)


def _tolerance(count: int, p: float) -> float:
    """기대값의 5%와 표준편차의 4배 중 큰 값. 밤 시간대는 기대값이 작아 5%만으로는 우연한 변동에 걸린다."""
    expected = count * p
    return max(0.05 * expected, 4 * math.sqrt(count * p * (1 - p)))


@pytest.fixture(scope="module")
def plans():
    """모든 계정이 다이어터인지 확인하고 하루 일정을 만든다."""
    accounts = [build_account(CONFIG, index) for index in range(ACCOUNTS)]
    assert {a.persona for a in accounts} == {"dieter"}
    return [DIETER.plan_day(CONFIG, account, DAY) for account in accounts]


def test_same_input_gives_same_plan():
    for index in range(50):
        assert _plan(index) == _plan(index)


def test_different_days_give_different_plans():
    assert any(_plan(i) != _plan(i, day=date(2026, 10, 29)) for i in range(20))


def test_planned_times_are_korean_time_on_that_day():
    for index in range(300):
        for session in _plan(index):
            assert session.at.tzinfo == CONFIG.tz
            assert session.at.date() == DAY
            assert session.inputs.planned_at == session.at


def test_one_session_per_hour_and_one_meal_per_window():
    meals = CONFIG.personas["dieter"]["meals"]
    for index in range(1000):
        plan = _plan(index)
        assert [p.at for p in plan] == sorted(p.at for p in plan)
        hours = [p.at.hour for p in plan]
        assert len(hours) == len(set(hours))
        logged = Counter(p.inputs.meal_type for p in plan if p.session == "meal_log")
        assert all(n == 1 for n in logged.values())
        for session in plan:
            if session.session != "meal_log":
                assert (session.session, session.name) == ("view", "dieter_view")
                continue
            window = meals[session.inputs.meal_type]
            assert hhmm(window["from"]) <= session.at.time() < hhmm(window["to"])
            # 식사 기록은 그 끼니 창에서 그날 처음 연 세션이다
            earlier = [p for p in plan if p.at < session.at and hhmm(window["from"]) <= p.at.time()]
            assert not earlier


def test_meal_decision_does_not_depend_on_other_hours():
    """저녁 창의 표만 0으로 바꿔도 나머지 시간대의 세션과 아침·점심 식사 기록은 그대로다."""
    dinner = (17, 18, 19)
    table = list(CONFIG.open_probability)
    for hour in dinner:
        table[hour] = 0.0
    no_dinner = replace(CONFIG, open_probability=tuple(table))
    for index in range(300):
        rest = [p for p in _plan(index) if p.at.hour not in dinner]
        assert _plan(index, no_dinner) == rest


def test_hourly_session_counts_follow_table(plans):
    # 페르소나 activity 1.0이고 계정 배수의 평균이 1이므로 계정 하나의 확률은 표의 값이다
    assert float(CONFIG.personas["dieter"]["activity"]) == 1.0
    counts = Counter(p.at.hour for plan in plans for p in plan)
    for hour, p in enumerate(CONFIG.open_probability):
        expected = ACCOUNTS * p
        assert abs(counts[hour] - expected) <= _tolerance(ACCOUNTS, p), (hour, counts[hour], expected)


def _chance_of_opening_in_window(window: dict) -> float:
    """끼니 창 안의 시간대 중 하나 이상 열 확률. 계정 배수의 비중으로 평균한다."""
    start, end = hhmm(window["from"]).hour, hhmm(window["to"]).hour
    total = sum(w for _, w in CONFIG.activity_multipliers)
    chance = 0.0
    for multiplier, weight in CONFIG.activity_multipliers:
        none_open = math.prod(1 - CONFIG.open_probability[h] * multiplier for h in range(start, end))
        chance += weight / total * (1 - none_open)
    return chance


def test_meal_counts_follow_window_and_probability(plans):
    counts = Counter(p.inputs.meal_type for plan in plans for p in plan if p.session == "meal_log")
    for meal_type, window in CONFIG.personas["dieter"]["meals"].items():
        # 끼니 창이 시각 단위로 시간대 경계에 맞춰 있어야 위 계산이 맞다
        assert hhmm(window["from"]).minute == 0 and hhmm(window["to"]).minute == 0
        p = _chance_of_opening_in_window(window) * float(window["probability"])
        expected = ACCOUNTS * p
        assert abs(counts[meal_type] - expected) <= _tolerance(ACCOUNTS, p), (meal_type, counts[meal_type], expected)
