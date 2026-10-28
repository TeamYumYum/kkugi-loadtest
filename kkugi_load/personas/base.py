"""페르소나 공통 인터페이스.

페르소나는 규칙만 가진다. 계정의 프로필을 만들고, 그 계정이 하루에 어떤 세션을 언제 실행할지 정한다.
세션의 요청 흐름은 sessions 패키지에 있고 여러 페르소나가 함께 쓴다.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kkugi_load.accounts.identity import Account
from kkugi_load.config import Config


@dataclass(frozen=True)
class BodyProfile:
    """온보딩에 보내는 값."""

    nickname: str
    gender: str
    birth_date: date
    height_cm: float
    weight_kg: float
    activity_level: str
    health_goal: str

    def onboarding_body(self) -> dict:
        return {
            "nickname": self.nickname,
            "healthGoal": self.health_goal,
            "gender": self.gender,
            "birthDate": self.birth_date.isoformat(),
            "heightCm": self.height_cm,
            "weightKg": self.weight_kg,
            "activityLevel": self.activity_level,
        }


@dataclass(frozen=True)
class PlannedSession:
    account_index: int
    # 통계 이름. 예: dieter_lunch
    name: str
    at: datetime
    # sessions.SESSIONS의 키. 예: meal_log
    session: str
    # 세션 종류별 입력
    inputs: Any


def sample_body(
    rng: random.Random, nickname: str, health_goal: str, bmi: tuple[float, float], activity_levels: list[str],
) -> BodyProfile:
    gender = rng.choice(["MALE", "FEMALE"])
    height = round(rng.uniform(165, 185) if gender == "MALE" else rng.uniform(152, 170), 1)
    weight = round(rng.uniform(*bmi) * (height / 100) ** 2, 1)
    return BodyProfile(
        nickname=nickname,
        gender=gender,
        birth_date=date(1975, 1, 1) + timedelta(days=rng.randrange(0, 365 * 30)),
        height_cm=height,
        weight_kg=weight,
        activity_level=rng.choice(activity_levels),
        health_goal=health_goal,
    )


def time_between(rng: random.Random, day: date, start: time, end: time, tz: ZoneInfo) -> datetime:
    begin = datetime.combine(day, start, tz)
    span = int((datetime.combine(day, end, tz) - begin).total_seconds() // 60)
    return begin + timedelta(minutes=rng.randint(0, max(span, 0)))


class Persona(ABC):
    name: str

    @abstractmethod
    def build_profile(self, config: Config, index: int) -> Any:
        """계정의 고정 프로필. body 속성에 BodyProfile이 있어야 한다."""

    @abstractmethod
    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        """그날 실행할 세션을 시각순으로 돌려준다. 같은 계정과 날짜에서는 늘 같은 결과여야 한다."""
