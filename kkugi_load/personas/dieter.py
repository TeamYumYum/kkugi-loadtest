"""다이어터. 끼니 창에서 처음 앱을 열 때 그 끼니를 기록하고, 아침에는 체중을 재고, 그 밖에 열 때는 홈 화면만 본다.

앱을 여는 시각은 공통 시간대 표가 정한다(base.open_times). 끼니 창(간식 창 포함) 안에서 그날 처음 연 세션만 그 끼니의
probability로 식사 기록(meal_log)이 되고, 나머지는 조회(view)다. 그래서 끼니마다 식사 기록은 하루 최대 한 번이다.
식사 기록의 일부(edit_probability)는 저장 직후 음식 하나의 양을 고친다.
체중 창 안의 조회 세션 중 그날 첫 번째는 body_weight.probability로 체중 기록(weight_log)이 된다. 체중 기록도 하루 최대 한 번이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from kkugi_load.accounts.identity import Account, seeded_rng, seeded_uniforms
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.sessions.meal_log import MealInputs
from kkugi_load.sessions.view import ViewInputs
from kkugi_load.sessions.weight_log import WeightInputs, drifted_weight


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
        weight = settings["body_weight"]
        weight_start, weight_end = hhmm(weight["from"]), hhmm(weight["to"])
        # 체중 창에서 체중 기록 여부를 이미 정했는지. 첫 조회 세션에서 한 번만 정한다
        weight_decided = False
        planned = []
        for at in open_times(config, self.name, account.index, day):
            meal_type = next((name for name, start, end in windows if start <= at.time() < end), None)
            if meal_type is not None and meal_type not in opened_meals:
                opened_meals.add(meal_type)
                meal = self._plan_meal(config, account, day, meal_type, at)
                if meal is not None:
                    planned.append(meal)
                    continue
            if not weight_decided and weight_start <= at.time() < weight_end:
                weight_decided = True
                session = self._plan_weight(config, account, day, at)
                if session is not None:
                    planned.append(session)
                    continue
            planned.append(PlannedSession(account.index, "dieter_view", at, "view", ViewInputs(planned_at=at)))
        return planned

    def _plan_weight(self, config: Config, account: Account, day: date, at: datetime) -> PlannedSession | None:
        """체중 창의 첫 조회 세션을 체중 기록으로 할지 정한다. 기록하지 않으면 None.

        체중은 시작 체중에서 기준일부터 하루 trend_kg_per_day씩 바뀌고, 계정·날짜로 정한 흔들림(±noise_kg)을 더한다.
        """
        weight = config.personas[self.name]["body_weight"]
        decide, noise = seeded_uniforms(config.seed, "weight", account.index, day.isoformat(), count=2)
        if decide >= float(weight["probability"]):
            return None
        days = (day - date.fromisoformat(weight["reference_date"])).days
        kg = drifted_weight(
            account.profile.body.weight_kg, days, float(weight["trend_kg_per_day"]),
            (2 * noise - 1) * float(weight["noise_kg"]),
        )
        return PlannedSession(account.index, "dieter_weight", at, "weight_log", WeightInputs(planned_at=at, weight_kg=kg))

    def _plan_meal(self, config: Config, account: Account, day: date, meal_type: str, at: datetime) -> PlannedSession | None:
        """끼니 창의 첫 세션을 식사 기록으로 할지 정한다. 기록하지 않으면 None.

        값은 끼니마다 해시로 따로 정한다. 다른 시간대의 결과가 바뀌어도 이 끼니의 기록 여부와 입력값은 그대로다.
        계정 2만 개면 하루 4만 번 넘게 부르므로 random.Random을 만들지 않는다(open_times와 같은 이유).
        """
        settings = config.personas[self.name]
        items = int(settings["items_per_meal"])
        # 기록 여부 1개, 그 뒤로 음식마다 단어·양·검색 결과 고르기에 하나씩, 마지막에 수정 여부·고칠 음식·배율에 하나씩 쓴다.
        # 수정 값을 뒤에 붙였으므로 앞의 값은 수정 기능을 더하기 전과 같다
        u = seeded_uniforms(config.seed, "meal", account.index, day.isoformat(), meal_type, count=4 + 3 * items)
        if u[0] >= float(settings["meals"][meal_type]["probability"]):
            return None
        # 아침과 간식은 가벼운 음식에서, 점심·저녁은 계정의 선호 음식에서 고른다
        light = meal_type in ("BREAKFAST", "SNACK")
        pool = list(settings["breakfast_keywords"]) if light else list(account.profile.preferred_keywords)
        keywords = _sample(pool, u[1:1 + items])
        grams_min, grams_max = int(settings["grams_min"]), int(settings["grams_max"])
        grams = tuple(grams_min + int(x * (grams_max - grams_min + 1)) for x in u[1 + items:1 + 2 * items])
        inputs = MealInputs(
            meal_type=meal_type,
            planned_at=at,
            keywords=keywords,
            # 검색 결과가 없을 때 이어서 검색할 단어
            fallback_keywords=tuple(k for k in pool if k not in keywords),
            grams=grams,
            picks=tuple(int(x * 1000) for x in u[1 + 2 * items:1 + 3 * items]),
            edit_grams=_edit_grams(grams, u[1 + 3 * items:], float(settings["edit_probability"])),
        )
        return PlannedSession(account.index, f"dieter_{meal_type.lower()}", at, "meal_log", inputs)


def _edit_grams(grams: tuple[int, ...], u: tuple[float, ...], probability: float) -> tuple[int, ...] | None:
    """저장 직후 고칠 양. 음식 하나의 양을 0.5-1.5배로 바꾼다. 고치지 않으면 None.

    u는 수정 여부, 고칠 음식, 배율 순서의 값 세 개다. 바꾼 양이 원래와 같으면 10g 늘려 실제로 바뀌게 한다.
    """
    decide, which, factor = u
    if decide >= probability:
        return None
    i = int(which * len(grams))
    new = min(5000, max(1, round(grams[i] * (0.5 + factor))))
    if new == grams[i]:
        new += 10
    return grams[:i] + (new,) + grams[i + 1:]


def _sample(pool: list[str], uniforms: tuple[float, ...]) -> tuple[str, ...]:
    """pool에서 겹치지 않게 len(uniforms)개를 고른다. 앞에서부터 섞는 Fisher-Yates를 고를 만큼만 한다."""
    if len(uniforms) > len(pool):
        raise ValueError(f"단어 {len(pool)}개에서 {len(uniforms)}개를 고를 수 없다")
    pool = list(pool)
    for i, x in enumerate(uniforms):
        j = i + int(x * (len(pool) - i))
        pool[i], pool[j] = pool[j], pool[i]
    return tuple(pool[:len(uniforms)])
