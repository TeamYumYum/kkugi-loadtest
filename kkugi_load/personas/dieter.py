"""다이어터. 끼니 창에서 처음 앱을 열 때 그 끼니를 기록하고, 아침에는 체중을 재고, 저녁에는 가끔 운동을 기록하고, 그 밖에 열 때는 홈 화면만 본다.

앱을 여는 시각은 공통 시간대 표가 정한다(base.open_times). 끼니 창(간식 창 포함) 안에서 그날 처음 연 세션만 그 끼니의
probability로 식사 기록(meal_log)이 되고, 나머지는 조회(view)다. 그래서 끼니마다 식사 기록은 하루 최대 한 번이다.
식사 기록의 일부(edit_probability)는 저장 직후 음식 하나의 양을 고친다.
끼니의 칼로리는 그날 목표 칼로리의 비율(kcal_share)로 정한다. 계정의 지킬 확률(adherence_groups)로 그날 지킬지 정하고,
그날의 섭취 비율(intake)을 기록하는 끼니의 몫(meals.*.share)대로 나눈다(apportion).
체중 창 안의 조회 세션 중 그날 첫 번째는 body_weight.probability로 체중 기록(weight_log)이 된다. 체중 기록도 하루 최대 한 번이다.
운동 창 안의 조회 세션 중 그날 첫 번째는 주간 목표 횟수 / 7 확률로 운동 기록(workout)이 된다. 운동 기록도 하루 최대 한 번이다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from kkugi_load.accounts.identity import Account, seeded_rng, seeded_uniforms
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.sessions.meal_log import MealInputs
from kkugi_load.sessions.view import ViewInputs
from kkugi_load.sessions.weight_log import WeightInputs, drifted_weight
from kkugi_load.personas.workout_plan import workout_inputs


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
        workout = settings["workout"]
        workout_start, workout_end = hhmm(workout["from"]), hhmm(workout["to"])
        workout_decided = False
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
            if not workout_decided and workout_start <= at.time() < workout_end:
                workout_decided = True
                session = self._plan_workout(config, account, day, at)
                if session is not None:
                    planned.append(session)
                    continue
            planned.append(PlannedSession(account.index, f"{self.name}_view", at, "view", ViewInputs(planned_at=at)))
        shared = {m.meal_type: m for m in self.apportion(
            config, account, day, [p.inputs for p in planned if p.session == "meal_log"])}
        return [replace(p, inputs=shared[p.inputs.meal_type]) if p.session == "meal_log" else p for p in planned]

    def adherence(self, config: Config, account: Account) -> float:
        """계정이 하루 목표를 지킬 확률. 계정마다 adherence_groups에서 비중대로 한 무리를 고정으로 고른다."""
        groups = config.personas[self.name]["adherence_groups"]
        (u,) = seeded_uniforms(config.seed, "adherence", account.index)
        total = sum(float(g["weight"]) for g in groups)
        edge = 0.0
        for group in groups:
            edge += float(group["weight"]) / total
            if u < edge:
                return float(group["achieve"])
        return float(groups[-1]["achieve"])

    def intake_ratio(self, config: Config, account: Account, day: date) -> float:
        """그날 기록할 하루 섭취 칼로리를 목표의 비율로.

        계정의 지킬 확률(adherence)로 그날 지킬지 정한다. 지키는 날은 achieved_ratio 범위, 못 지키는 날은 missed_over
        확률로 missed_high(많이 먹음), 나머지는 missed_low(적게 먹음) 범위에서 고른다. 서버는 0.9-1.1을 달성으로 본다.
        """
        intake = config.personas[self.name]["intake"]
        keep, over, where = seeded_uniforms(config.seed, "intake_day", account.index, day.isoformat(), count=3)
        if keep < self.adherence(config, account):
            low, high = intake["achieved_ratio"]
        elif over < float(intake["missed_over"]):
            low, high = intake["missed_high"]
        else:
            low, high = intake["missed_low"]
        return float(low) + where * (float(high) - float(low))

    def apportion(self, config: Config, account: Account, day: date, meals: list[MealInputs]) -> list[MealInputs]:
        """그날 기록할 끼니들에 하루 섭취 비율을 끼니 몫대로 나눠 kcal_share를 채운다.

        기록하는 끼니의 몫 합으로 나누므로, 몇 끼를 기록하든 그날 기록한 칼로리의 합이 섭취 비율이 된다.
        """
        settings = config.personas[self.name]
        if not meals:
            return []
        shares = {m.meal_type: float(settings["meals"][m.meal_type]["share"]) for m in meals}
        covered = sum(shares.values())
        ratio = self.intake_ratio(config, account, day)
        return [replace(m, kcal_share=ratio * shares[m.meal_type] / covered) for m in meals]

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
        return PlannedSession(account.index, f"{self.name}_weight", at, "weight_log", WeightInputs(planned_at=at, weight_kg=kg))

    def _plan_workout(self, config: Config, account: Account, day: date, at: datetime) -> PlannedSession | None:
        """운동 창의 첫 조회 세션을 운동 기록으로 할지 정한다. 기록하지 않으면 None.

        주간 목표, 루틴 값, 루틴을 직접 만드는지, 근력, 부위별 종목 풀은 계정마다 고정이다. 준비가 실패해 다른 날 다시
        해도 같은 목표와 루틴을 만든다. 근력 = 페르소나 strength × 성별 배수 × 개인차(0.8-1.2)다.
        오늘 할 부위, 자유 운동 종목, 못 찾는 검색은 날짜마다 정한다.
        """
        inputs = workout_inputs(config, account, day, at)
        return PlannedSession(account.index, f"{self.name}_workout", at, "workout", inputs) if inputs is not None else None

    def _plan_meal(self, config: Config, account: Account, day: date, meal_type: str, at: datetime) -> PlannedSession | None:
        """끼니 창의 첫 세션을 식사 기록으로 할지 정한다. 기록하지 않으면 None."""
        inputs = self.meal_inputs(config, account, day, meal_type, at)
        if inputs is None:
            return None
        return PlannedSession(account.index, f"{self.name}_{meal_type.lower()}", at, "meal_log", inputs)

    def meal_inputs(self, config: Config, account: Account, day: date, meal_type: str, at: datetime) -> MealInputs | None:
        """그 끼니를 at에 먹은 것으로 기록할 입력. 끼니의 probability로 기록하지 않기로 하면 None.

        값은 끼니마다 해시로 따로 정한다. 다른 시간대의 결과가 바뀌어도 이 끼니의 기록 여부와 입력값은 그대로다.
        계정 2만 개면 하루 4만 번 넘게 부르므로 random.Random을 만들지 않는다(open_times와 같은 이유).
        """
        settings = config.personas[self.name]
        items = int(settings["items_per_meal"])
        # 기록 여부 1개, 그 뒤로 음식마다 단어·칼로리 비율·검색 결과 고르기에 하나씩, 마지막에 수정 여부·고칠 음식·배율에
        # 하나씩 쓴다. 칼로리 비율 자리는 예전에 양(그램)을 정하던 자리다. kcal_share는 apportion이 채운다
        u = seeded_uniforms(config.seed, "meal", account.index, day.isoformat(), meal_type, count=4 + 3 * items)
        if u[0] >= float(settings["meals"][meal_type]["probability"]):
            return None
        # 아침과 간식은 가벼운 음식에서, 점심·저녁은 계정의 선호 음식에서 고른다
        light = meal_type in ("BREAKFAST", "SNACK")
        pool = list(settings["breakfast_keywords"]) if light else list(account.profile.preferred_keywords)
        keywords = _sample(pool, u[1:1 + items])
        weights = [0.5 + x for x in u[1 + items:1 + 2 * items]]
        return MealInputs(
            meal_type=meal_type,
            planned_at=at,
            keywords=keywords,
            # 검색 결과가 없을 때 이어서 검색할 단어
            fallback_keywords=tuple(k for k in pool if k not in keywords),
            kcal_share=0.0,
            splits=tuple(w / sum(weights) for w in weights),
            picks=tuple(int(x * 1000) for x in u[1 + 2 * items:1 + 3 * items]),
            misjudge=_misjudge(items, u[1 + 3 * items:], float(settings["edit_probability"])),
        )


def _misjudge(items: int, u: tuple[float, ...], probability: float) -> tuple[int, float] | None:
    """처음에 양을 잘못 입력할 음식의 순번과 배수(0.5-1.5). 잘못 입력하지 않으면 None.

    u는 수정 여부, 고칠 음식, 배수 순서의 값 세 개다.
    """
    decide, which, factor = u
    if decide >= probability:
        return None
    return int(which * items), 0.5 + factor


def _sample(pool: list[str], uniforms: tuple[float, ...]) -> tuple[str, ...]:
    """pool에서 겹치지 않게 len(uniforms)개를 고른다. 앞에서부터 섞는 Fisher-Yates를 고를 만큼만 한다."""
    if len(uniforms) > len(pool):
        raise ValueError(f"단어 {len(pool)}개에서 {len(uniforms)}개를 고를 수 없다")
    pool = list(pool)
    for i, x in enumerate(uniforms):
        j = i + int(x * (len(pool) - i))
        pool[i], pool[j] = pool[j], pool[i]
    return tuple(pool[:len(uniforms)])
