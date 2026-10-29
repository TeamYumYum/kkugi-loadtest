"""Worker 하나가 맡은 계정들의 세션 일정표.

페르소나가 계정마다 하루 일정을 정하고, 실행기(Locust User)는 시각이 된 세션을 여기서 꺼내 실행한다.
전체 RPS를 정하지 않는다. 트래픽은 계정 수와 페르소나의 일정에서 나온다.

- realtime: 계획 시각이 되면 실행한다. 발생기를 켜기 전에 지난 세션은 버린다. 늦어도 max_lateness
  안이면 실행하고, 그보다 늦거나 날짜가 바뀌면 놓친 세션으로 센다.
- immediate: 오늘의 세션을 시각과 관계없이 바로 실행한다. 로컬 검증용이다.

운영 중 조절(kkugi_load/control.py):
- 배율(0~1): 계획된 세션을 이 확률로만 실행한다. 일정은 그대로 두고 세션을 꺼낼 때(next) 거른다.
  세션마다 (계정, 날짜, 시간대)로 정해지는 0~1 값이 배율보다 작아야 남으므로, 배율을 바꿔도 같은 세션이
  일관되게 남거나 빠지고, 낮은 배율에서 남는 세션은 높은 배율에서 남는 세션의 부분집합이다. 거른 세션은
  일정에서 빠지므로 배율을 다시 올리면 아직 시각이 되지 않은 세션만 되살아난다. 정지는 배율 0이다.
- 설정 다시 읽기(replan): 새 설정으로 오늘 일정을 다시 만든다. 이미 꺼낸 세션과(realtime에서) 다시 만든
  시각 이전의 세션은 넣지 않아 같은 세션이 두 번 실행되지 않는다. 실행 중인 세션은 건드리지 않는다.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from kkugi_load.accounts.identity import Account, seeded_rng
from kkugi_load.capacity import Capacity, estimate
from kkugi_load.config import Config, Pool, read_scale
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


def scale_draw(config: Config, planned: PlannedSession) -> float:
    """세션이 배율에 남는지 정하는 0~1 값. 이 값이 배율보다 작으면 실행한다.

    한 시간대에 한 계정의 세션은 최대 하나라 (계정, 날짜, 시간대)가 세션 하나를 가리킨다. 태그를 "scale"로 두어
    앱을 여는 확률("open")이나 계정 활동 배수("activity")와 상관되지 않게 한다.
    """
    at = planned.at.astimezone(config.tz)
    return seeded_rng(config.seed, "scale", planned.account_index, at.date().isoformat(), at.hour).random()


def _key(planned: PlannedSession) -> tuple[int, datetime]:
    return planned.account_index, planned.at


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
        # 오늘 이미 꺼낸 세션. 일정을 다시 만들 때 같은 세션을 다시 넣지 않으려고 기억한다. 날짜가 바뀌면 비운다
        self._taken: set[tuple[int, datetime]] = set()
        # 발생기를 켜기 전에 지나 버려 실행하지 않은 세션 수
        self.dropped_before_start = 0
        # 배율(0~1). set_scale로 바꾼다
        self.scale = read_scale(config.scale)
        # 배율로 거른 세션 수. 전체 누적과, 아직 통계에 남기지 않은 수(take_scaled_out)
        self.scaled_out = 0
        self._scaled_out_unreported = 0
        # 가장 최근에 만든 일정의 실행기 필요량(capacity.py). 일정을 만들기 전에는 None
        self.capacity: Capacity | None = None
        # 새 일정을 만들 때마다 실행기 필요량을 넘겨 부른다
        self.on_plan = on_plan

    def _push(self, at: datetime, planned: PlannedSession) -> None:
        heapq.heappush(self._heap, _Entry(at, next(self._seq), planned))

    def _build(self, config: Config, day: date, cutoff: datetime) -> tuple[list[PlannedSession], int, Capacity]:
        """그날 일정에서 cutoff보다 이른 세션(realtime)과 이미 꺼낸 세션을 뺀 것, 뺀 수, 실행기 필요량."""
        kept: list[PlannedSession] = []
        dropped = 0
        for account in self.accounts:
            for planned in PERSONAS[account.persona].plan_day(config, account, day):
                if self.pool and not self.pool.allows(planned.at.time()):
                    continue
                if _key(planned) in self._taken:
                    continue
                if self.mode == REALTIME and planned.at < cutoff:
                    dropped += 1
                    continue
                kept.append(planned)
        # 그날 남은 일정만 센다. 시작 전에 지난 세션은 실행하지 않으므로 실행기가 필요 없다.
        # 배율은 곱하지 않는다. 운영 중에 다시 올라갈 수 있으므로 배율 1일 때의 필요량을 본다.
        # 모르는 세션 종류면 여기서 오류가 나므로 일정에 넣기 전에 계산한다
        return kept, dropped, estimate(config, kept)

    def _commit(self, kept: list[PlannedSession], capacity: Capacity, now: datetime) -> None:
        self._heap = []
        for planned in kept:
            self._push(now if self.mode == IMMEDIATE else planned.at, planned)
        self.capacity = capacity
        if self.on_plan is not None:
            self.on_plan(capacity)

    def _plan(self, day: date) -> None:
        now = self.now()
        self._taken.clear()
        kept, dropped, capacity = self._build(self.config, day, now - self.max_lateness)
        self._planned_day = day
        self.dropped_before_start += dropped
        log.info(
            "%s 일정: 세션 %d개 예정, 시작 전에 지난 %d개 제외. 필요 실행기 %d개(가장 바쁜 시간대 %s시, 평균 동시 세션 %.1f)",
            day, len(kept), dropped, capacity.required_users, capacity.busiest_hour, capacity.busiest_concurrency,
        )
        self._commit(kept, capacity, now)

    def replan(self, config: Config) -> None:
        """새 설정으로 오늘 일정을 다시 만든다. 일정을 아직 만들지 않았으면 설정만 바꾼다.

        다시 만든 시각 이전의 세션(realtime)과 오늘 이미 꺼낸 세션은 넣지 않는다. 지나간 세션이 다시
        실행되면 안 되기 때문이다. 실행 중인 세션은 그대로 두고, 끝나면 release로 풀린다. 새 일정을 만들다
        오류가 나면 지금 일정과 설정을 그대로 두고 오류를 그대로 낸다. 배율은 바꾸지 않는다.
        """
        if self._planned_day is None:
            self.config, self.max_lateness = config, config.max_lateness
            return
        now = self.now()
        kept, dropped, capacity = self._build(config, self._planned_day, now)
        self.config, self.max_lateness = config, config.max_lateness
        log.info(
            "%s 일정을 새 설정으로 다시 만듦: 세션 %d개 예정, 지난 %d개 제외, 실행 중 %d개 유지. 필요 실행기 %d개"
            "(가장 바쁜 시간대 %s시, 평균 동시 세션 %.1f)",
            self._planned_day, len(kept), dropped, len(self._busy), capacity.required_users, capacity.busiest_hour,
            capacity.busiest_concurrency,
        )
        self._commit(kept, capacity, now)

    def set_scale(self, value: float) -> None:
        """배율을 바꾼다. 일정은 다시 만들지 않는다. 0~1 밖이면 ValueError."""
        self.scale = read_scale(value)

    def take_scaled_out(self) -> int:
        """지난번 이후 배율로 거른 세션 수. 통계(SCHEDULE scaled_out)에 남길 몫을 가져가며 0으로 되돌린다."""
        count, self._scaled_out_unreported = self._scaled_out_unreported, 0
        return count

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
                # 배율을 먼저 본다. 거른 세션은 미루거나 놓친 것으로 세지 않는다
                if not scale_draw(self.config, planned) < self.scale:
                    self._taken.add(_key(planned))
                    self.scaled_out += 1
                    self._scaled_out_unreported += 1
                    continue
                if planned.account_index in self._busy:
                    postponed.append(planned)
                    continue
                self._taken.add(_key(planned))
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
