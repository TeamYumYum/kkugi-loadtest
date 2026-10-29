from datetime import date, datetime, time, timedelta

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
