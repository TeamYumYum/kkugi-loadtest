"""휴면 직전. 가끔 앱을 열어 홈 화면만 본다. 식사·체중·운동은 기록하지 않는다.

앱을 여는 시각은 공통 시간대 표 × 페르소나 activity(작게 둔다) × 계정 활동 배수로 정하고(base.open_times),
연 시각마다 조회 세션(view)만 실행한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from kkugi_load.accounts.identity import Account, seeded_rng
from kkugi_load.config import Config
from kkugi_load.personas.base import BodyProfile, Persona, PlannedSession, open_times, sample_body
from kkugi_load.sessions.view import ViewInputs


@dataclass(frozen=True)
class DormantProfile:
    body: BodyProfile


class Dormant(Persona):
    name = "dormant"

    def build_profile(self, config: Config, index: int) -> DormantProfile:
        rng = seeded_rng(config.seed, "dormant_profile", index)
        return DormantProfile(sample_body(
            rng, f"{config.email_prefix}{index:05d}", rng.choice(["LOSE", "MAINTAIN"]),
            bmi=(21, 28), activity_levels=["SEDENTARY", "LIGHT"],
        ))

    def plan_day(self, config: Config, account: Account, day: date) -> list[PlannedSession]:
        return [
            PlannedSession(account.index, f"{self.name}_view", opened, "view", ViewInputs(planned_at=opened))
            for opened in open_times(config, self.name, account.index, day)
        ]
