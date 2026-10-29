from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from kkugi_load import config as config_module
from kkugi_load.config import Pool
from kkugi_load.personas import PERSONAS, build_account
from kkugi_load.scheduler import IMMEDIATE, REALTIME, Due, Missed, Scheduler

CONFIG = config_module.load({})
TZ = CONFIG.tz
DAY = date(2026, 10, 28)
LATENESS = timedelta(minutes=30)


class Clock:
    def __init__(self, now: datetime):
        self.value = now

    def __call__(self) -> datetime:
        return self.value


def _active_accounts(count: int):
    """오늘 세션이 2개 이상인 계정만 고른다."""
    picked = []
    for index in range(1000):
        account = build_account(CONFIG, index)
        if len(PERSONAS[account.persona].plan_day(CONFIG, account, DAY)) >= 2:
            picked.append(account)
        if len(picked) == count:
            return picked
    raise AssertionError("활동 계정을 찾지 못함")


def _plan(account):
    return PERSONAS[account.persona].plan_day(CONFIG, account, DAY)


def _drain(scheduler):
    """지금 실행할 수 있는 세션을 모두 꺼내고 바로 끝낸다."""
    out = []
    while (item := scheduler.next()) is not None:
        out.append(item)
        if isinstance(item, Due):
            scheduler.release(item.planned.account_index)
    return out


def test_realtime_runs_sessions_at_planned_time_in_order():
    accounts = _active_accounts(3)
    clock = Clock(datetime.combine(DAY, datetime.min.time(), TZ))
    scheduler = Scheduler(CONFIG, accounts, REALTIME, LATENESS, clock)
    assert scheduler.next() is None
    all_planned = sorted((p for a in accounts for p in _plan(a)), key=lambda p: p.at)

    started = []
    for planned in all_planned:
        clock.value = planned.at
        started += [item.planned for item in _drain(scheduler)]
    assert started == all_planned
    assert scheduler.pending() == 0


def test_realtime_drops_sessions_that_passed_before_start():
    account = _active_accounts(1)[0]
    plan = _plan(account)
    clock = Clock(plan[-1].at + LATENESS + timedelta(minutes=1))
    scheduler = Scheduler(CONFIG, [account], REALTIME, LATENESS, clock)
    assert _drain(scheduler) == []
    assert scheduler.dropped_before_start == len(plan)


def test_realtime_reports_sessions_started_too_late_as_missed():
    account = _active_accounts(1)[0]
    first = _plan(account)[0]
    clock = Clock(first.at - timedelta(minutes=1))
    scheduler = Scheduler(CONFIG, [account], REALTIME, LATENESS, clock)
    assert scheduler.next() is None
    clock.value = first.at + LATENESS + timedelta(seconds=1)
    item = scheduler.next()
    assert isinstance(item, Missed) and item.planned == first


def test_one_account_never_runs_two_sessions_at_once():
    account = _active_accounts(1)[0]
    scheduler = Scheduler(CONFIG, [account], IMMEDIATE, LATENESS, Clock(datetime.combine(DAY, datetime.min.time(), TZ)))
    first = scheduler.next()
    assert isinstance(first, Due)
    assert scheduler.next() is None
    scheduler.release(account.index)
    assert isinstance(scheduler.next(), Due)


def test_immediate_runs_everything_now_and_finishes():
    accounts = _active_accounts(3)
    clock = Clock(datetime.combine(DAY, datetime.min.time(), TZ))
    scheduler = Scheduler(CONFIG, accounts, IMMEDIATE, LATENESS, clock)
    items = _drain(scheduler)
    assert len(items) == sum(len(_plan(a)) for a in accounts)
    assert all(isinstance(i, Due) and i.delay == timedelta(0) for i in items)
    assert scheduler.finished()


def test_realtime_plans_next_day_after_midnight():
    account = _active_accounts(1)[0]
    clock = Clock(datetime.combine(DAY, datetime.max.time(), TZ) - timedelta(minutes=1))
    scheduler = Scheduler(CONFIG, [account], REALTIME, LATENESS, clock)
    _drain(scheduler)
    clock.value = datetime.combine(DAY + timedelta(days=1), datetime.min.time(), TZ)
    scheduler.next()
    tomorrow = PERSONAS[account.persona].plan_day(CONFIG, account, DAY + timedelta(days=1))
    assert scheduler.pending() == len(tomorrow)


def test_pool_hours_limit_planned_sessions():
    accounts = _active_accounts(5)
    lunch_only = Pool("peak", 0, 5, ((time(11, 30), time(13, 30)),))
    clock = Clock(datetime.combine(DAY, datetime.min.time(), TZ))
    scheduler = Scheduler(CONFIG, accounts, IMMEDIATE, LATENESS, clock, lunch_only)
    items = _drain(scheduler)
    # 점심 창 안이므로 점심 기록이나 조회만 남는다
    assert items and all(i.planned.name in ("dieter_lunch", "dieter_view") for i in items)
    assert all(time(11, 30) <= i.planned.at.time() < time(13, 30) for i in items)
    expected = [p for a in accounts for p in _plan(a) if time(11, 30) <= p.at.time() < time(13, 30)]
    assert len(items) == len(expected)


