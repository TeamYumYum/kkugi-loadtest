"""8,000명 단계: 다이어터 추가분과 휴면 직전. 기존 4,000명은 그대로 두고 새 풀에만 배정한다."""

import tomllib
from collections import Counter
from datetime import date, timedelta

import pytest

from kkugi_load import config as cm
from kkugi_load.personas import PERSONAS, build_account

MONDAY = date(2026, 11, 2)
WEEK = [MONDAY + timedelta(days=d) for d in range(7)]
NEW_POOLS = {
    "dieter-extra-private": "dieter", "dieter-extra-public": "dieter",
    "dormant-private": "dormant", "dormant-public": "dormant",
}
EXISTING_POOLS = [
    "base", "public", "beginner-private", "beginner-public",
    "batch-private", "batch-public", "expert-private", "expert-public",
]
SAMPLE = 120


def _accounts(pool: str, limit: int = SAMPLE):
    config = cm.load({"KKUGI_POOL": pool})
    return config, [build_account(config, i) for i in range(config.pool.start, min(config.pool.end, config.pool.start + limit))]


def _ranges() -> dict[str, range]:
    with cm.DEFAULT_CONFIG.open("rb") as f:
        pools = tomllib.load(f)["pools"]
    return {name: range(p["start"], p["start"] + p["count"]) for name, p in pools.items()}


def _overlaps(a: range, b: range) -> bool:
    return a.start < b.stop and b.start < a.stop


def test_new_pools_hold_3800_dieters_and_200_dormant_split_95_5():
    seen = set()
    counts, routes = Counter(), Counter()
    for pool, persona in NEW_POOLS.items():
        config = cm.load({"KKUGI_POOL": pool})
        for index in range(config.pool.start, config.pool.end):
            assert index not in seen
            seen.add(index)
            assert build_account(config, index).persona == persona
            counts[persona] += 1
            routes[persona, pool.rsplit("-", 1)[1]] += 1
    assert counts == {"dieter": 3800, "dormant": 200}
    assert (routes["dieter", "private"], routes["dieter", "public"]) == (3610, 190)
    assert (routes["dormant", "private"], routes["dormant", "public"]) == (190, 10)
    assert seen == set(range(10000, 13800)) | set(range(50000, 50200))


def test_new_pools_do_not_overlap_existing_pools():
    ranges = _ranges()
    for new in NEW_POOLS:
        for old in EXISTING_POOLS:
            assert not _overlaps(ranges[new], ranges[old]), (new, old)


def test_peak_pools_moved_to_90000s_and_overlap_no_other_pool():
    ranges = _ranges()
    assert ranges["peak-1"] == range(90000, 91000)
    assert ranges["peak-2"] == range(91000, 92000)
    for peak in ("peak-1", "peak-2"):
        for name, other in ranges.items():
            if name != peak:
                assert not _overlaps(ranges[peak], other), (peak, name)


@pytest.mark.parametrize("pool", ["dormant-private", "dormant-public"])
def test_dormant_only_views_and_plans_are_repeatable(pool):
    config, accounts = _accounts(pool)
    persona = PERSONAS["dormant"]
    for account in accounts:
        assert account.profile.body.health_goal in ("LOSE", "MAINTAIN")
        for day in WEEK:
            plan = persona.plan_day(config, account, day)
            assert plan == persona.plan_day(config, account, day)
            assert all(p.session == "view" and p.name == "dormant_view" for p in plan)
            assert all(p.inputs.planned_at == p.at for p in plan)
            assert all(p.at.date() == day and p.at.tzinfo == config.tz for p in plan)
            assert [p.at for p in plan] == sorted(p.at for p in plan)


def test_dormant_opens_the_app_far_less_than_dieters():
    def sessions_per_account_day(pool: str) -> float:
        config, accounts = _accounts(pool, limit=190)
        persona = PERSONAS[accounts[0].persona]
        total = sum(len(persona.plan_day(config, account, day)) for account in accounts for day in WEEK)
        return total / (len(accounts) * len(WEEK))

    dormant = sessions_per_account_day("dormant-private")
    dieter = sessions_per_account_day("dieter-extra-private")
    # 활동 배수 0.1 대 1.0이라 기대값은 약 0.4 대 4.1이다
    assert 0 < dormant < dieter / 5
