"""예정된 세션 하나를 실행하고 결과를 Locust 통계에 남긴다.

통계 이름:
- SESSION <이름>            세션 하나의 성공·실패와 걸린 시간(생각 시간 포함)
- SESSION <이름> (skipped)  이미 기록해서 실행하지 않은 세션
- SESSION [setup] <이름>    세션 전 계정 준비(로그인, 온보딩)가 실패한 세션
- SCHEDULE start_delay      계획 시각보다 늦게 시작한 정도
- SCHEDULE missed           너무 늦어 실행하지 않은 세션(실패로 센다)
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Callable

from kkugi_load.client.api import ApiError, KkugiApi
from kkugi_load.config import Config
from kkugi_load.personas.base import PlannedSession
from kkugi_load.sessions import meal_log, steps
from kkugi_load.sessions.base import SessionContext, SessionSkipped

log = logging.getLogger("kkugi")

# 세션 종류 이름 → 실행 함수
SESSIONS: dict[str, Callable] = {"meal_log": meal_log.run}

OK, SKIPPED, FAILED = "ok", "skipped", "failed"


def report_start_delay(events, delay_ms: float) -> None:
    """계획 시각보다 늦게 시작한 정도. 통계의 SCHEDULE start_delay로 본다."""
    events.request.fire(
        request_type="SCHEDULE", name="start_delay", response_time=delay_ms, response_length=0, exception=None,
        context={},
    )


def report_missed(events, planned: PlannedSession, reason: str) -> None:
    """실행하지 못한 세션. 통계의 SCHEDULE missed에 실패로 남는다."""
    events.request.fire(
        request_type="SCHEDULE", name="missed", response_time=0, response_length=0,
        exception=RuntimeError(f"{planned.name}: {reason}"), context={"account": planned.account_index},
    )
    log.warning("계정 %d %s 놓침: %s", planned.account_index, planned.name, reason)


def run_planned(events, api: KkugiApi, config: Config, planned: PlannedSession, rng: random.Random) -> str:
    def fire(name: str, started: float, exception: Exception | None) -> None:
        events.request.fire(
            request_type="SESSION", name=name, response_time=(time.perf_counter() - started) * 1000,
            response_length=0, exception=exception, context={"account": planned.account_index},
        )

    started = time.perf_counter()
    try:
        steps.ensure_ready(api)
    except Exception as e:  # noqa: BLE001 실패를 통계에 남기고 다음 세션을 계속한다
        fire(f"[setup] {planned.name}", started, e)
        log.error("계정 %d 준비 실패(%s): %s", planned.account_index, planned.name, e)
        return FAILED

    started = time.perf_counter()
    ctx = SessionContext(api=api, config=config, now=lambda: datetime.now(config.tz), sleep=time.sleep, rng=rng)
    try:
        outcome = SESSIONS[planned.session](ctx, planned.inputs)
    except SessionSkipped as e:
        fire(f"{planned.name} (skipped)", started, None)
        log.info("계정 %d %s 건너뜀: %s", planned.account_index, planned.name, e)
        return SKIPPED
    except Exception as e:  # noqa: BLE001
        fire(planned.name, started, e)
        if isinstance(e, ApiError):
            log.error("계정 %d %s 실패: %s", planned.account_index, planned.name, e)
        else:
            log.exception("계정 %d %s 실패", planned.account_index, planned.name)
        return FAILED
    fire(planned.name, started, None)
    log.info("계정 %d %s 완료: %s", planned.account_index, planned.name, outcome)
    return OK
