"""운동 초보자. 주 2-3회 루틴 운동을 기록하고, 나머지 접속에서는 홈을 조회한다."""

from dataclasses import dataclass
from datetime import date

from kkugi_load.accounts.identity import Account, seeded_rng
from kkugi_load.config import Config
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.personas.workout_plan import weekly_days, window_time, workout_inputs
from kkugi_load.sessions.view import ViewInputs


@dataclass(frozen=True)
class BeginnerProfile:
    body: BodyProfile


class Beginner(Persona):
    name = "beginner"

    def build_profile(self, config: Config, index: int) -> BeginnerProfile:
        rng = seeded_rng(config.seed, "beginner_profile", index)
        return BeginnerProfile(sample_body(
            rng, f"{config.email_prefix}{index:05d}", rng.choice(["MAINTAIN", "GAIN"]),
            bmi=(21, 26), activity_levels=["LIGHT", "MODERATE"],
        ))

    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        settings = config.personas[self.name]["workout"]
        at = window_time(config, account.index, day, settings["from"], settings["to"], "beginner_time")
        inputs = workout_inputs(config, account, day, at, scheduled=True)
        # 같은 주를 매일 다시 계산해도 동일한 요일을 고른다.
        is_workout_day = day.weekday() in weekly_days(config, account.index, day, inputs.weekly_target, "beginner_week")
        plan = [
            PlannedSession(account.index, "beginner_view", opened, "view", ViewInputs(opened))
            for opened in open_times(config, self.name, account.index, day)
            if not is_workout_day or opened.hour != at.hour
        ]
        if is_workout_day:
            plan.append(PlannedSession(account.index, "beginner_workout", at, "workout", inputs))
        return sorted(plan, key=lambda p: p.at)
