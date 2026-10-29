"""분산 실행에서 Worker끼리 계정 범위가 겹치지 않게 한다.

Locust Master는 Worker마다 User를 몇 개 돌릴지만 정하고 우리 계정은 모른다. 그래서 계정은 Worker가
설정의 계정 풀로 직접 맡고, Master는 Worker가 알려 준 범위가 실행 중인 다른 Worker와 겹치는지만 본다.

- Worker는 시작할 때 자기 범위를 Master에 등록하고, 승인을 받기 전에는 세션을 실행하지 않는다.
- 겹치면 Master가 나중에 등록한 Worker를 거부하고, 그 Worker는 스스로 종료한다.
- 응답이 끊긴 Worker의 범위는 다시 쓸 수 있다. 같은 계정을 쓰는 Worker를 재시작할 때다.
- Master는 --enable-rebalancing 없이 뜨면 종료한다. 없으면 끊긴 Worker의 User가 남은 Worker로 돌아가지 않는다.
"""

from __future__ import annotations

import logging
from typing import Callable

import gevent
from locust.runners import STATE_MISSING

from kkugi_load.config import Pool

log = logging.getLogger("kkugi")

MSG_REGISTER = "kkugi_pool_register"
MSG_ACCEPTED = "kkugi_pool_accepted"
MSG_REJECTED = "kkugi_pool_rejected"


class PoolRegistry:
    """Master가 Worker별 계정 범위를 기록한다."""

    def __init__(self) -> None:
        self.claims: dict[str, dict] = {}

    def register(self, node_id: str, claim: dict, alive: Callable[[str], bool]) -> str | None:
        """등록하고 None을 돌려준다. 실행 중인 다른 Worker와 겹치면 등록하지 않고 이유를 돌려준다."""
        for other_id, other in self.claims.items():
            if other_id == node_id or not alive(other_id):
                continue
            if claim["start"] < other["end"] and other["start"] < claim["end"]:
                return (
                    f"계정 {claim['start']}-{claim['end'] - 1}({claim['pool']})이 Worker {other_id}의 "
                    f"{other['start']}-{other['end'] - 1}({other['pool']})과 겹침"
                )
        self.claims[node_id] = claim
        return None


def rebalancing_problem(parsed_options) -> str | None:
    """Master 옵션에 --enable-rebalancing이 없으면 이유를 돌려준다. 옵션을 알 수 없으면 꺼진 것으로 본다."""
    if getattr(parsed_options, "enable_rebalancing", False):
        return None
    return (
        "--enable-rebalancing 없이 Master를 띄웠다. 이대로면 Worker가 끊길 때 그 Worker의 User가 "
        "남은 Worker로 돌아가지 않아 실행기가 조용히 줄어든다"
    )


def setup_master(environment) -> PoolRegistry:
    runner = environment.runner
    registry = PoolRegistry()

    problem = rebalancing_problem(environment.parsed_options)
    if problem:
        log.error("Master를 종료한다: %s", problem)
        environment.process_exit_code = 1

        def quit_master():
            # 웹 UI가 init 뒤에 뜨므로 시작을 마친 다음 runner와 웹 UI를 함께 내린다
            runner.quit()
            if environment.web_ui is not None and environment.web_ui.greenlet is not None:
                environment.web_ui.greenlet.kill(block=False)

        gevent.spawn(quit_master)
        return registry

    def alive(node_id: str) -> bool:
        client = runner.clients.get(node_id)
        return client is not None and client.state != STATE_MISSING

    def on_register(environment, msg, **_kwargs):
        reason = registry.register(msg.node_id, msg.data, alive)
        if reason:
            log.error("Worker %s 거부: %s", msg.node_id, reason)
            runner.send_message(MSG_REJECTED, {"reason": reason}, client_id=msg.node_id)
            return
        log.info("Worker %s 계정 풀 %s 승인", msg.node_id, msg.data)
        runner.send_message(MSG_ACCEPTED, None, client_id=msg.node_id)

    runner.register_message(MSG_REGISTER, on_register)
    return registry


class WorkerGate:
    """Worker가 Master의 승인을 받았는지 기록한다. 단독 실행에서는 처음부터 열려 있다."""

    def __init__(self, accepted: bool):
        self.accepted = accepted


def setup_worker(environment, pool: Pool) -> WorkerGate:
    runner = environment.runner
    gate = WorkerGate(accepted=False)

    def on_accepted(environment, msg, **_kwargs):
        gate.accepted = True
        log.info("Master가 계정 풀 %s(%d-%d)를 승인했다", pool.name, pool.start, pool.end - 1)

    def on_rejected(environment, msg, **_kwargs):
        log.error("Master가 계정 풀을 거부해 종료한다: %s", msg.data["reason"])
        # 프로세스 관리자가 알아차리도록 실패 코드로 끝낸다
        environment.process_exit_code = 1
        runner.quit()

    runner.register_message(MSG_ACCEPTED, on_accepted)
    runner.register_message(MSG_REJECTED, on_rejected)
    runner.send_message(MSG_REGISTER, {"pool": pool.name, "start": pool.start, "end": pool.end})
    return gate
