"""Worker 하나가 맡은 계정들의 세션 일정표.

페르소나가 계정마다 하루 일정을 정하고, 실행기(Locust User)는 시각이 된 세션을 여기서 꺼내 실행한다.
전체 RPS를 정하지 않는다. 트래픽은 계정 수와 페르소나의 일정에서 나온다.

- realtime: 계획 시각이 되면 실행한다. 발생기를 켜기 전에 지난 세션은 버린다. 늦어도 max_lateness
  안이면 실행하고, 그보다 늦거나 날짜가 바뀌면 놓친 세션으로 센다.
- immediate: 오늘의 세션을 시각과 관계없이 바로 실행한다. 로컬 검증용이다.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from kkugi_load.accounts.identity import Account
from kkugi_load.capacity import Capacity, estimate
from kkugi_load.config import Config, Pool
from kkugi_load.personas import PERSONAS
from kkugi_load.personas.base import PlannedSession

log = logging.getLogger("kkugi")

REALTIME, IMMEDIATE = "realtime", "immediate"
# 같은 계정의 세션이 진행 중이면 이만큼 뒤로 미룬다
BUSY_RETRY = timedelta(seconds=30)


@dataclass(order=True)
class _Entry:
    at: datetime
    seq: int
    planned: PlannedSession = field(compare=False)


@dataclass
class Due:
    planned: PlannedSession
    # 계획 시각보다 늦게 시작한 정도. immediate에서는 0
    delay: timedelta


@dataclass
class Missed:
    planned: PlannedSession
    reason: str


class Scheduler:
    def __init__(
        self, config: Config, accounts: list[Account], mode: str, max_lateness: timedelta,
        now: Callable[[], datetime], pool: Pool | None = None,
        on_plan: Callable[[Capacity], None] | None = None,
    ):
        if mode not in (REALTIME, IMMEDIATE):
            raise ValueError(f"알 수 없는 일정 방식: {mode}")
        self.config = config
        self.accounts = accounts
        self.mode = mode
        self.max_lateness = max_lateness
        self.now = now
        self.pool = pool
        self._heap: list[_Entry] = []
        self._seq = itertools.count()
        self._busy: set[int] = set()
        self._planned_day: date | None = None
        # 발생기를 켜기 전에 지나 버려 실행하지 않은 세션 수
        self.dropped_before_start = 0
        # 가장 최근에 만든 일정의 실행기 필요량(capacity.py). 일정을 만들기 전에는 None
        self.capacity: Capacity | None = None
        # 새 일정을 만들 때마다 실행기 필요량을 넘겨 부른다
        self.on_plan = on_plan

    def _push(self, at: datetime, planned: PlannedSession) -> None:
        heapq.heappush(self._heap, _Entry(at, next(self._seq), planned))

    def _plan(self, day: date) -> None:
        now = self.now()
        kept: list[PlannedSession] = []
        dropped = 0
        for account in self.accounts:
            for planned in PERSONAS[account.persona].plan_day(self.config, account, day):
                if self.pool and not self.pool.allows(planned.at.time()):
                    continue
                if self.mode == REALTIME and planned.at < now - self.max_lateness:
                    dropped += 1
                    continue
                kept.append(planned)
        # 그날 남은 일정만 센다. 시작 전에 지난 세션은 실행하지 않으므로 실행기가 필요 없다.
        # 모르는 세션 종류면 여기서 오류가 나므로 일정에 넣기 전에 계산한다
        self.capacity = estimate(self.config, kept)
        for planned in kept:
            self._push(now if self.mode == IMMEDIATE else planned.at, planned)
        self._planned_day = day
        self.dropped_before_start += dropped
        log.info(
            "%s 일정: 세션 %d개 예정, 시작 전에 지난 %d개 제외. 필요 실행기 %d개(가장 바쁜 시간대 %s시, 평균 동시 세션 %.1f)",
            day, len(kept), dropped, self.capacity.required_users, self.capacity.busiest_hour,
            self.capacity.busiest_concurrency,
        )
        if self.on_plan is not None:
            self.on_plan(self.capacity)

    def plan_if_needed(self) -> bool:
        """오늘 일정이 아직 없거나(realtime에서) 날짜가 바뀌었으면 새로 만든다. 만들었으면 True."""
        now = self.now()
        if self._planned_day is None or (self.mode == REALTIME and now.date() > self._planned_day):
            self._plan(now.date())
            return True
        return False

    def next(self) -> Due | Missed | None:
        """실행할 세션을 꺼낸다. 받은 쪽은 실행이 끝나면 release를 부른다. 실행할 것이 없으면 None."""
        self.plan_if_needed()
        now = self.now()

        postponed = []
        try:
            while self._heap and self._heap[0].at <= now:
                entry = heapq.heappop(self._heap)
                planned = entry.planned
                if planned.account_index in self._busy:
                    postponed.append(planned)
                    continue
                if self.mode == REALTIME:
                    if planned.at.date() != now.date():
                        return Missed(planned, "날짜가 바뀜")
                    if now - planned.at > self.max_lateness:
                        return Missed(planned, "허용 지연 초과")
                self._busy.add(planned.account_index)
                delay = timedelta(0) if self.mode == IMMEDIATE else max(now - planned.at, timedelta(0))
                return Due(planned, delay)
            return None
        finally:
            for planned in postponed:
                self._push(now + BUSY_RETRY if self.mode == REALTIME else now, planned)

    def release(self, account_index: int) -> None:
        self._busy.discard(account_index)

    def finished(self) -> bool:
        """immediate에서 오늘의 세션을 모두 끝냈는가."""
        return self.mode == IMMEDIATE and self._planned_day is not None and not self._heap and not self._busy

    def pending(self) -> int:
        return len(self._heap)
