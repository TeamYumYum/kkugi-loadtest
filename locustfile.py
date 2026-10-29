"""꾸기 인공 트래픽 발생기의 Locust 진입점.

Locust User는 계정이 아니라 실행기다. 이 프로세스가 맡은 계정들의 일정표(kkugi_load/scheduler.py)에서
시각이 된 세션을 꺼내 실행한다. 그래서 User 수는 "동시에 진행할 수 있는 세션 수"이고, 계정 수와 다르다.
User 수가 모자라면 SCHEDULE start_delay가 늘고 SCHEDULE missed가 생긴다. 그래서 Worker(또는 단독 실행)는
User 생성이 끝났을 때와 날짜가 바뀌어 새 일정을 만들었을 때, 가진 User 수가 오늘 일정의 필요 실행기 수
(kkugi_load/capacity.py)보다 적으면 경고를 남긴다.

분산 실행에서 Master는 세션을 실행하지 않고 Worker들의 계정 범위가 겹치지 않는지만 확인한다
(kkugi_load/distributed.py). 계정은 Worker가 KKUGI_POOL로 고른 계정 풀에서 맡는다. Master는
--enable-rebalancing 없이 뜨면 종료한다.

운영 중 조절(kkugi_load/control.py): Master(또는 단독 실행)의 웹 UI 경로 /kkugi/*로 배율을 바꾸거나 정지하거나
설정을 다시 읽는다. Master는 바뀐 값을 Worker에 보내고, Worker와 단독 실행은 그 값을 자기 Scheduler에 적용한다.
설정을 다시 읽으면 세션은 Scheduler가 가진 새 설정(SCHEDULER.config)으로 실행한다. 시작 설정(CONFIG)은 계정을
만들 때만 쓴다.

거버너(kkugi_load/governor.py): Master(또는 단독 실행)가 자기 통계에서 최근 창의 HTTP 실패율과 p99를 보고 실제
배율을 자동으로 낮추거나 되돌린다. 단독 실행에서는 바꾼 배율을 자기 Scheduler에 바로 적용한다.

통계 이름:
- GET /..., POST /...      세션의 요청
- [setup] ..., [auth] ...  계정 준비와 토큰 발급. 세션 요청과 나눠 본다
- SESSION ..., SCHEDULE .. 세션 결과, 일정 지연, 배율로 거른 세션(kkugi_load/runner.py)
- ACCOUNT withdrawn         바깥에서 탈퇴된 계정. 다음 세션이 새 이메일로 가입한다(kkugi_load/runner.py)
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime

from locust import HttpUser, constant, events, task
from locust.runners import LocalRunner, MasterRunner, WorkerRunner

from kkugi_load import config as config_module
from kkugi_load import governor
from kkugi_load.capacity import shortage_warning
from kkugi_load.accounts.identity import Account
from kkugi_load.accounts.state import AccountState, StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.control import Applier, Control, add_routes
from kkugi_load.distributed import WorkerGate, setup_master, setup_worker
from kkugi_load.personas import build_account
from kkugi_load.runner import report_missed, report_scaled_out, report_start_delay, run_planned
from kkugi_load.scheduler import Due, Scheduler

log = logging.getLogger("kkugi")

ENV = dict(os.environ)
# 시작할 때 읽은 설정 원문. 다시 읽은 설정의 바꿀 수 없는 값을 이것과 비교한다
CONFIG_TEXT = config_module.read_text(ENV)
CONFIG = config_module.loads(CONFIG_TEXT, ENV)
STORE: StateStore | None = None
SCHEDULER: Scheduler | None = None
GATE: WorkerGate | None = None
ACCOUNTS: dict[int, Account] = {}
STATES: dict[int, AccountState] = {}
# User 생성이 한 번이라도 끝났는가. 그 전에는 User 수가 다 차지 않아 실행기 부족 경고를 하지 않는다
SPAWNED = False
# spawning_complete는 environment를 넘겨주지 않으므로 init에서 받아 둔다
ENVIRONMENT = None


def _warn_capacity(environment) -> None:
    """이 프로세스가 가진 User 수가 오늘 일정의 필요 실행기 수보다 적으면 경고한다."""
    if SCHEDULER is None or SCHEDULER.capacity is None:
        return
    # Worker에서는 자기 User 수다(Runner.user_count = 실행 중인 User greenlet 수)
    message = shortage_warning(SCHEDULER.capacity, environment.runner.user_count)
    if message:
        log.warning(message)


@events.init.add_listener
def _on_init(environment, **_kwargs):
    global STORE, SCHEDULER, GATE, ENVIRONMENT
    ENVIRONMENT = environment
    if isinstance(environment.runner, MasterRunner):
        control = Control(CONFIG.scale, CONFIG_TEXT, ENV)
        publish = setup_master(environment, control)
        # None이면 --enable-rebalancing이 없어 Master를 내리는 중이다
        if publish is not None:
            governor.start(environment, control, publish)
        return
    pool = CONFIG.pool
    STORE = StateStore(CONFIG.state_db)
    indexes = range(pool.start, pool.end)
    accounts = [build_account(CONFIG, i) for i in indexes]
    for account in accounts:
        STATES[account.index] = STORE.load(account.index, account.persona)
    ACCOUNTS.update({account.index: account for account in accounts})
    SCHEDULER = Scheduler(
        CONFIG, accounts, CONFIG.schedule_mode, CONFIG.max_lateness, lambda: datetime.now(CONFIG.tz), pool,
        # 날짜가 바뀌어 새 일정을 만들면 다시 확인한다. 첫 일정은 User 생성이 끝났을 때 확인한다
        on_plan=lambda _capacity: _warn_capacity(environment) if SPAWNED else None,
    )
    log.info(
        "계정 풀 %s: %d-%d(%d개), 시간대 %s, 일정 방식 %s, 상태 DB %s, 시작 배율 %s", pool.name, pool.start,
        pool.end - 1, pool.count, [f"{a:%H:%M}-{b:%H:%M}" for a, b in pool.only_between] or "하루 전체",
        CONFIG.schedule_mode, CONFIG.state_db, CONFIG.scale,
    )
    applier = Applier(SCHEDULER, CONFIG_TEXT, ENV)
    if isinstance(environment.runner, WorkerRunner):
        GATE = setup_worker(environment, pool, applier)
        return
    GATE = WorkerGate(accepted=True)
    # 단독 실행: Master가 없으므로 웹 UI 경로가 바뀐 값을 자기 Scheduler에 바로 적용한다
    control = Control(CONFIG.scale, CONFIG_TEXT, ENV)

    def publish():
        control.record("local", applier.apply(control.payload()))

    if environment.web_ui is not None:
        add_routes(environment.web_ui, control, publish)
    governor.start(environment, control, publish)


@events.spawning_complete.add_listener
def _on_spawning_complete(user_count, **_kwargs):
    # Locust는 이 이벤트에 user_count만 넘긴다. 분산 실행에서는 전체 Worker의 합계라 쓰지 않고, 이 프로세스의
    # User 수를 runner에서 읽는다. Master에는 일정이 없어 확인하지 않는다
    global SPAWNED
    if SCHEDULER is None:
        return
    SPAWNED = True
    # Master가 계정 범위를 승인하기 전이면 일정이 아직 없다. 세션은 실행하지 않고 일정만 만든다
    if not SCHEDULER.plan_if_needed():
        _warn_capacity(ENVIRONMENT)


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
        if not GATE.accepted:
            # Master가 계정 범위를 승인하기 전에는 실행하지 않는다
            return
        item = SCHEDULER.next()
        report_scaled_out(self.environment.events, SCHEDULER.take_scaled_out())
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
            # 다시 읽은 설정이 있으면 그것으로 실행한다
            config = SCHEDULER.config
            api = KkugiApi(self.client, config, account, STATES[account.index], STORE)
            run_planned(self.environment.events, api, config, planned, self.rng)
        finally:
            SCHEDULER.release(planned.account_index)
