"""다이어터 하루 일정이 시간대별 표를 따르는지 확인한다."""

import math
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta

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
                assert (session.session, session.name) in {("view", "dieter_view"), ("weight_log", "dieter_weight"), ("workout", "dieter_workout")}
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
        # 끼니 칼로리 몫(kcal_share)은 그날 기록하는 끼니에 따라 다시 나누므로 비교에서 뺀다
        def strip(plan):
            return [replace(p, inputs=replace(p.inputs, kcal_share=0.0)) if p.session == "meal_log" else p for p in plan]
        rest = [p for p in _plan(index) if p.at.hour not in dinner]
        assert strip(_plan(index, no_dinner)) == strip(rest)


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


def test_meal_edit_changes_one_amount_at_edit_probability(plans):
    meals = [p.inputs for plan in plans for p in plan if p.session == "meal_log"]
    edited = [m for m in meals if m.misjudge is not None]
    for meal in edited:
        index, factor = meal.misjudge
        assert 0 <= index < len(meal.splits) and 0.5 <= factor < 1.5
    p = float(CONFIG.personas["dieter"]["edit_probability"])
    assert abs(len(edited) - len(meals) * p) <= _tolerance(len(meals), p)


def test_one_morning_weight_in_window_with_drifted_value(plans):
    weight = CONFIG.personas["dieter"]["body_weight"]
    start, end = hhmm(weight["from"]), hhmm(weight["to"])
    sessions = 0
    for index, plan in enumerate(plans[:2000]):
        records = [p for p in plan if p.session == "weight_log"]
        assert len(records) <= 1
        for record in records:
            sessions += 1
            assert start <= record.at.time() < end
            # 기록 세션보다 앞선 창 안의 조회 세션은 없다. 체중 기록은 창의 첫 조회 세션이다
            assert not [p for p in plan if p.session == "view" and start <= p.at.time() < record.at.time()]
            days = (DAY - date.fromisoformat(weight["reference_date"])).days
            trend = build_account(CONFIG, index).profile.body.weight_kg + days * float(weight["trend_kg_per_day"])
            # 흔들림 범위에 0.1kg 반올림 오차를 더한다
            assert abs(record.inputs.weight_kg - trend) <= float(weight["noise_kg"]) + 0.05 + 1e-9
    assert sessions > 0


def test_meal_inputs_keep_values_from_before_edit_was_added():
    """수정 값은 해시 값의 뒤에 붙였으므로 기록 여부, 검색어, 양, 고르기 값은 그대로다."""
    from kkugi_load.accounts.identity import seeded_uniforms
    settings = CONFIG.personas["dieter"]
    items = int(settings["items_per_meal"])
    for index in range(200):
        for session in _plan(index):
            if session.session != "meal_log":
                continue
            meal = session.inputs
            old = seeded_uniforms(CONFIG.seed, "meal", index, DAY.isoformat(), meal.meal_type, count=1 + 3 * items)
            assert meal.picks == tuple(int(x * 1000) for x in old[1 + 2 * items:])


def test_evening_workout_follows_weekly_target(plans):
    """운동은 운동 창의 첫 조회 세션이고, 계정 값은 날짜와 관계없이 같고, 하루 비율은 목표 / 7을 넘지 않는다."""
    from kkugi_load.sessions.workout import (
        BODY_PARTS, EXERCISES_PER_ROUTINE, FEMALE_STRENGTH, MISSING_EXERCISES, PARTS_PER_WORKOUT,
        POOL_SEEDS_PER_PART, SETS_PER_EXERCISE,
    )
    workout = CONFIG.personas["dieter"]["workout"]
    start, end = hhmm(workout["from"]), hhmm(workout["to"])
    sessions = 0
    targets, customs, missing, day_parts = [], [], [], set()
    for index, plan in enumerate(plans[:2000]):
        records = [p for p in plan if p.session == "workout"]
        assert len(records) <= 1
        for record in records:
            sessions += 1
            assert start <= record.at.time() < end
            assert not [p for p in plan if p.session == "view" and start <= p.at.time() < record.at.time()]
            inputs = record.inputs
            assert workout["weekly_target_min"] <= inputs.weekly_target <= workout["weekly_target_max"]
            assert 1 <= inputs.routine_count <= workout["routines_max"]
            assert len(inputs.routine_seeds) == len(BODY_PARTS) + EXERCISES_PER_ROUTINE
            assert len(inputs.pool_seeds) == len(BODY_PARTS) * POOL_SEEDS_PER_PART
            assert len(inputs.set_rolls) == EXERCISES_PER_ROUTINE * SETS_PER_EXERCISE
            assert workout["duration_minutes_min"] <= inputs.duration_minutes <= workout["duration_minutes_max"]
            assert inputs.browse_body_part in (None, *BODY_PARTS)
            assert inputs.reps_range == (workout["reps_min"], workout["reps_max"])
            assert len(inputs.free_seeds) == EXERCISES_PER_ROUTINE
            assert len(inputs.day_parts) == len(set(inputs.day_parts)) == PARTS_PER_WORKOUT
            assert set(inputs.day_parts) <= set(BODY_PARTS)
            assert inputs.missing_keyword is None or inputs.missing_keyword in MISSING_EXERCISES
            missing.append(inputs.missing_keyword is not None)
            day_parts.add(inputs.day_parts)
            profile = build_account(CONFIG, index).profile.body
            assert inputs.body_weight_kg == profile.weight_kg
            gender = FEMALE_STRENGTH if profile.gender == "FEMALE" else 1.0
            assert 0.8 - 1e-9 <= inputs.strength / (workout["strength"] * gender) <= 1.2 + 1e-9
            customs.append(inputs.custom_routine)
            targets.append(inputs.weekly_target)
            # 계정 고정 값은 다른 날에도 같다
            other = [p for p in _plan(index, day=date(2026, 10, 29)) if p.session == "workout"]
            for o in other:
                fixed = ("weekly_target", "routine_count", "routine_seeds", "custom_routine", "strength", "pool_seeds")
                assert [getattr(o.inputs, k) for k in fixed] == [getattr(inputs, k) for k in fixed]
    assert sessions > 0
    assert sessions <= 2000 * workout["weekly_target_max"] / 7
    assert set(targets) == set(range(workout["weekly_target_min"], workout["weekly_target_max"] + 1))
    p = float(workout["custom_routine_probability"])
    assert abs(sum(customs) - len(customs) * p) <= _tolerance(len(customs), p)
    p = float(workout["missing_exercise_probability"])
    assert abs(sum(missing) - len(missing) * p) <= _tolerance(len(missing), p)
    # 오늘 부위는 사람과 날짜마다 다르다. 부위 6개에서 순서 있게 2개를 고르는 30가지가 거의 다 나온다
    assert len(day_parts) >= 25


