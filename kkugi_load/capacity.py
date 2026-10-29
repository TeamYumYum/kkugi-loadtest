"""하루 일정에서 필요한 실행기(Locust User) 수를 추정한다.

User 하나는 한 번에 세션 하나만 실행하므로, 한 Worker의 동시 세션 상한은 그 Worker가 받은 User 수다.
시간대(한국 시간의 시)마다 Σ(세션 수 × 그 종류의 평균 길이) ÷ 3600으로 평균 동시 세션 수를 구하고,
가장 바쁜 시간대 값에 여유 배수를 곱해 올림한 것을 필요 실행기 수로 본다.

평균 동시 세션 수는 한 시간 동안 고르게 퍼졌을 때의 값이다. 시간대 안에서 몰리는 정도는 여유 배수로 흡수한다.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from kkugi_load.config import Config
from kkugi_load.personas.base import PlannedSession

# 가장 바쁜 시간대의 평균 동시 세션 수에 곱하는 여유 배수. 가정
MARGIN = 2
# 세션 하나의 요청 시간 여유(초). 응답 대기를 생각 시간 밖에서 따로 잡는다. 가정
REQUEST_SECONDS = 1.0


def _mean(bounds: tuple[float, float]) -> float:
    return (bounds[0] + bounds[1]) / 2


def session_seconds(config: Config, session: str) -> float:
    """세션 종류의 평균 길이(초) 추정값. 실제 길이는 SESSION 통계로 확인한다.

    새 세션 종류를 더하면 여기에도 더한다. 모르는 종류는 ValueError를 낸다.
    """
    if session == "meal_log":
        # 음식마다 검색 생각 시간과 선택 생각 시간이 한 번씩 들어간다. 음식 수는 다이어터 설정을 쓴다
        items = int(config.personas["dieter"]["items_per_meal"])
        return items * (_mean(config.search_think) + _mean(config.select_think)) + REQUEST_SECONDS
    if session == "view":
        # 생각 시간 없이 조회 세 건
        return REQUEST_SECONDS
    raise ValueError(f"평균 길이를 모르는 세션 종류: {session}")


@dataclass(frozen=True)
class Capacity:
    # 필요 실행기 수. 세션이 없으면 0
    required_users: int
    # 가장 바쁜 시간대(한국 시간의 시). 세션이 없으면 None
    busiest_hour: int | None
    # 가장 바쁜 시간대의 평균 동시 세션 수(여유 배수 전)
    busiest_concurrency: float
    # 추정에 넣은 세션 수
    sessions: int


def estimate(config: Config, planned: Iterable[PlannedSession]) -> Capacity:
    counts: Counter[tuple[int, str]] = Counter(
        (p.at.astimezone(config.tz).hour, p.session) for p in planned
    )
    lengths = {session: session_seconds(config, session) for _, session in counts}
    concurrency: dict[int, float] = {}
    for (hour, session), count in counts.items():
        concurrency[hour] = concurrency.get(hour, 0.0) + count * lengths[session] / 3600
    if not concurrency:
        return Capacity(required_users=0, busiest_hour=None, busiest_concurrency=0.0, sessions=0)
    # 같은 값이면 이른 시간대
    busiest = max(sorted(concurrency), key=lambda hour: concurrency[hour])
    peak = concurrency[busiest]
    return Capacity(
        # 부동소수 오차로 정수 경계에서 하나 더 올라가지 않게 한다
        required_users=math.ceil(round(peak * MARGIN, 9)),
        busiest_hour=busiest,
        busiest_concurrency=peak,
        sessions=sum(counts.values()),
    )


def shortage_warning(capacity: Capacity, user_count: int) -> str | None:
    """User 수가 필요 실행기 수보다 적으면 경고 문구, 아니면 None."""
    if user_count >= capacity.required_users:
        return None
    return (
        f"실행기 부족: 이 프로세스의 User {user_count}개 < 필요 {capacity.required_users}개"
        f"({capacity.busiest_hour}시 평균 동시 세션 {capacity.busiest_concurrency:.1f} × 여유 {MARGIN}). "
        "세션이 늦게 시작되거나(SCHEDULE start_delay) 놓칠 수 있다(SCHEDULE missed)"
    )
