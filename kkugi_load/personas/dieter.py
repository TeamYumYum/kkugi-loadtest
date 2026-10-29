"""다이어터. 하루 2-3끼를 기록하고, 앱을 열 때마다 요약과 목표를 본다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from kkugi_load.accounts.identity import Account, meal_uuid, seeded_rng
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, sample_body, time_between
from kkugi_load.sessions.meal_log import MealInputs


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
        rng = seeded_rng(config.seed, "day", account.index, day.isoformat())
        if rng.random() >= float(settings["active_probability"]):
            return []

        items = int(settings["items_per_meal"])
        preferred = list(account.profile.preferred_keywords)
        planned = []
        for meal_type, meal in settings["meals"].items():
            if rng.random() >= float(meal["probability"]):
                continue
            pool = list(settings["breakfast_keywords"]) if meal_type == "BREAKFAST" else preferred
            keywords = tuple(rng.sample(pool, items))
            at = time_between(rng, day, hhmm(meal["from"]), hhmm(meal["to"]), config.tz)
            inputs = MealInputs(
                meal_type=meal_type,
                client_uuid=meal_uuid(account.email, day, meal_type),
                planned_at=at,
                keywords=keywords,
                # 검색 결과가 없을 때 이어서 검색할 단어
                fallback_keywords=tuple(k for k in pool if k not in keywords),
                grams=tuple(rng.randint(int(settings["grams_min"]), int(settings["grams_max"])) for _ in keywords),
                picks=tuple(rng.randrange(0, 1000) for _ in keywords),
            )
            planned.append(PlannedSession(account.index, f"dieter_{meal_type.lower()}", at, "meal_log", inputs))
        return sorted(planned, key=lambda p: p.at)
