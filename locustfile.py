"""꾸기 인공 트래픽 발생기의 Locust 진입점.

지금은 다이어터의 점심 기록 세션 하나만 있다. Locust User 하나가 계정 하나를 맡아 세션을 한 번
실행하고 멈춘다. 시간대별 스케줄은 아직 없다.

통계 이름:
- GET /..., POST /...      측정 세션의 요청
- [setup] ..., [auth] ...  계정 준비와 토큰 발급. 측정 세션과 나눠 본다
- SESSION diet_lunch       세션 하나의 성공·실패와 걸린 시간(생각 시간 포함)
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

from locust import HttpUser, events, task
from locust.exception import StopUser
from locust.runners import LocalRunner

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import build_account, build_day_plan
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import ApiError, KkugiApi
from kkugi_load.sessions import diet_lunch

log = logging.getLogger("kkugi")

CONFIG = config_module.load()


class _AccountPool:
    """이 프로세스가 맡은 계정 번호를 User에게 하나씩 나눠 준다. 같은 번호를 두 번 주지 않는다."""

    def __init__(self, start: int, count: int):
        self._indexes = list(range(start, start + count))
        self.claimed = 0
        self.finished = 0

    def claim(self) -> int | None:
        if self.claimed >= len(self._indexes):
            return None
        index = self._indexes[self.claimed]
        self.claimed += 1
        return index

    def all_finished(self) -> bool:
        return self.finished >= len(self._indexes)


POOL = _AccountPool(CONFIG.account_start, CONFIG.account_count)
STORE: StateStore | None = None


@events.init.add_listener
def _on_init(environment, **_kwargs):
    global STORE
    STORE = StateStore(CONFIG.state_db)
    log.info(
        "계정 %d-%d 담당, 상태 DB %s", CONFIG.account_start, CONFIG.account_start + CONFIG.account_count - 1,
        CONFIG.state_db,
    )


@events.quitting.add_listener
def _on_quitting(environment, **_kwargs):
    if STORE is not None:
        STORE.close()


class DietLunchUser(HttpUser):
    def on_start(self):
        index = POOL.claim()
        if index is None:
            log.warning("남은 계정이 없어 User를 멈춘다. User 수를 KKUGI_ACCOUNT_COUNT 이하로 둔다")
            raise StopUser()
        self.account = build_account(CONFIG, index)
        state = STORE.load(self.account.email, index, self.account.persona)
        self.api = KkugiApi(self.client, CONFIG, self.account, state, STORE)
        self.rng = random.Random()

    def _fire(self, name: str, started: float, exception: Exception | None) -> None:
        self.environment.events.request.fire(
            request_type="SESSION", name=name, response_time=(time.perf_counter() - started) * 1000,
            response_length=0, exception=exception, context={"account": self.account.index},
        )

    @task
    def lunch_session(self):
        now = datetime.now(CONFIG.tz)
        plan = build_day_plan(CONFIG, self.account, now.date())
        try:
            started = time.perf_counter()
            try:
                diet_lunch.prepare(self.api, plan, now)
            except (ApiError, diet_lunch.ValidationFailed) as e:
                self._fire("[setup] diet_lunch", started, e)
                log.error("계정 %d 준비 실패: %s", self.account.index, e)
                return

            started = time.perf_counter()
            try:
                outcome = diet_lunch.run(
                    self.api, CONFIG, plan, lambda: datetime.now(CONFIG.tz), time.sleep, self.rng,
                )
            except diet_lunch.SessionSkipped as e:
                self._fire("diet_lunch (skipped)", started, None)
                log.info("계정 %d 세션 건너뜀: %s", self.account.index, e)
                return
            except (ApiError, diet_lunch.ValidationFailed) as e:
                self._fire("diet_lunch", started, e)
                log.error("계정 %d 세션 실패: %s", self.account.index, e)
                return
            self.api.state.last_session_on = plan.day.isoformat()
            STORE.save(self.api.state)
            self._fire("diet_lunch", started, None)
            log.info("계정 %d 점심 기록 완료: %s", self.account.index, outcome)
        finally:
            self._finish()

    def _finish(self):
        POOL.finished += 1
        # 단독 실행에서는 모든 계정이 한 번씩 끝나면 종료한다. 분산 실행의 종료는 Master가 정한다
        if POOL.all_finished() and isinstance(self.environment.runner, LocalRunner):
            log.info("모든 계정의 세션이 끝나 종료한다")
            self.environment.runner.quit()
        raise StopUser()