# --- 배율과 일정 다시 만들기 ---

def _start_of_day() -> datetime:
    return datetime.combine(DAY, datetime.min.time(), TZ)


def _kept_at(accounts, scale: float) -> set:
    scheduler = Scheduler(CONFIG, accounts, IMMEDIATE, LATENESS, Clock(_start_of_day()))
    scheduler.set_scale(scale)
    return {(i.planned.account_index, i.planned.at) for i in _drain(scheduler)}


def test_scale_keeps_the_same_sessions_and_lower_scale_keeps_a_subset():
    accounts = _active_accounts(40)
    total = sum(len(_plan(a)) for a in accounts)
    low, high = _kept_at(accounts, 0.3), _kept_at(accounts, 0.7)
    assert low == _kept_at(accounts, 0.3)
    assert low < high
    assert len(_kept_at(accounts, 1.0)) == total
    # 배율만큼 남는다(세션 수가 적어 넉넉하게 본다)
    assert 0.15 * total < len(low) < 0.45 * total


def test_scale_zero_runs_nothing_and_counts_scaled_out():
    accounts = _active_accounts(3)
    scheduler = Scheduler(CONFIG, accounts, IMMEDIATE, LATENESS, Clock(_start_of_day()))
    scheduler.set_scale(0)
    assert _drain(scheduler) == []
    total = sum(len(_plan(a)) for a in accounts)
    assert scheduler.scaled_out == total
    assert scheduler.take_scaled_out() == total
    assert scheduler.take_scaled_out() == 0
    # 거른 세션은 놓친 것이 아니고 남아 있지도 않다
    assert scheduler.finished()


def test_raising_scale_revives_only_sessions_not_yet_due():
    accounts = _active_accounts(5)
    plan = sorted((p for a in accounts for p in _plan(a)), key=lambda p: p.at)
    middle = plan[len(plan) // 2].at
    clock = Clock(_start_of_day())
    scheduler = Scheduler(CONFIG, accounts, REALTIME, LATENESS, clock)
    scheduler.set_scale(0)
    clock.value = middle
    assert _drain(scheduler) == []
    scheduler.set_scale(1)
    started = []
    for planned in plan:
        clock.value = max(planned.at, middle)
        started += [item.planned for item in _drain(scheduler)]
    assert started == [p for p in plan if p.at > middle]


def test_invalid_scale_is_rejected():
    scheduler = Scheduler(CONFIG, _active_accounts(1), IMMEDIATE, LATENESS, Clock(_start_of_day()))
    for bad in (-0.1, 1.1, float("nan"), "abc"):
        with pytest.raises(ValueError):
            scheduler.set_scale(bad)
    assert scheduler.scale == 1.0


def test_replan_does_not_add_sessions_before_now_or_already_taken():
    accounts = _active_accounts(5)
    plan = sorted((p for a in accounts for p in _plan(a)), key=lambda p: p.at)
    clock = Clock(_start_of_day())
    scheduler = Scheduler(CONFIG, accounts, REALTIME, LATENESS, clock)
    started = []
    middle = plan[len(plan) // 2].at
    for planned in plan:
        if planned.at > middle:
            break
        clock.value = planned.at
        started += [item.planned for item in _drain(scheduler)]
    # 다시 만든 시각은 마지막으로 실행한 세션과 같은 시각이다. 그 세션이 다시 들어오면 안 된다
    scheduler.replan(CONFIG)
    assert scheduler.pending() == len([p for p in plan if p.at > middle])
    for planned in plan:
        clock.value = max(planned.at, middle)
        started += [item.planned for item in _drain(scheduler)]
    assert started == plan


def test_replan_in_immediate_mode_skips_sessions_already_taken():
    accounts = _active_accounts(3)
    scheduler = Scheduler(CONFIG, accounts, IMMEDIATE, LATENESS, Clock(_start_of_day()))
    first = scheduler.next()
    assert isinstance(first, Due)
    scheduler.release(first.planned.account_index)
    scheduler.replan(CONFIG)
    rest = [item.planned for item in _drain(scheduler)]
    assert first.planned not in rest
    assert len(rest) + 1 == sum(len(_plan(a)) for a in accounts)


def test_replan_keeps_running_session_busy_and_recomputes_capacity():
    account = _active_accounts(1)[0]
    plans = []
    scheduler = Scheduler(CONFIG, [account], IMMEDIATE, LATENESS, Clock(_start_of_day()), on_plan=plans.append)
    running = scheduler.next()
    assert isinstance(running, Due) and len(plans) == 1
    # 모든 시간대의 확률을 0으로 바꾼 설정으로 다시 만든다. 남은 세션이 없어진다
    quiet = replace(CONFIG, open_probability=tuple(0.0 for _ in range(24)))
    scheduler.replan(quiet)
    assert scheduler.config is quiet
    assert scheduler.pending() == 0
    assert len(plans) == 2 and plans[-1].sessions == 0 and scheduler.capacity == plans[-1]
    # 실행 중인 세션은 여전히 그 계정을 잡고 있다
    assert running.planned.account_index in scheduler._busy
    scheduler.release(running.planned.account_index)
    assert scheduler.finished()