def test_day_intake_is_split_over_logged_meals(plans):
    """그날 기록하는 식사 칼로리 몫의 합은 끼니 수와 관계없이 하루 섭취 비율이다."""
    for index, plan in enumerate(plans[:2000]):
        meals = [p.inputs for p in plan if p.session == "meal_log"]
        if not meals:
            continue
        ratio = DIETER.intake_ratio(CONFIG, build_account(CONFIG, index), DAY)
        assert abs(sum(m.kcal_share for m in meals) - ratio) < 1e-9


def test_half_keep_the_goal_90_percent_and_half_60_percent():
    """계정의 무리는 고정이고 비중은 절반씩이다. 무리마다 목표를 지키는 날의 비율이 achieve에 가깝다."""
    intake = CONFIG.personas["dieter"]["intake"]
    low, high = intake["achieved_ratio"]
    days = [date(2026, 11, 1) + timedelta(days=d) for d in range(30)]
    kept = {0.9: [0, 0], 0.6: [0, 0]}
    for index in range(1000):
        account = build_account(CONFIG, index)
        achieve = DIETER.adherence(CONFIG, account)
        assert achieve == DIETER.adherence(CONFIG, account)
        for day in days:
            ratio = DIETER.intake_ratio(CONFIG, account, day)
            assert ratio == DIETER.intake_ratio(CONFIG, account, day)
            kept[achieve][0] += low <= ratio <= high
            kept[achieve][1] += 1
    accounts = {a: n / len(days) for a, (_, n) in kept.items()}
    assert abs(accounts[0.9] - 500) <= _tolerance(1000, 0.5)
    for achieve, (hit, n) in kept.items():
        assert abs(hit - n * achieve) <= _tolerance(n, achieve), (achieve, hit / n)


def test_missed_days_are_outside_the_judged_range():
    """못 지키는 날은 서버의 달성 범위(0.9-1.1) 밖이고, missed_over 비율만큼 많이 먹는다."""
    intake = CONFIG.personas["dieter"]["intake"]
    over = under = 0
    for index in range(500):
        account = build_account(CONFIG, index)
        for d in range(30):
            ratio = DIETER.intake_ratio(CONFIG, account, date(2026, 11, 1) + timedelta(days=d))
            if 0.9 <= ratio <= 1.1:
                assert intake["achieved_ratio"][0] <= ratio <= intake["achieved_ratio"][1]
            elif ratio > 1.1:
                over += 1
            else:
                under += 1
    p = float(intake["missed_over"])
    assert abs(over - (over + under) * p) <= _tolerance(over + under, p)


@pytest.mark.parametrize("change, message", [
    ({"adherence_groups": []}, "adherence_groups"),
    ({"adherence_groups": [{"weight": 1, "achieve": 1.5}]}, "adherence_groups"),
    ({"intake": {"achieved_ratio": [0.85, 1.0], "missed_low": [0.6, 0.85], "missed_high": [1.15, 1.4],
                 "missed_over": 0.3}}, "achieved_ratio"),
    ({"intake": {"achieved_ratio": [0.93, 1.07], "missed_low": [0.6, 0.95], "missed_high": [1.15, 1.4],
                 "missed_over": 0.3}}, "missed_low"),
])
def test_intake_settings_are_validated(change, message):
    from kkugi_load.config import read_intake
    settings = {**CONFIG.personas["dieter"], **change}
    with pytest.raises(ValueError, match=message):
        read_intake("dieter", settings)


def test_meal_share_is_required():
    from kkugi_load.config import read_intake
    settings = dict(CONFIG.personas["dieter"])
    settings["meals"] = {**settings["meals"], "LUNCH": {k: v for k, v in settings["meals"]["LUNCH"].items() if k != "share"}}
    with pytest.raises(ValueError, match="share"):
        read_intake("dieter", settings)
