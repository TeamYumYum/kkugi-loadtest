"""운동 초보자. 주 2-3회 루틴 운동을 기록하고, 나머지 접속에서는 홈을 조회한다."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from kkugi_load.accounts.identity import Account, seeded_rng, seeded_uniforms
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.personas.workout_plan import workout_inputs
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
        monday = day - timedelta(days=day.weekday())
        # 같은 주를 매일 다시 계산해도 동일한 요일을 고른다.
        weekdays = seeded_uniforms(config.seed, "beginner_week", account.index, monday.isoformat(), count=7)
        (minute,) = seeded_uniforms(config.seed, "beginner_time", account.index, day.isoformat())
        start, end = hhmm(settings["from"]), hhmm(settings["to"])
        lower, upper = start.hour * 60 + start.minute, end.hour * 60 + end.minute
        at = datetime.combine(day, time(), config.tz) + timedelta(minutes=lower + int(minute * (upper - lower)))
        inputs = workout_inputs(config, account, day, at, scheduled=True)
        workout_days = sorted(range(7), key=lambda d: weekdays[d])[:inputs.weekly_target]
        is_workout_day = day.weekday() in workout_days
        plan = [
            PlannedSession(account.index, "beginner_view", opened, "view", ViewInputs(opened))
            for opened in open_times(config, self.name, account.index, day)
            if not is_workout_day or opened.hour != at.hour
        ]
        if is_workout_day:
            plan.append(PlannedSession(account.index, "beginner_workout", at, "workout", inputs))
        return sorted(plan, key=lambda p: p.at)
