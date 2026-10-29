"""다이어터. 끼니 창에서 처음 앱을 열 때 그 끼니를 기록하고, 그 밖에 열 때는 홈 화면만 본다.

앱을 여는 시각은 공통 시간대 표가 정한다(base.open_times). 끼니 창 안에서 그날 처음 연 세션만 그 끼니의
probability로 식사 기록(meal_log)이 되고, 나머지는 모두 조회(view)다. 그래서 끼니마다 식사 기록은 하루 최대 한 번이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from kkugi_load.accounts.identity import Account, seeded_rng
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.sessions.meal_log import MealInputs
from kkugi_load.sessions.view import ViewInputs


@dataclass(frozen=True)
class DieterProfile:
    body: BodyProfile
    preferred_keywords: tuple[str, ...]


class Dieter(Persona):
    name = "dieter"

    def build_profile(self, config: Config, index: int) -> DieterProfile:
        settings = config.personas[self.name]
        rng = seeded_rng(config.seed, "profile", index)
        body = sample_body(
            rng, f"{config.email_prefix}{index:05d}", "LOSE",
            # 다이어터라 표준 체중보다 조금 무겁게 잡는다
            bmi=(23, 29), activity_levels=["SEDENTARY", "LIGHT", "LIGHT", "MODERATE"],
        )
        preferred = tuple(rng.sample(settings["main_keywords"], int(settings["preferred_keywords"])))
        return DieterProfile(body=body, preferred_keywords=preferred)

    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        settings = config.personas[self.name]
        windows = [(meal_type, hhmm(meal["from"]), hhmm(meal["to"])) for meal_type, meal in settings["meals"].items()]
        # 그날 이미 세션을 연 끼니 창. 두 번째부터는 조회 세션이다
        opened_meals = set()
        planned = []
        for at in open_times(config, self.name, account.index, day):
            meal_type = next((name for name, start, end in windows if start <= at.time() < end), None)
            if meal_type is not None and meal_type not in opened_meals:
                opened_meals.add(meal_type)
                meal = self._plan_meal(config, account, day, meal_type, at)
                if meal is not None:
                    planned.append(meal)
                    continue
            planned.append(PlannedSession(account.index, "dieter_view", at, "view", ViewInputs(planned_at=at)))
        return planned

    def _plan_meal(self, config: Config, account: Account, day: date, meal_type: str, at: datetime) -> PlannedSession | None:
        """끼니 창의 첫 세션을 식사 기록으로 할지 정한다. 기록하지 않으면 None.

        난수는 끼니마다 따로 만든다. 다른 시간대의 결과가 바뀌어도 이 끼니의 기록 여부와 입력값은 그대로다.
        """
        settings = config.personas[self.name]
        rng = seeded_rng(config.seed, "meal", account.index, day.isoformat(), meal_type)
        if rng.random() >= float(settings["meals"][meal_type]["probability"]):
            return None
        items = int(settings["items_per_meal"])
        pool = list(settings["breakfast_keywords"]) if meal_type == "BREAKFAST" else list(account.profile.preferred_keywords)
        keywords = tuple(rng.sample(pool, items))
        inputs = MealInputs(
            meal_type=meal_type,
            planned_at=at,
            keywords=keywords,
            # 검색 결과가 없을 때 이어서 검색할 단어
            fallback_keywords=tuple(k for k in pool if k not in keywords),
            grams=tuple(rng.randint(int(settings["grams_min"]), int(settings["grams_max"])) for _ in keywords),
            picks=tuple(rng.randrange(0, 1000) for _ in keywords),
        )
        return PlannedSession(account.index, f"dieter_{meal_type.lower()}", at, "meal_log", inputs)
