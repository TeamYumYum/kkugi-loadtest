"""하루 몰아 기록형. 낮에는 가끔 홈만 보고, 밤 23시대에 앱을 한 번 열어 그날의 식사·체중·운동을 몰아서 기록한다.

몰아 기록 세션(batch_log)은 그날 기록하는 날(night.probability)에만 있다. 끼니마다 식사 기록 여부와 입력은 다이어터와
같은 규칙(meal_inputs)으로 정하고, 먹은 시각은 끼니 창 안의 시각이다. 체중은 다이어터와 같은 규칙으로 아침에 잰 값이고,
운동은 주마다 주간 목표 횟수만큼 요일을 정해 그날 저녁에 한 것으로 올린다. 몰아 기록하는 시간대의 홈 조회는 몰아 기록
세션으로 대체하므로 한 계정의 시간대별 세션은 최대 하나다.

몰아 기록 창은 밤 12시 전에 끝나게 둔다. 세션이 자정을 넘기면 서버가 어제 시각의 식사를 받지 않는다.
"""

from __future__ import annotations

from datetime import date

from kkugi_load.accounts.identity import Account, seeded_rng, seeded_uniforms
from kkugi_load.config import Config
from kkugi_load.personas.base import PlannedSession, open_times, sample_body
from kkugi_load.personas.dieter import Dieter, DieterProfile
from kkugi_load.personas.workout_plan import weekly_days, window_time, workout_inputs
from kkugi_load.sessions.batch_log import BatchInputs
from kkugi_load.sessions.view import ViewInputs


class Batch(Dieter):
    name = "batch"

    def build_profile(self, config: Config, index: int) -> DieterProfile:
        settings = config.personas[self.name]
        rng = seeded_rng(config.seed, "batch_profile", index)
        body = sample_body(
            rng, f"{config.email_prefix}{index:05d}", rng.choice(["LOSE", "MAINTAIN"]),
            bmi=(21, 28), activity_levels=["LIGHT", "MODERATE"],
        )
        preferred = tuple(rng.sample(settings["main_keywords"], int(settings["preferred_keywords"])))
        return DieterProfile(body=body, preferred_keywords=preferred)

    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        settings = config.personas[self.name]
        night = settings["night"]
        (decide,) = seeded_uniforms(config.seed, "batch_night", account.index, day.isoformat())
        batch = self._batch(config, account, day) if decide < float(night["probability"]) else None
        plan = [
            PlannedSession(account.index, f"{self.name}_view", opened, "view", ViewInputs(planned_at=opened))
            for opened in open_times(config, self.name, account.index, day)
            if batch is None or opened.hour != batch.at.hour
        ]
        if batch is not None:
            plan.append(batch)
        return sorted(plan, key=lambda p: p.at)

    def _batch(self, config: Config, account: Account, day: date) -> PlannedSession | None:
        """그날 밤의 몰아 기록 세션. 올릴 것이 하나도 없으면 None."""
        settings = config.personas[self.name]
        night = settings["night"]
        at = window_time(config, account.index, day, night["from"], night["to"], "batch_night_time")
        meals = []
        for meal_type, meal in settings["meals"].items():
            eaten = window_time(config, account.index, day, meal["from"], meal["to"], f"batch_eaten_{meal_type}")
            inputs = self.meal_inputs(config, account, day, meal_type, eaten)
            if inputs is not None:
                meals.append(inputs)
        weight = settings["body_weight"]
        weighed = window_time(config, account.index, day, weight["from"], weight["to"], "batch_weighed")
        weight_session = self._plan_weight(config, account, day, weighed)
        workout = settings["workout"]
        done = window_time(config, account.index, day, workout["from"], workout["to"], "batch_workout_time")
        workout_today = workout_inputs(config, account, day, done, scheduled=True)
        if day.weekday() not in weekly_days(config, account.index, day, workout_today.weekly_target, "batch_week"):
            workout_today = None
        inputs = BatchInputs(
            planned_at=at,
            meals=tuple(sorted(meals, key=lambda m: m.planned_at)),
            weight=weight_session.inputs if weight_session is not None else None,
            workout=workout_today,
        )
        if not inputs.meals and inputs.weight is None and inputs.workout is None:
            return None
        return PlannedSession(account.index, f"{self.name}_night", at, "batch_log", inputs)
