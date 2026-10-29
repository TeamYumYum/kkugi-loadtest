"""거버너: 서버가 버거워 보이면 실제 배율을 자동으로 낮추고, 정상이 이어지면 조금씩 되돌린다.

Master(또는 단독 실행)가 [governor] interval_seconds마다 자기 Locust 통계에서 최근 window_seconds 창의 HTTP 요청
실패율과 p99 응답 시간을 본다. 바꾸는 것은 Control의 실제 배율(scale)뿐이고 사람이 정한 배율(requested_scale)이
상한이다(Control.limit_scale). 바꾼 값은 운영 중 조절과 같은 통로(publish)로 Worker에 보낸다.

무엇을 보는가:
- HTTP 요청만 본다. 통계 이름의 메서드 칸(request_type)이 GET/POST 같은 HTTP 메서드인 항목이다. [setup], [auth]가
  붙은 요청도 서버가 받은 요청이라 포함한다. SESSION(생각 시간이 들어 있다), SCHEDULE(missed는 실패로 세지만 실행기
  부족이지 서버 문제가 아니다), ACCOUNT는 뺀다.
- Locust의 "current" 백분위는 10초 창이라 쓰지 않는다. 확인 때마다 HTTP 항목들의 누적 요청 수, 실패 수, 응답 시간
  분포(StatsEntry.response_times: 반올림한 ms → 횟수)를 합친 스냅샷을 남기고, 창 길이만큼 앞선 스냅샷과의 차이로 창
  안의 값을 계산한다. 분포를 합쳐 계산하므로 p99는 Locust와 같은 반올림 단위(100ms 이상은 10ms, 1초 이상은 100ms)다.

판단 규칙(확인 한 번에 하나만 일어난다):
1. 통계가 초기화되었으면(웹 UI의 reset, 테스트 재시작, 누적 값의 감소) 그 창은 판단하지 않고 스냅샷을 새로 쌓는다.
2. 창 길이만큼 앞선 스냅샷이 아직 없으면 판단하지 않는다. 시작한 뒤 첫 창이 찰 때까지다.
3. 창의 시작이 마지막으로 배율이 바뀐 시각(거버너가 낮췄거나 사람이 정한 때)보다 앞서면 판단하지 않는다. 낮춘
   뒤에도 같은 나쁜 창으로 매번 다시 낮추지 않고, 사람이 정한 값도 그 값으로 흐른 창으로만 판단한다.
4. 창 안의 HTTP 요청 수가 min_requests보다 적으면 판단하지 않는다. 밤에 한두 건의 실패로 낮추지 않게 한다.
5. 실패율이 max_failure_ratio를 넘거나 p99가 max_p99_ms를 넘으면 실제 배율을 절반으로 낮춘다.
6. 정상이면 정상 연속 구간을 잇는다. 구간은 처음 정상으로 판단한 창의 시작부터 센다. 그 길이가 recovery_seconds
   이상이고 실제 배율이 사람이 정한 배율보다 낮으면 recovery_step만큼 올리고(상한은 사람이 정한 배율) 구간을 그
   시각부터 다시 센다. 판단하지 않은 확인(4번 등)은 구간을 끊지도 늘리지도 않는다. 다음 정상 판단에서 그 시간까지
   함께 센다.
- 사람이 배율을 정하면(/kkugi/scale, /kkugi/stop) 그 값에서 다시 시작한다. 정상 구간을 지우고 3번의 대기를 둔다.
- [governor]를 다시 읽으면 다음 확인부터 새 값을 쓴다. 끄면(enabled = false) 거버너가 낮춘 배율을 사람이 정한 배율로
  되돌리고 쌓은 스냅샷을 버린다. 다시 켜면 처음부터 쌓는다.

구조:
- http_totals: Locust 통계에서 HTTP 항목만 합친 누적 값(HttpTotals).
- Governor: 판단 로직. 시계와 통계를 함수로 받아 가짜 시계와 가짜 통계로 시험할 수 있다. tick()이 확인 한 번이다.
- start: 주기마다 tick()을 부르는 gevent greenlet. 배율이 바뀌면 publish로 Worker(단독 실행은 자기 Scheduler)에 알린다.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import gevent

from kkugi_load.config import GovernorSettings

log = logging.getLogger("kkugi")

# 통계 항목의 메서드 칸이 이 중 하나면 HTTP 요청이다. 나머지(SESSION, SCHEDULE, ACCOUNT)는 우리가 직접 남긴 항목이다
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
PERCENTILE = 0.99


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class HttpTotals:
    """HTTP 항목들의 누적 값. epoch는 통계 객체가 통째로 바뀌었는지(테스트 재시작) 알아보는 표지다."""

    epoch: object
    requests: int
    failures: int
    # 반올림한 응답 시간(ms) → 횟수
    response_times: dict[int, int]


@dataclass(frozen=True)
class Window:
    """창 하나의 HTTP 요청 값."""

    seconds: float
    requests: int
    failures: int
    # 창 안에 응답 시간이 하나도 없으면 None
    p99_ms: int | None

    @property
    def failure_ratio(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    def describe(self) -> str:
        p99 = "없음" if self.p99_ms is None else f"{self.p99_ms}ms"
        return (
            f"최근 {self.seconds:.0f}초 HTTP 요청 {self.requests}건, 실패율 {self.failure_ratio:.2%}"
            f"({self.failures}건), p99 {p99}"
        )

    def as_dict(self) -> dict:
        return {
            "seconds": round(self.seconds, 1), "requests": self.requests, "failures": self.failures,
            "failure_ratio": round(self.failure_ratio, 4), "p99_ms": self.p99_ms,
        }


@dataclass(frozen=True)
class _Snapshot:
    at: float
    totals: HttpTotals


def http_totals(stats) -> HttpTotals:
    """Locust RequestStats에서 HTTP 항목만 합친다. 항목의 메서드 칸이 HTTP_METHODS에 있는 것만 센다."""
    requests = failures = 0
    response_times: Counter[int] = Counter()
    for entry in list(stats.entries.values()):
        if entry.method not in HTTP_METHODS:
            continue
        requests += entry.num_requests
        failures += entry.num_failures
        response_times.update(entry.response_times)
    # clear_all(테스트 재시작)은 total을 새 객체로 바꾼다. reset_all(웹 UI의 reset)은 같은 객체를 비우므로 누적 값의
    # 감소와 reset_stats 이벤트(start)로 알아본다
    return HttpTotals(epoch=id(stats.total), requests=requests, failures=failures, response_times=dict(response_times))


def _was_reset(old: HttpTotals, new: HttpTotals) -> bool:
    if old.epoch != new.epoch or new.requests < old.requests or new.failures < old.failures:
        return True
    return any(new.response_times.get(ms, 0) < count for ms, count in old.response_times.items())


def _percentile(response_times: dict[int, int], percent: float) -> int | None:
    """Locust의 calculate_response_time_percentile과 같은 방식. 큰 값부터 세어 내려간다."""
    total = sum(response_times.values())
    if total == 0:
        return None
    allowed = int(total * percent)
    processed = 0
    for ms in sorted(response_times, reverse=True):
        processed += response_times[ms]
        if total - processed <= allowed:
            return ms
    return 0


def _window(base: _Snapshot, last: _Snapshot) -> Window:
    old, new = base.totals, last.totals
    response_times = {
        ms: count - old.response_times.get(ms, 0)
        for ms, count in new.response_times.items() if count > old.response_times.get(ms, 0)
    }
    return Window(
        seconds=last.at - base.at, requests=new.requests - old.requests, failures=new.failures - old.failures,
        p99_ms=_percentile(response_times, PERCENTILE),
    )


class Governor:
    """판단 로직. control의 실제 배율만 바꾼다. 바꾼 뒤 Worker에 알리는 일은 부르는 쪽이 한다.

    settings: 확인 때마다 부르는 [governor] 값. 다시 읽은 설정이 바로 반영되게 함수로 받는다.
    read_stats: 확인 때마다 부르는 HTTP 누적 값.
    clock: 단조 증가하는 초. 창과 대기 시간을 이것으로 잰다.
    wall: 상태에 보여 줄 시각 문자열.
    """

    def __init__(self, control, settings: Callable[[], GovernorSettings], read_stats: Callable[[], HttpTotals],
                 clock: Callable[[], float], wall: Callable[[], str] = _now_iso):
        self.control = control
        self.settings = settings
        self.read_stats = read_stats
        self.clock = clock
        self.wall = wall
        self._history: list[_Snapshot] = []
        # 마지막으로 배율이 바뀐 시각(clock). 창의 시작이 이보다 앞서면 판단하지 않는다
        self._changed_at: float | None = None
        # 정상 연속 구간의 시작(clock). 없으면 None
        self._healthy_since: float | None = None
        # 사람이 배율을 정한 횟수(Control.requested_changes)를 마지막으로 본 값
        self._seen_requested = control.requested_changes
        # reset_stats 이벤트를 받았는가. 다음 확인에서 스냅샷을 새로 쌓는다
        self._reset_pending = False
        # /kkugi/control에 보여 줄 상태
        self.enabled = settings().enabled
        self.checked_at: str | None = None
        self.last_window: Window | None = None
        self.last_result: str | None = None
        self.last_change: dict | None = None

    def mark_reset(self) -> None:
        """통계가 초기화되었음을 알린다(웹 UI의 reset 버튼)."""
        self._reset_pending = True

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "checked_at": self.checked_at,
            "last_window": self.last_window.as_dict() if self.last_window else None,
            "last_result": self.last_result,
            "last_change": self.last_change,
        }

    def tick(self) -> bool:
        """확인 한 번. 실제 배율을 바꿨으면 True를 돌려준다."""
        now = self.clock()
        settings = self.settings()
        self.checked_at = self.wall()
        if not settings.enabled:
            return self._disabled()
        if not self.enabled:
            log.info("거버너를 켬")
            self.enabled = True

        if self.control.requested_changes != self._seen_requested:
            self._seen_requested = self.control.requested_changes
            self._changed_at = now
            self._healthy_since = None
            log.info("사람이 배율을 %s로 정해 거버너가 그 값에서 다시 시작한다", self.control.requested_scale)

        current = _Snapshot(now, self.read_stats())
        if self._reset_pending or (self._history and _was_reset(self._history[-1].totals, current.totals)):
            self._reset_pending = False
            self._history = [current]
            self.last_window = None
            return self._skip("통계가 초기화되어 판단하지 않고 스냅샷을 다시 쌓는다")
        self._history.append(current)

        # 창 길이만큼 앞선 스냅샷 중 가장 최근 것이 창의 시작이다. 확인이 조금씩 늦게 도는 것을 주기의 절반까지 봐준다
        cutoff = now - settings.window_seconds + settings.interval_seconds / 2
        starts = [i for i, snapshot in enumerate(self._history) if snapshot.at <= cutoff]
        if not starts:
            return self._skip(f"창({settings.window_seconds:.0f}초)이 아직 차지 않음")
        del self._history[:starts[-1]]
        base = self._history[0]
        window = _window(base, current)
        self.last_window = window

        if self._changed_at is not None and base.at < self._changed_at:
            return self._skip(f"배율이 바뀐 뒤 새 창이 아직 차지 않음. {window.describe()}")
        if window.requests < settings.min_requests:
            return self._skip(f"요청이 {settings.min_requests}건보다 적어 판단하지 않음. {window.describe()}")

        problems = []
        if window.failure_ratio > settings.max_failure_ratio:
            problems.append(f"실패율 {window.failure_ratio:.2%} > {settings.max_failure_ratio:.2%}")
        if window.p99_ms is not None and window.p99_ms > settings.max_p99_ms:
            problems.append(f"p99 {window.p99_ms}ms > {settings.max_p99_ms:.0f}ms")
        if problems:
            return self._lower(now, window, problems)
        return self._healthy(now, base, window, settings)

    def _lower(self, now: float, window: Window, problems: list[str]) -> bool:
        self._healthy_since = None
        old = self.control.scale
        new = self.control.limit_scale(old / 2)
        if new == old:
            return self._skip(f"기준을 넘었지만 배율이 이미 {old}다: {', '.join(problems)}. {window.describe()}")
        # 낮춘 뒤 창이 새로 찰 때까지 판단하지 않는다(판단 규칙 3)
        self._changed_at = now
        return self._changed(old, new, f"기준 초과로 낮춤: {', '.join(problems)}. {window.describe()}", log.warning)

    def _healthy(self, now: float, base: _Snapshot, window: Window, settings: GovernorSettings) -> bool:
        if self._healthy_since is None:
            # 이 창 전체가 정상이었으므로 창의 시작부터 센다
            self._healthy_since = base.at
        healthy_for = now - self._healthy_since
        old, ceiling = self.control.scale, self.control.requested_scale
        if old >= ceiling or healthy_for < settings.recovery_seconds:
            return self._skip(f"정상({healthy_for:.0f}초째). {window.describe()}")
        new = self.control.limit_scale(round(min(old + settings.recovery_step, ceiling), 6))
        self._healthy_since = now
        return self._changed(
            old, new, f"{healthy_for:.0f}초 동안 정상이라 올림(상한 {ceiling}). {window.describe()}", log.info,
        )

    def _disabled(self) -> bool:
        changed = False
        if self.enabled:
            old, ceiling = self.control.scale, self.control.requested_scale
            if old < ceiling:
                new = self.control.limit_scale(ceiling)
                self._changed(old, new, "거버너를 꺼서 사람이 정한 배율로 되돌림", log.info)
                changed = True
            log.info("거버너를 끔")
        self.enabled = False
        self._history = []
        self._changed_at = self._healthy_since = None
        self._reset_pending = False
        self.last_window = None
        self.last_result = "꺼짐"
        return changed

    def _skip(self, result: str) -> bool:
        self.last_result = result
        log.info("거버너 판단: 배율 %s 유지. %s", self.control.scale, result)
        return False

    def _changed(self, old: float, new: float, reason: str, emit) -> bool:
        self.last_result = reason
        self.last_change = {"at": self.wall(), "from": old, "to": new, "reason": reason}
        emit("거버너가 배율을 %s에서 %s로 바꿈: %s", old, new, reason)
        return True


def start(environment, control, publish: Callable[[], None], clock: Callable[[], float] | None = None) -> Governor:
    """Master(또는 단독 실행)에서 거버너를 돌린다. 배율을 바꾸면 publish로 알린다.

    시작하자마자 첫 스냅샷을 남기고, 그 뒤로 [governor] interval_seconds마다 확인한다. 꺼져 있어도 greenlet은 돌며
    다시 읽은 설정으로 켜지기를 기다린다.
    """
    governor = Governor(
        control, lambda: control.governor_settings, lambda: http_totals(environment.runner.stats),
        clock or time.monotonic,
    )
    control.governor = governor
    # Locust는 이 이벤트에 인자를 넘기지 않는다(web.py의 reset_stats)
    environment.events.reset_stats.add_listener(lambda **_kwargs: governor.mark_reset())

    def loop():
        while True:
            try:
                if governor.tick():
                    publish()
            except Exception:  # noqa: BLE001 판단 한 번이 실패해도 다음 확인을 계속한다
                log.exception("거버너 확인 실패")
            gevent.sleep(control.governor_settings.interval_seconds)

    gevent.spawn(loop)
    settings = control.governor_settings
    log.info(
        "거버너 시작: %s, 주기 %.0f초, 창 %.0f초, 실패율 기준 %.2f%%, p99 기준 %.0fms, 최소 요청 %d건, 회복 %.0f초마다 %s",
        "켜짐" if settings.enabled else "꺼짐", settings.interval_seconds, settings.window_seconds,
        settings.max_failure_ratio * 100, settings.max_p99_ms, settings.min_requests, settings.recovery_seconds,
        settings.recovery_step,
    )
    return governor
