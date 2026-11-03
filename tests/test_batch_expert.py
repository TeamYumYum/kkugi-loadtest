"""4,000명 단계: 하루 몰아 기록형과 운동 숙련자. 기존 2,000명은 그대로 두고 새 풀에만 배정한다."""

import hashlib
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from kkugi_load import config as cm
from kkugi_load.capacity import session_seconds
from kkugi_load.config import hhmm
from kkugi_load.personas import PERSONAS, build_account
from kkugi_load.personas.workout_plan import weekly_days
from kkugi_load.sessions import batch_log
from kkugi_load.sessions.base import SessionSkipped
from kkugi_load.sessions.batch_log import BatchInputs

MONDAY = date(2026, 11, 2)
WEEK = [MONDAY + timedelta(days=d) for d in range(7)]
NEW_POOLS = {"batch-private": "batch", "batch-public": "batch", "expert-private": "expert", "expert-public": "expert"}
SAMPLE = 120


def _accounts(pool: str, limit: int = SAMPLE):
    config = cm.load({"KKUGI_POOL": pool})
    return config, [build_account(config, i) for i in range(config.pool.start, min(config.pool.end, config.pool.start + limit))]


def test_new_pools_hold_1000_each_without_touching_existing_ranges():
    seen = set()
    counts, routes = Counter(), Counter()
    for pool, persona in NEW_POOLS.items():
        config = cm.load({"KKUGI_POOL": pool})
        for index in range(config.pool.start, config.pool.end):
            assert index not in seen and index >= 2000
            seen.add(index)
            assert build_account(config, index).persona == persona
            counts[persona] += 1
            routes[persona, pool.split("-")[1]] += 1
    assert counts == {"batch": 1000, "expert": 1000}
    for persona in counts:
        assert (routes[persona, "private"], routes[persona, "public"]) == (950, 50)
    assert seen == set(range(30000, 31000)) | set(range(40000, 41000))


def test_beginner_profiles_and_plans_match_pr6_fingerprint():
    # PR #6의 운동 초보자 구현으로 1,000-1,999번의 두 날짜 전체를 계산한 값이다. 공용 주간 일정 함수로 옮긴 뒤에도 같아야 한다
    values = []
    for pool in ("beginner-private", "beginner-public"):
        config = cm.load({"KKUGI_POOL": pool})
        for index in range(config.pool.start, config.pool.end):
            account = build_account(config, index)
            for day in (MONDAY, date(2026, 11, 8)):
                values.append(repr((account, PERSONAS["beginner"].plan_day(config, account, day))))
    assert hashlib.sha256("\n".join(values).encode()).hexdigest() == "f950f883d0f581159da90547e9594f770f824cd13bbdba357b78cfdc0f04d6dc"


@pytest.mark.parametrize("pool", ["batch-private", "expert-private"])
def test_plans_are_repeatable_and_hold_one_session_per_hour(pool):
    config, accounts = _accounts(pool)
    persona = PERSONAS[accounts[0].persona]
    for account in accounts:
        for day in WEEK:
            plan = persona.plan_day(config, account, day)
            assert plan == persona.plan_day(config, account, day)
            hours = [p.at.hour for p in plan]
            assert len(hours) == len(set(hours))
            assert [p.at for p in plan] == sorted(p.at for p in plan)
            assert all(p.at.date() == day and p.at.tzinfo == config.tz for p in plan)


def test_batch_logs_the_day_at_night_with_meal_and_weight_times():
    config, accounts = _accounts("batch-private")
    settings = config.personas["batch"]
    night = settings["night"]
    nights = 0
    for account in accounts:
        assert account.profile.body.health_goal in ("LOSE", "MAINTAIN")
        for day in WEEK:
            plan = PERSONAS["batch"].plan_day(config, account, day)
            assert {p.session for p in plan} <= {"view", "batch_log"}
            batches = [p for p in plan if p.session == "batch_log"]
            assert len(batches) <= 1
            for p in batches:
                nights += 1
                assert p.name == "batch_night"
                assert hhmm(night["from"]) <= p.at.time() < hhmm(night["to"])
                inputs = p.inputs
                assert inputs.planned_at == p.at
                assert inputs.meals or inputs.weight is not None or inputs.workout is not None
                assert [m.planned_at for m in inputs.meals] == sorted(m.planned_at for m in inputs.meals)
                for meal in inputs.meals:
                    window = settings["meals"][meal.meal_type]
                    assert hhmm(window["from"]) <= meal.planned_at.time() < hhmm(window["to"])
                    assert meal.planned_at.date() == day
                if inputs.weight is not None:
                    assert inputs.weight.planned_at.date() == day and inputs.weight.planned_at < p.at
                if inputs.workout is not None:
                    workout = settings["workout"]
                    assert hhmm(workout["from"]) <= inputs.workout.planned_at.time() < hhmm(workout["to"])
    # 밤 기록 확률 0.95
    assert 0.9 < nights / (len(accounts) * len(WEEK)) <= 1.0


def test_batch_works_out_only_on_weekly_days_and_at_most_its_target():
    config, accounts = _accounts("batch-private")
    workout = config.personas["batch"]["workout"]
    for account in accounts:
        done = []
        for day in WEEK:
            for p in PERSONAS["batch"].plan_day(config, account, day):
                if p.session == "batch_log" and p.inputs.workout is not None:
                    done.append(day.weekday())
                    target = p.inputs.workout.weekly_target
                    assert workout["weekly_target_min"] <= target <= workout["weekly_target_max"]
                    assert day.weekday() in weekly_days(config, account.index, day, target, "batch_week")
        assert len(done) <= workout["weekly_target_max"]


