"""운동 숙련자. 주 5-6회 저녁에 루틴 운동을 기록하고, 끼니마다 식사를 기록한다.

하루 흐름은 다이어터와 같다(끼니 창의 첫 세션은 식사 기록, 체중 창의 첫 조회는 일부 체중 기록, 나머지는 조회).
다른 점은 운동이다. 다이어터는 운동 창의 첫 조회를 주간 목표 / 7 확률로 운동 기록으로 바꾸지만, 숙련자는 운동 초보자처럼
주마다 운동할 요일을 주간 목표 횟수만큼 정한다. 운동하는 날에는 운동 창의 첫 조회를 운동 기록으로 바꾸고, 운동 창에
조회가 없으면 운동 창 안의 비어 있는 시간대에 운동 세션을 더한다. 그래서 주간 목표 횟수만큼 반드시 운동한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from kkugi_load.accounts.identity import Account, seeded_rng
from kkugi_load.config import Config, hhmm
from kkugi_load.personas.base import PlannedSession, sample_body
from kkugi_load.personas.dieter import Dieter, DieterProfile
from kkugi_load.personas.workout_plan import weekly_days, window_time, workout_inputs


class Expert(Dieter):
    name = "expert"

    def build_profile(self, config: Config, index: int) -> DieterProfile:
        settings = config.personas[self.name]
        rng = seeded_rng(config.seed, "expert_profile", index)
        body = sample_body(
            rng, f"{config.email_prefix}{index:05d}", rng.choice(["GAIN", "MAINTAIN"]),
            # 근육량이 있어 표준 체중보다 조금 무겁다
            bmi=(22, 27), activity_levels=["MODERATE"],
        )
        preferred = tuple(rng.sample(settings["main_keywords"], int(settings["preferred_keywords"])))
        return DieterProfile(body=body, preferred_keywords=preferred)

    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        planned = super().plan_day(config, account, day)
        if not self._is_workout_day(config, account, day) or any(p.session == "workout" for p in planned):
            return planned
        # 운동 창에 조회가 없던 날. 계정·날짜로 정한 시각부터 운동 창 안의 비어 있는 시간대를 찾는다
        workout = config.personas[self.name]["workout"]
        at = window_time(config, account.index, day, workout["from"], workout["to"], "expert_time")
        start, end = hhmm(workout["from"]).hour, hhmm(workout["to"]).hour
        hours = list(range(start, end)) or [start]
        busy = {p.at.hour for p in planned}
        for offset in range(len(hours)):
            hour = hours[(hours.index(at.hour) + offset) % len(hours)]
            if hour not in busy:
                when = at + timedelta(hours=hour - at.hour)
                planned.append(self._workout_session(config, account, day, when))
                return sorted(planned, key=lambda p: p.at)
        # 운동 창의 모든 시간대가 식사 기록으로 찼다. 지금 설정에서는 생기지 않는다
        return planned

    def _plan_workout(self, config: Config, account: Account, day: date, at: datetime) -> PlannedSession | None:
        """운동하는 날이면 운동 창의 첫 조회를 운동 기록으로 바꾼다."""
        if not self._is_workout_day(config, account, day):
            return None
        return self._workout_session(config, account, day, at)

    def _workout_session(self, config: Config, account: Account, day: date, at: datetime) -> PlannedSession:
        inputs = workout_inputs(config, account, day, at, scheduled=True)
        return PlannedSession(account.index, f"{self.name}_workout", at, "workout", inputs)

    def _is_workout_day(self, config: Config, account: Account, day: date) -> bool:
        workout = config.personas[self.name]["workout"]
        # 주간 목표는 계정마다 고정이다. 운동 입력과 같은 값을 쓰려고 입력 생성기에서 읽는다
        at = window_time(config, account.index, day, workout["from"], workout["to"], "expert_time")
        target = workout_inputs(config, account, day, at, scheduled=True).weekly_target
        return day.weekday() in weekly_days(config, account.index, day, target, "expert_week")
