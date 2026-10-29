"""계정 번호에서 이메일, 페르소나, 식사 UUID를 정한다.

같은 시드와 계정 번호에서는 늘 같은 값이 나온다. 그래서 세션을 다시 실행해도 같은 식사 UUID가 나오고,
서버의 clientUuid 멱등 처리로 중복 기록이 생기지 않는다.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from kkugi_load.config import Config

# 식사 clientUuid를 만드는 이름공간. 바꾸면 이미 기록한 식사와 UUID가 달라진다
MEAL_UUID_NAMESPACE = uuid.UUID("6f1c2a52-7c1e-4d0b-9a4e-3b8f5d2c7e10")
MEAL_TYPES = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")


@dataclass(frozen=True)
class Account:
    index: int
    email: str
    persona: str
    # 페르소나가 정하는 프로필. body.onboarding_body()로 온보딩 요청 본문을 만든다
    profile: Any


def seeded_rng(seed: int, *parts: object) -> random.Random:
    return random.Random(":".join(str(p) for p in (seed, *parts)))


def email_of(config: Config, index: int) -> str:
    return f"{config.email_prefix}-{index:05d}@{config.email_domain}"


def persona_of(config: Config, index: int) -> str:
    """설정의 weight 비율대로 페르소나를 배정한다. 이름순으로 누적하므로 설정 파일의 순서와 관계없다."""
    names = sorted(config.personas)
    weights = [float(config.personas[name]["weight"]) for name in names]
    return seeded_rng(config.seed, "persona", index).choices(names, weights=weights)[0]


def meal_uuid(email: str, day: date, meal_type: str) -> str:
    return str(uuid.uuid5(MEAL_UUID_NAMESPACE, f"{email}|{day.isoformat()}|{meal_type}"))


def possible_meal_uuids(email: str, day: date) -> set[str]:
    """이 계정이 이 날짜에 기록할 수 있는 식사 UUID 전체. 인공 계정의 식사는 발생기만 쓴다."""
    return {meal_uuid(email, day, meal_type) for meal_type in MEAL_TYPES}


def eaten_at(planned_at: datetime, now: datetime) -> datetime:
    """서버는 식사 시각을 한국 시간 오늘 0시부터 지금까지만 받는다. 계획 시각이 아직 오지 않았으면 지금으로 당긴다."""
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(start_of_day, min(planned_at, now.replace(microsecond=0)))
