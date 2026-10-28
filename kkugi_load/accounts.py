"""인공 계정의 고정 프로필과 하루 계획을 만든다.

같은 시드, 계정 번호, 날짜에서는 늘 같은 값이 나온다. 그래서 세션을 다시 실행해도 같은 식사
UUID와 같은 내용이 나오고, 서버의 clientUuid 멱등 처리로 중복 기록이 생기지 않는다.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from kkugi_load.config import Config, DietLunchConfig

# 식사 clientUuid를 만드는 이름공간. 바꾸면 이미 기록한 식사와 UUID가 달라진다
MEAL_UUID_NAMESPACE = uuid.UUID("6f1c2a52-7c1e-4d0b-9a4e-3b8f5d2c7e10")

PERSONA_DIETER = "dieter"


@dataclass(frozen=True)
class Profile:
    """온보딩에 보내는 값과 계정의 행동 성향."""

    nickname: str
    gender: str
    birth_date: date
    height_cm: float
    weight_kg: float
    activity_level: str
    health_goal: str
    preferred_lunch_keywords: tuple[str, ...]

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
class Account:
    index: int
    email: str
    persona: str
    profile: Profile


@dataclass(frozen=True)
class MealPlan:
    meal_type: str
    client_uuid: str
    planned_at: datetime
    keywords: tuple[str, ...]
    grams: tuple[int, ...]
    # 검색 결과에서 고를 순번의 기준값. 결과 수로 나눈 나머지를 쓴다
    picks: tuple[int, ...]


@dataclass(frozen=True)
class DayPlan:
    day: date
    breakfast: MealPlan
    lunch: MealPlan


def _rng(seed: int, *parts: object) -> random.Random:
    return random.Random(":".join(str(p) for p in (seed, *parts)))


def build_account(config: Config, index: int) -> Account:
    rng = _rng(config.seed, "profile", index)
    lunch = config.diet_lunch
    gender = rng.choice(["MALE", "FEMALE"])
    height = round(rng.uniform(165, 185) if gender == "MALE" else rng.uniform(152, 170), 1)
    # 다이어터라 표준 체중보다 조금 무겁게 잡는다
    bmi = rng.uniform(23, 29)
    weight = round(bmi * (height / 100) ** 2, 1)
    birth = date(1975, 1, 1) + timedelta(days=rng.randrange(0, 365 * 30))
    profile = Profile(
        nickname=f"{config.email_prefix}{index:05d}",
        gender=gender,
        birth_date=birth,
        height_cm=height,
        weight_kg=weight,
        activity_level=rng.choice(["SEDENTARY", "LIGHT", "LIGHT", "MODERATE"]),
        health_goal="LOSE",
        preferred_lunch_keywords=tuple(rng.sample(lunch.lunch_keywords, lunch.preferred_lunch_keywords)),
    )
    return Account(
        index=index,
        email=f"{config.email_prefix}-{index:05d}@{config.email_domain}",
        persona=PERSONA_DIETER,
        profile=profile,
    )


def meal_uuid(email: str, day: date, meal_type: str) -> str:
    return str(uuid.uuid5(MEAL_UUID_NAMESPACE, f"{email}|{day.isoformat()}|{meal_type}"))


def _planned_time(rng: random.Random, day: date, start: time, end: time, tz: ZoneInfo) -> datetime:
    begin = datetime.combine(day, start, tz)
    span = int((datetime.combine(day, end, tz) - begin).total_seconds() // 60)
    return begin + timedelta(minutes=rng.randint(0, max(span, 0)))


def _meal(
    rng: random.Random, account: Account, day: date, meal_type: str, keywords: tuple[str, ...],
    lunch: DietLunchConfig, planned_at: datetime,
) -> MealPlan:
    return MealPlan(
        meal_type=meal_type,
        client_uuid=meal_uuid(account.email, day, meal_type),
        planned_at=planned_at,
        keywords=keywords,
        grams=tuple(rng.randint(lunch.grams_min, lunch.grams_max) for _ in keywords),
        picks=tuple(rng.randrange(0, 1000) for _ in keywords),
    )


def build_day_plan(config: Config, account: Account, day: date) -> DayPlan:
    rng = _rng(config.seed, "day", account.index, day.isoformat())
    lunch = config.diet_lunch
    breakfast_keywords = tuple(rng.sample(lunch.breakfast_keywords, 2))
    lunch_keywords = tuple(rng.sample(account.profile.preferred_lunch_keywords, lunch.items))
    return DayPlan(
        day=day,
        breakfast=_meal(
            rng, account, day, "BREAKFAST", breakfast_keywords, lunch,
            _planned_time(rng, day, lunch.breakfast_from, lunch.breakfast_to, config.tz),
        ),
        lunch=_meal(
            rng, account, day, "LUNCH", lunch_keywords, lunch,
            _planned_time(rng, day, lunch.lunch_from, lunch.lunch_to, config.tz),
        ),
    )


def eaten_at(planned_at: datetime, now: datetime) -> datetime:
    """서버는 식사 시각을 한국 시간 오늘 0시부터 지금까지만 받는다. 계획 시각이 아직 오지 않았으면 지금으로 당긴다."""
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(start_of_day, min(planned_at, now.replace(microsecond=0)))
