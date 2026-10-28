"""꾸기 인공 트래픽 발생기의 Locust 진입점.

Locust User 하나가 계정 하나를 맡아, 페르소나가 정한 오늘의 세션을 시각과 관계없이 차례로 한 번씩
실행하고 멈춘다. 시간대별 스케줄은 아직 없다.

통계 이름:
- GET /..., POST /...      세션의 요청
- [setup] ..., [auth] ...  계정 준비와 토큰 발급. 세션 요청과 나눠 본다
- SESSION ...              세션 결과(kkugi_load/runner.py)
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

from locust import HttpUser, events, task
from locust.exception import StopUser
from locust.runners import LocalRunner

from kkugi_load import config as config_module
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import PERSONAS, build_account
from kkugi_load.runner import run_planned

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


class AccountUser(HttpUser):
    def on_start(self):
        index = POOL.claim()
        if index is None:
            log.warning("남은 계정이 없어 User를 멈춘다. User 수를 KKUGI_ACCOUNT_COUNT 이하로 둔다")
            raise StopUser()
        self.account = build_account(CONFIG, index)
        state = STORE.load(self.account.email, index, self.account.persona)
        self.api = KkugiApi(self.client, CONFIG, self.account, state, STORE)
        self.rng = random.Random()

    @task
    def todays_sessions(self):
        try:
            day = datetime.now(CONFIG.tz).date()
            planned = PERSONAS[self.account.persona].plan_day(CONFIG, self.account, day)
            if not planned:
                log.info("계정 %d는 오늘 활동하지 않는다", self.account.index)
            for session in planned:
                run_planned(self.environment.events, self.api, CONFIG, session, self.rng)
        finally:
            POOL.finished += 1
            # 단독 실행에서는 모든 계정이 끝나면 종료한다. 분산 실행의 종료는 Master가 정한다
            if POOL.all_finished() and isinstance(self.environment.runner, LocalRunner):
                log.info("모든 계정의 세션이 끝나 종료한다")
                self.environment.runner.quit()
        raise StopUser()