def test_expert_works_out_exactly_its_weekly_target_in_the_evening():
    config, accounts = _accounts("expert-private")
    workout = config.personas["expert"]["workout"]
    targets = Counter()
    for account in accounts:
        assert account.profile.body.health_goal in ("GAIN", "MAINTAIN")
        sessions = [
            p for day in WEEK for p in PERSONAS["expert"].plan_day(config, account, day) if p.session == "workout"
        ]
        target = sessions[0].inputs.weekly_target
        targets[target] += 1
        assert len(sessions) == target
        assert len({p.at.date() for p in sessions}) == target
        for p in sessions:
            assert p.name == "expert_workout"
            assert hhmm(workout["from"]) <= p.at.time() < hhmm(workout["to"])
            assert p.inputs.planned_at == p.at
    assert set(targets) == {5, 6}


def test_expert_logs_meals_with_dieter_rules():
    config, accounts = _accounts("expert-private")
    names = Counter(p.name for account in accounts for day in WEEK for p in PERSONAS["expert"].plan_day(config, account, day))
    for meal in ("expert_breakfast", "expert_lunch", "expert_dinner", "expert_snack"):
        assert names[meal] > 0
    assert not any(name.startswith("dieter_") for name in names)


def _ctx():
    return object()


def test_batch_session_runs_parts_in_order_and_skips_recorded_ones(monkeypatch):
    calls = []

    def fake(kind, skip=False):
        def run(_ctx, inputs):
            calls.append((kind, inputs))
            if skip:
                raise SessionSkipped("이미 기록함")
            return {"kind": kind}
        return run

    monkeypatch.setattr(batch_log.meal_log, "run", lambda ctx, meal: fake(meal.meal_type, skip=meal.meal_type == "LUNCH")(ctx, meal))
    monkeypatch.setattr(batch_log.weight_log, "run", fake("WEIGHT"))
    monkeypatch.setattr(batch_log.workout, "run", fake("WORKOUT"))

    class Meal:
        def __init__(self, meal_type):
            self.meal_type = meal_type

    inputs = BatchInputs(
        planned_at=datetime(2026, 11, 2, 23, 10), meals=(Meal("BREAKFAST"), Meal("LUNCH"), Meal("DINNER")),
        weight="w", workout="x",
    )
    outcome = batch_log.run(_ctx(), inputs)
    assert [kind for kind, _ in calls] == ["BREAKFAST", "LUNCH", "DINNER", "WEIGHT", "WORKOUT"]
    assert outcome["skipped"] == ["LUNCH"]
    assert set(outcome["meals"]) == {"BREAKFAST", "DINNER"}
    assert outcome["weight"] == {"kind": "WEIGHT"} and outcome["workout"] == {"kind": "WORKOUT"}


def test_batch_session_is_skipped_only_when_every_part_was_recorded(monkeypatch):
    def skipped(_ctx, _inputs):
        raise SessionSkipped("이미 기록함")

    monkeypatch.setattr(batch_log.weight_log, "run", skipped)
    monkeypatch.setattr(batch_log.workout, "run", skipped)
    with pytest.raises(SessionSkipped):
        batch_log.run(_ctx(), BatchInputs(planned_at=datetime(2026, 11, 2, 23), meals=(), weight="w", workout="x"))


def test_batch_session_stops_at_the_first_failure(monkeypatch):
    calls = []

    def failing(_ctx, _inputs):
        calls.append("WEIGHT")
        raise RuntimeError("서버 오류")

    monkeypatch.setattr(batch_log.weight_log, "run", failing)
    monkeypatch.setattr(batch_log.workout, "run", lambda _ctx, _inputs: calls.append("WORKOUT"))
    with pytest.raises(RuntimeError):
        batch_log.run(_ctx(), BatchInputs(planned_at=datetime(2026, 11, 2, 23), meals=(), weight="w", workout="x"))
    assert calls == ["WEIGHT"]


def test_batch_session_length_adds_its_parts():
    config = cm.load({})
    length = session_seconds(config, "batch_log")
    assert session_seconds(config, "meal_log") * 2 < length < session_seconds(config, "meal_log") * 3 + 30


def _with(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_night_window_must_end_by_2330():
    text = open("config/default.toml", encoding="utf-8").read()
    bad = _with(text, '[personas.batch.night]\nprobability = 0.95\nfrom = "23:00"\nto = "23:30"',
                '[personas.batch.night]\nprobability = 0.95\nfrom = "23:00"\nto = "23:50"')
    with pytest.raises(ValueError, match="몰아 기록 창"):
        cm.loads(bad, {"KKUGI_POOL": "batch-private"})


def test_weekly_target_is_checked_for_every_persona_with_workouts():
    text = open("config/default.toml", encoding="utf-8").read()
    bad = _with(text, "weekly_target_min = 5\nweekly_target_max = 6", "weekly_target_min = 5\nweekly_target_max = 8")
    with pytest.raises(ValueError, match="expert의 주간 목표"):
        cm.loads(bad, {})
