"""분산 실행에서 Worker끼리 계정 범위가 겹치지 않게 한다.

Locust Master는 Worker마다 User를 몇 개 돌릴지만 정하고 우리 계정은 모른다. 그래서 계정은 Worker가
설정의 계정 풀로 직접 맡고, Master는 Worker가 알려 준 범위가 실행 중인 다른 Worker와 겹치는지만 본다.

- Worker는 시작할 때 자기 범위를 Master에 등록하고, 승인을 받기 전에는 세션을 실행하지 않는다.
- 겹치면 Master가 나중에 등록한 Worker를 거부하고, 그 Worker는 스스로 종료한다.
- 응답이 끊긴 Worker의 범위는 다시 쓸 수 있다. 같은 계정을 쓰는 Worker를 재시작할 때다.
- Master는 --enable-rebalancing 없이 뜨면 종료한다. 없으면 끊긴 Worker의 User가 남은 Worker로 돌아가지 않는다.

운영 중 조절(kkugi_load/control.py)도 같은 통로로 보낸다.
- 승인 메시지(MSG_ACCEPTED)에 Control.payload()를 싣는다. 나중에 붙은 Worker도 현재 배율과 (다시 읽은 적이
  있으면) 설정을 받고, 그것을 적용한 다음에 세션을 실행하기 시작한다.
- 배율이나 설정이 바뀌면 Master가 MSG_CONTROL로 같은 payload를 모든 Worker에 보낸다.
- Worker는 승인이나 MSG_CONTROL을 적용할 때마다 MSG_APPLIED로 적용 상태(Applier.apply의 반환값)를 알린다.
  Master는 이것을 웹 UI의 /kkugi/control에 Worker별로 보여 준다.

거버너(kkugi_load/governor.py)도 Master에서 돈다. Worker들이 보낸 통계가 모이는 곳이 Master이기 때문이다.
setup_master가 돌려주는 publish를 locustfile.py가 거버너에 넘긴다. 거버너가 실제 배율을 바꾸면 사람이 바꿀 때와
같은 MSG_CONTROL로 보낸다. 웹 UI가 없어도 거버너는 돈다.
"""

from __future__ import annotations

import logging
from typing import Callable

import gevent
from locust.runners import STATE_MISSING

from kkugi_load.config import Pool
from kkugi_load.control import Applier, Control, add_routes

log = logging.getLogger("kkugi")

MSG_REGISTER = "kkugi_pool_register"
MSG_ACCEPTED = "kkugi_pool_accepted"
MSG_REJECTED = "kkugi_pool_rejected"
# Master → Worker: Control.payload() {"scale": float, "version": int, "config": str | None}
MSG_CONTROL = "kkugi_control"
# Worker → Master: Applier.apply()의 반환값 {"scale": float, "version": int, "error": str | None}
MSG_APPLIED = "kkugi_control_applied"


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


def setup_master(environment, control: Control) -> Callable[[], None] | None:
    """Master의 메시지와 웹 UI 경로를 붙이고 Worker에 조절 상태를 보내는 함수(publish)를 돌려준다.

    --enable-rebalancing이 없어 Master를 내리는 중이면 None을 돌려준다.
    """
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
        return None

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
        runner.send_message(MSG_ACCEPTED, control.payload(), client_id=msg.node_id)

    def on_applied(environment, msg, **_kwargs):
        control.record(msg.node_id, msg.data)
        if msg.data.get("error"):
            log.error("Worker %s 조절 적용 실패: %s", msg.node_id, msg.data)
        else:
            log.info("Worker %s 조절 적용: %s", msg.node_id, msg.data)

    def publish():
        runner.send_message(MSG_CONTROL, control.payload())

    runner.register_message(MSG_REGISTER, on_register)
    runner.register_message(MSG_APPLIED, on_applied)
    if environment.web_ui is not None:
        add_routes(environment.web_ui, control, publish, alive)
    else:
        log.warning("웹 UI 없이 Master를 띄워 배율과 설정을 운영 중에 바꿀 수 없다")
    return publish


class WorkerGate:
    """Worker가 Master의 승인을 받았는지 기록한다. 단독 실행에서는 처음부터 열려 있다."""

    def __init__(self, accepted: bool):
        self.accepted = accepted


def setup_worker(environment, pool: Pool, applier: Applier) -> WorkerGate:
    runner = environment.runner
    gate = WorkerGate(accepted=False)

    def apply(payload):
        runner.send_message(MSG_APPLIED, applier.apply(payload))

    def on_accepted(environment, msg, **_kwargs):
        log.info("Master가 계정 풀 %s(%d-%d)를 승인했다", pool.name, pool.start, pool.end - 1)
        # 현재 배율과 설정을 먼저 적용한다. 그 전에 문을 열면 시작 설정의 배율로 세션이 나갈 수 있다
        apply(msg.data)
        gate.accepted = True

    def on_control(environment, msg, **_kwargs):
        apply(msg.data)

    def on_rejected(environment, msg, **_kwargs):
        log.error("Master가 계정 풀을 거부해 종료한다: %s", msg.data["reason"])
        # 프로세스 관리자가 알아차리도록 실패 코드로 끝낸다
        environment.process_exit_code = 1
        runner.quit()

    runner.register_message(MSG_ACCEPTED, on_accepted)
    runner.register_message(MSG_REJECTED, on_rejected)
    runner.register_message(MSG_CONTROL, on_control)
    runner.send_message(MSG_REGISTER, {"pool": pool.name, "start": pool.start, "end": pool.end})
    return gate
