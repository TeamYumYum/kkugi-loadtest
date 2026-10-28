"""꾸기 인공 트래픽 발생기의 Locust 진입점.

Locust User는 계정이 아니라 실행기다. 이 프로세스가 맡은 계정들의 일정표(kkugi_load/scheduler.py)에서
시각이 된 세션을 꺼내 실행한다. 그래서 User 수는 "동시에 진행할 수 있는 세션 수"이고, 계정 수와 다르다.
User 수가 모자라면 SCHEDULE start_delay가 늘고 SCHEDULE missed가 생긴다.

통계 이름:
- GET /..., POST /...      세션의 요청
- [setup] ..., [auth] ...  계정 준비와 토큰 발급. 세션 요청과 나눠 본다
- SESSION ..., SCHEDULE .. 세션 결과와 일정 지연(kkugi_load/runner.py)
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

from locust import HttpUser, constant, events, task
from locust.runners import LocalRunner

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import Account
from kkugi_load.accounts.state import AccountState, StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.runner import report_missed, report_start_delay, run_planned
from kkugi_load.scheduler import Due, Scheduler

log = logging.getLogger("kkugi")

CONFIG = config_module.load()
STORE: StateStore | None = None
SCHEDULER: Scheduler | None = None
ACCOUNTS: dict[int, Account] = {}
STATES: dict[int, AccountState] = {}


@events.init.add_listener
def _on_init(environment, **_kwargs):
    global STORE, SCHEDULER
    STORE = StateStore(CONFIG.state_db)
    indexes = range(CONFIG.account_start, CONFIG.account_start + CONFIG.account_count)
    accounts = [build_account(CONFIG, i) for i in indexes]
    for account in accounts:
        STATES[account.index] = STORE.load(account.email, account.index, account.persona)
    ACCOUNTS.update({account.index: account for account in accounts})
    SCHEDULER = Scheduler(
        CONFIG, accounts, CONFIG.schedule_mode, CONFIG.max_lateness, lambda: datetime.now(CONFIG.tz),
    )
    log.info(
        "계정 %d-%d(%d개) 담당, 일정 방식 %s, 상태 DB %s", CONFIG.account_start,
        CONFIG.account_start + CONFIG.account_count - 1, CONFIG.account_count, CONFIG.schedule_mode, CONFIG.state_db,
    )


@events.quitting.add_listener
def _on_quitting(environment, **_kwargs):
    if STORE is not None:
        STORE.close()


class SessionExecutor(HttpUser):
    # 실행할 세션이 없을 때 다시 확인하는 간격(초)
    wait_time = constant(1)

    def on_start(self):
        self.rng = random.Random()

    @task
    def run_next(self):
        item = SCHEDULER.next()
        if item is None:
            if SCHEDULER.finished() and isinstance(self.environment.runner, LocalRunner):
                log.info("오늘의 세션을 모두 실행해 종료한다")
                self.environment.runner.quit()
            return
        planned = item.planned
        if not isinstance(item, Due):
            report_missed(self.environment.events, planned, item.reason)
            return
        try:
            report_start_delay(self.environment.events, item.delay.total_seconds() * 1000)
            account = ACCOUNTS[planned.account_index]
            api = KkugiApi(self.client, CONFIG, account, STATES[account.index], STORE)
            run_planned(self.environment.events, api, CONFIG, planned, self.rng)
        finally:
            SCHEDULER.release(planned.account_index)
