"""기존 1,000명의 배정과 상태를 보존하고 새 운동 초보자 1,000명만 추가한다."""

import hashlib
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta

import pytest

from kkugi_load import config as cm
from kkugi_load.accounts.state import StateStore
from kkugi_load.capacity import estimate
from kkugi_load.personas import PERSONAS, build_account

DAY = date(2026, 11, 2)
POOLS = ("base", "public", "beginner-private", "beginner-public")


def test_exact_population_and_routes_without_reassigning_old_accounts():
    counts, routes, indexes = Counter(), Counter(), set()
    for name in POOLS:
        config = cm.load({"KKUGI_POOL": name})
        expected = "beginner" if name.startswith("beginner-") else "dieter"
        for index in range(config.pool.start, config.pool.end):
            assert index not in indexes
            indexes.add(index)
            account = build_account(config, index)
            assert account.persona == expected
            counts[account.persona] += 1
            routes[account.persona, "public" if name.endswith("public") else "private"] += 1
    assert counts == {"dieter": 1000, "beginner": 1000}
    assert indexes == set(range(2000))
    for name in counts:
        assert (routes[name, "private"], routes[name, "public"]) == (950, 50)


def test_dieter_profiles_and_plans_match_pr5_fingerprint():
    # PR #5의 다이어터 구현과 기존 설정으로 0-999번의 두 날짜 전체를 계산한 값이다.
    # 2026-11-09 식사 양을 칼로리로 정하도록 바꾸며 갱신했다. 그때 양 관련 필드(grams, edit_grams → kcal_share, splits,
    # misjudge)를 빼면 이전 값과 세션·시각·검색어·고르기 값이 모두 같음을 이전 커밋의 코드와 대조했다
    config = cm.load({})
    values = []
    for index in range(1000):
        account = build_account(config, index)
        for day in (DAY, date(2026, 11, 8)):
            values.append(repr((account, PERSONAS["dieter"].plan_day(config, account, day))))
    assert hashlib.sha256("\n".join(values).encode()).hexdigest() == "901fa0fa23530643209df6653b2af2bf4dad5dfc05ce19fc242ae0582e589328"


def test_existing_state_loads_without_identity_rotation(tmp_path):
    config = cm.load({"KKUGI_POOL": "public"})
    with closing(StateStore(tmp_path / "public.db")) as store:
        state = store.load(950, "dieter")
        state.generation, state.member_id, state.access_token, state.onboarded = 2, 12, "saved", True
        store.save(state)
    with closing(StateStore(tmp_path / "public.db")) as store:
        account = build_account(config, 950)
        assert store.load(950, account.persona) == state


def test_beginner_plan_is_repeatable_and_records_two_or_three_evening_workouts_per_week():
    config = cm.load({"KKUGI_POOL": "beginner-private"})
    for index in range(1000, 1050):
        account = build_account(config, index)
        assert account == build_account(config, index)
        assert account.profile.body.health_goal in ("MAINTAIN", "GAIN")
        assert 20 <= account.profile.body.weight_kg <= 300
        targets = set()
        for week in range(2):
            workouts = []
            for offset in range(7):
                day = DAY + timedelta(days=7 * week + offset)
                plan = PERSONAS["beginner"].plan_day(config, account, day)
                assert plan == PERSONAS["beginner"].plan_day(config, account, day)
                assert [p.at for p in plan] == sorted(p.at for p in plan)
                assert len({p.at.hour for p in plan}) == len(plan)
                for p in plan:
                    assert p.at.date() == day and p.at.tzinfo == config.tz and p.inputs.planned_at == p.at
                    assert p.session in ("view", "workout")
                    if p.session == "workout":
                        assert 18 <= p.at.hour < 22 and p.inputs.custom_routine
                        assert p.inputs.reps_range == (8, 12)
                        workouts.append(p)
            assert 2 <= len(workouts) <= 3
            assert {p.inputs.weekly_target for p in workouts} == {len(workouts)}
            targets.add(len(workouts))
        assert len(targets) == 1


def test_fixed_assignment_is_not_changed_by_future_weights():
    config = cm.load({})
    changed = replace(config, personas=config.personas | {"future": {"weight": 1000}})
    assert all(build_account(changed, i).persona == "dieter" for i in range(1000))


@pytest.mark.parametrize("old,new", [
    ('persona = "beginner"', 'persona = "missing"'),
    ("weekly_target_max = 3", "weekly_target_max = 8"),
    ('from = "18:00"', 'from = "23:00"'),
])
def test_invalid_pool_or_beginner_schedule_stops_start(old, new):
    text = cm.read_text({})
    with pytest.raises(ValueError):
        cm.loads(text.replace(old, new), {"KKUGI_POOL": "beginner-private"})


def test_pool_assignment_cannot_change_on_reload():
    text = cm.read_text({})
    assert cm.frozen_changes(text, text.replace('persona = "beginner"', 'persona = "dieter"')) == ["pools"]


def test_four_workers_capacity_fits_example_user_count():
    from pathlib import Path
    users = int(next(line.split("=")[1] for line in Path("deploy/.env.example").read_text(encoding="utf-8").splitlines() if line.startswith("KKUGI_USERS=")))
    for name in POOLS:
        config = cm.load({"KKUGI_POOL": name})
        accounts = [build_account(config, i) for i in range(config.pool.start, config.pool.end)]
        for day in (DAY, DAY + timedelta(days=6)):
            plan = [p for a in accounts for p in PERSONAS[a.persona].plan_day(config, a, day)]
            assert estimate(config, plan).required_users <= users // len(POOLS)
