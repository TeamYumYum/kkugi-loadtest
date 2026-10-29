"""계정 번호에서 이메일, 페르소나, 계정 활동 배수, 식사 UUID를 정한다.

같은 시드와 계정 번호에서는 늘 같은 값이 나온다. 그래서 세션을 다시 실행해도 같은 식사 UUID가 나오고,
서버의 clientUuid 멱등 처리로 중복 기록이 생기지 않는다.

이메일만 계정 번호와 generation(탈퇴되어 다시 만든 횟수)으로 정한다. 식사 UUID는 이메일로 정하므로
다시 만든 회원의 식사와 겹치지 않는다. 페르소나, 프로필, 계정 활동 배수는 계정 번호로만 정하므로 바뀌지 않는다.

난수는 두 가지로 만든다. 계정마다 한 번 정하는 프로필과 페르소나는 seeded_rng로 random.Random을 만든다. 날마다
계정마다 다시 정하는 하루 일정(열기 시각, 끼니 입력)과 계정 활동 배수는 seeded_uniforms로 해시에서 값을 바로 얻는다.
문자열 시드로 random.Random을 만드는 비용이 커서, 하루 일정에 그것을 쓰면 계정 2만 개의 일정을 만드는 데 10초가
넘게 걸렸고 그동안 Worker의 세션 실행이 멈췄다.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import random
import struct
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kkugi_load.config import Config

# 이메일 번호 = generation × GENERATION_STRIDE + 계정 번호. 계정 번호는 이보다 작아야 한다
GENERATION_STRIDE = 1_000_000
# 식사 clientUuid를 만드는 이름공간. 바꾸면 이미 기록한 식사와 UUID가 달라진다
MEAL_UUID_NAMESPACE = uuid.UUID("6f1c2a52-7c1e-4d0b-9a4e-3b8f5d2c7e10")
MEAL_TYPES = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")


@dataclass(frozen=True)
class Account:
    index: int
    persona: str
    # 페르소나가 정하는 프로필. body.onboarding_body()로 온보딩 요청 본문을 만든다
    profile: Any


def seeded_rng(seed: int, *parts: object) -> random.Random:
    return random.Random(":".join(str(p) for p in (seed, *parts)))


# 53비트 정수를 0 이상 1 미만으로 옮기는 배율
_UNIT = 2.0 ** -53


def seeded_uniforms(seed: int, *parts: object, count: int = 1) -> tuple[float, ...]:
    """시드와 parts에서 0 이상 1 미만의 균등한 값 count개를 정한다.

    SHAKE-256(출력 길이를 고를 수 있는 해시)으로 8바이트 × count를 뽑아 값마다 다른 구간을 쓴다. 해시 출력의
    서로 다른 구간은 사실상 독립이라 값끼리 상관되지 않고, i번째 값은 count와 관계없이 같다. 프로세스마다
    달라지는 hash()가 아니므로 재시작해도 같은 값이 나온다. parts의 첫 값을 용도별 태그로 두면 같은 계정이라도
    용도끼리 상관되지 않는다.
    """
    if count < 1:
        raise ValueError(f"count는 1 이상이어야 한다: {count}")
    key = ":".join(str(p) for p in (seed, *parts)).encode()
    words = struct.unpack(f">{count}Q", hashlib.shake_256(key).digest(8 * count))
    # random.random()처럼 상위 53비트만 써서 1.0이 나오지 않게 한다
    return tuple((w >> 11) * _UNIT for w in words)


def email_of(config: Config, index: int, generation: int = 0) -> str:
    """계정의 지금 이메일. 탈퇴되면 generation이 올라 새 이메일로 새 회원이 된다."""
    if not 0 <= index < GENERATION_STRIDE:
        raise ValueError(f"계정 번호 {index}는 0 이상 {GENERATION_STRIDE} 미만이어야 한다")
    return f"{config.email_prefix}-{generation * GENERATION_STRIDE + index:05d}@{config.email_domain}"


def persona_of(config: Config, index: int) -> str:
    """설정의 weight 비율대로 페르소나를 배정한다. 이름순으로 누적하므로 설정 파일의 순서와 관계없다."""
    names = sorted(config.personas)
    weights = [float(config.personas[name]["weight"]) for name in names]
    return seeded_rng(config.seed, "persona", index).choices(names, weights=weights)[0]


def activity_multiplier(config: Config, index: int) -> float:
    """계정 활동 배수. activity_multipliers의 비중대로 고르며, 시드와 계정 번호로만 정하므로 재시작해도 같다."""
    values = [m for m, _ in config.activity_multipliers]
    cumulative = list(itertools.accumulate(w for _, w in config.activity_multipliers))
    (u,) = seeded_uniforms(config.seed, "activity", index)
    # random.choices와 같은 방식으로 누적 비중에서 고른다
    return values[bisect.bisect(cumulative, u * cumulative[-1])]


def meal_uuid(email: str, day: date, meal_type: str) -> str:
    return str(uuid.uuid5(MEAL_UUID_NAMESPACE, f"{email}|{day.isoformat()}|{meal_type}"))


def possible_meal_uuids(email: str, day: date) -> set[str]:
    """이 계정이 이 날짜에 기록할 수 있는 식사 UUID 전체. 인공 계정의 식사는 발생기만 쓴다."""
    return {meal_uuid(email, day, meal_type) for meal_type in MEAL_TYPES}


def eaten_at(planned_at: datetime, now: datetime) -> datetime:
    """서버는 식사 시각을 한국 시간 오늘 0시부터 지금까지만 받는다. 계획 시각이 아직 오지 않았으면 지금으로 당긴다."""
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(start_of_day, min(planned_at, now.replace(microsecond=0)))
