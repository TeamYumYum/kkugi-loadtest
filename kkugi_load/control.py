"""운영 중에 트래픽을 조절한다: 배율, 정지, 설정 다시 읽기.

- 배율(0~1): 계획된 세션을 이 확률로만 실행한다. 거르는 방식은 scheduler.py에 있다. 정지는 배율 0이다.
- 설정 다시 읽기: Master가 자기 설정 파일을 다시 읽어 검증하고 원문 TOML을 Worker에 보낸다. Worker가 각자 자기
  파일을 읽으면 노트북마다 설정이 달라질 수 있어 Master의 설정에 맞춘다. config.FROZEN_KEYS(시간대, API 경로,
  계정, 인증, 인공 요청 헤더, 계정 풀)가 시작할 때와 다르면 Master는 거부하고 아무것도 보내지 않는다. Worker도
  자기 시작 설정과 비교해 다르면 적용하지 않는다. 다시 읽어도 배율은 바뀌지 않는다. [control] scale은 시작할 때만 쓴다.

구조:
- Control: Master(또는 단독 실행)의 상태. 사람이 정한 배율(requested_scale)과 실제로 Worker에 보내는
  배율(scale)을 나눠 둔다. 지금은 둘이 같고, 자동으로 배율을 낮추는 거버너(계획 2-3)는 scale만 requested_scale
  이하에서 바꾼다(limit_scale).
- Applier: Worker(또는 단독 실행)가 Control의 상태(payload)를 자기 Scheduler에 적용한다.
- add_routes: Master 웹 UI에 조절 경로를 더한다. 값은 폼(application/x-www-form-urlencoded)으로 받는다.
    GET  /kkugi/control  현재 배율, 설정 버전, Worker별 적용 상태
    POST /kkugi/scale    scale=<0~1>
    POST /kkugi/stop     배율 0
    POST /kkugi/reload   설정 파일 다시 읽기
  잘못된 값이나 거부한 설정은 400과 이유({"error": ...})를 돌려준다.

전달 메시지(kkugi_load/distributed.py)의 데이터는 payload()의 dict 하나다:
{"scale": 실제 배율, "version": 설정 버전, "config": 다시 읽은 설정 원문 또는 None}. 버전은 Master가 다시 읽을
때마다 1씩 올리고, 다시 읽은 적이 없으면 0이고 config는 None이다. Worker는 원문이 지금 적용한 것과 다를 때만
일정을 다시 만든다. Master가 재시작해 버전이 0부터 다시 세어도 원문으로 비교하므로 어긋나지 않는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from kkugi_load import config as config_module
from kkugi_load.config import read_scale
from kkugi_load.scheduler import Scheduler

log = logging.getLogger("kkugi")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Control:
    """Master(또는 단독 실행)가 Worker에 보낼 조절 상태."""

    def __init__(self, scale: float, startup_text: str, env: dict[str, str]):
        # 사람이 정한 배율. 자동 조절의 상한이다
        self.requested_scale = read_scale(scale)
        # 실제로 Worker에 보내는 배율. requested_scale 이하다
        self.scale = self.requested_scale
        # 시작할 때 읽은 설정 원문. 다시 읽은 설정의 FROZEN_KEYS를 이것과 비교한다
        self.startup_text = startup_text
        self.env = env
        # 마지막으로 다시 읽은 설정 원문과 버전. 다시 읽은 적이 없으면 None과 0
        self.config_text: str | None = None
        self.config_version = 0
        self.reloaded_at: str | None = None
        # Worker 노드 id(단독 실행은 "local") → 마지막으로 알려 온 적용 상태(Applier.apply의 반환값)
        self.workers: dict[str, dict] = {}

    def set_requested_scale(self, value: object) -> float:
        """사람이 배율을 정한다. 실제 배율도 같은 값이 된다. 0~1 밖이면 ValueError."""
        scale = read_scale(value)
        self.requested_scale = self.scale = scale
        log.info("배율을 %s로 정함", scale)
        return scale

    def limit_scale(self, value: float) -> float:
        """사람이 정한 배율을 넘지 않는 범위에서 실제 배율만 바꾼다. 자동 조절(거버너)용이다."""
        self.scale = min(read_scale(value), self.requested_scale)
        return self.scale

    def reload(self) -> int:
        """설정 파일을 다시 읽어 검증한다. 통과하면 버전을 올리고 새 버전을 돌려준다.

        읽거나 검증하지 못했거나 바꿀 수 없는 값이 다르면 아무것도 바꾸지 않고 ValueError를 낸다.
        """
        try:
            text = config_module.read_text(self.env)
            config_module.loads(text, self.env)
            changed = config_module.frozen_changes(self.startup_text, text)
        except Exception as e:  # noqa: BLE001 파일 오류, TOML 문법 오류, 검증 실패를 모두 이유로 돌려준다
            raise ValueError(f"설정을 읽지 못함: {type(e).__name__}: {e}") from e
        if changed:
            raise ValueError(f"다시 읽어도 바꿀 수 없는 설정이 시작할 때와 다름: {', '.join(changed)}")
        self.config_text = text
        self.config_version += 1
        self.reloaded_at = _now_iso()
        log.info("설정을 다시 읽음: 버전 %d", self.config_version)
        return self.config_version

    def payload(self) -> dict:
        """Worker에 보내는 데이터. 승인 메시지와 조절 메시지에 같은 형태로 싣는다."""
        return {"scale": self.scale, "version": self.config_version, "config": self.config_text}

    def record(self, node_id: str, status: dict) -> None:
        self.workers[node_id] = {**status, "reported_at": _now_iso()}

    def snapshot(self, alive: Callable[[str], bool] | None = None) -> dict:
        workers = {}
        for node_id, status in self.workers.items():
            workers[node_id] = dict(status)
            if alive is not None:
                workers[node_id]["alive"] = alive(node_id)
        return {
            "requested_scale": self.requested_scale,
            "scale": self.scale,
            "config_version": self.config_version,
            "config_reloaded_at": self.reloaded_at,
            "workers": workers,
        }


class Applier:
    """Worker(또는 단독 실행)가 받은 조절 상태를 자기 Scheduler에 적용한다."""

    def __init__(self, scheduler: Scheduler, startup_text: str, env: dict[str, str]):
        self.scheduler = scheduler
        self.startup_text = startup_text
        self.env = env
        # 지금 일정에 쓴 설정 원문과 그 버전. 처음에는 자기 파일이다
        self.applied_text = startup_text
        self.version = 0
        # 적용하지 못한 원문과 이유. 배율만 바뀌어 같은 원문이 다시 와도 오류 로그를 반복하지 않는다
        self._rejected: tuple[str, str] | None = None

    def apply(self, payload: dict) -> dict:
        """배율과 (바뀌었으면) 설정을 적용하고 적용 상태를 돌려준다. 실패해도 예외를 내지 않는다.

        돌려주는 값: {"scale": 지금 배율, "version": 지금 일정의 설정 버전, "error": 마지막 실패 이유 또는 None}
        """
        error = None
        try:
            self.scheduler.set_scale(payload["scale"])
        except (KeyError, ValueError) as e:
            error = f"배율을 적용하지 못함: {e}"
            log.error(error)

        text = payload.get("config")
        if text is not None and text != self.applied_text:
            if self._rejected and self._rejected[0] == text:
                error = self._rejected[1]
            else:
                try:
                    self._replan(text)
                    self.version = payload.get("version", self.version)
                except Exception as e:  # noqa: BLE001 받은 설정이 무엇이든 지금 일정을 지키고 이유를 알린다
                    reason = f"설정 버전 {payload.get('version')}을 적용하지 않음: {e}"
                    self._rejected = (text, reason)
                    error = reason
                    log.error(reason)
        elif text is not None:
            self.version = payload.get("version", self.version)

        log.info("조절 적용: 배율 %s, 설정 버전 %d", self.scheduler.scale, self.version)
        return {"scale": self.scheduler.scale, "version": self.version, "error": error}

    def _replan(self, text: str) -> None:
        changed = config_module.frozen_changes(self.startup_text, text)
        if changed:
            raise ValueError(f"바꿀 수 없는 설정이 이 Worker의 시작 설정과 다름: {', '.join(changed)}")
        old = self.scheduler.config
        # 계정 풀, 일정 방식, 상태 DB는 시작할 때의 것을 그대로 쓴다. 맡은 계정과 상태가 바뀌면 안 된다
        new = replace(
            config_module.loads(text, self.env), pool=old.pool, schedule_mode=old.schedule_mode,
            state_db=old.state_db,
        )
        self.scheduler.replan(new)
        self.applied_text = text
        self._rejected = None


def add_routes(web_ui, control: Control, publish: Callable[[], None],
               alive: Callable[[str], bool] | None = None) -> None:
    """Master 웹 UI(Flask)에 조절 경로를 더한다. 상태를 바꾸면 publish로 Worker에 알린다."""
    from flask import Response, request

    app = web_ui.app
    guard = web_ui.auth_required_if_enabled

    def jsonify(body: dict, status: int = 200) -> Response:
        # 한국어 이유를 curl에서 그대로 읽을 수 있게 유니코드 이스케이프로 바꾸지 않는다
        return Response(json.dumps(body, ensure_ascii=False, indent=2) + "\n", status=status,
                        mimetype="application/json")

    def bad(reason: str):
        return jsonify({"error": reason}, 400)

    @app.route("/kkugi/control", methods=["GET"])
    @guard
    def kkugi_control():
        return jsonify(control.snapshot(alive))

    @app.route("/kkugi/scale", methods=["POST"])
    @guard
    def kkugi_scale():
        if "scale" not in request.form:
            return bad("폼 값 scale이 없다. 예: -d scale=0.5")
        try:
            control.set_requested_scale(request.form["scale"].strip())
        except ValueError as e:
            return bad(str(e))
        publish()
        return jsonify(control.snapshot(alive))

    @app.route("/kkugi/stop", methods=["POST"])
    @guard
    def kkugi_stop():
        control.set_requested_scale(0.0)
        publish()
        return jsonify(control.snapshot(alive))

    @app.route("/kkugi/reload", methods=["POST"])
    @guard
    def kkugi_reload():
        try:
            control.reload()
        except ValueError as e:
            log.error("설정 다시 읽기 거부: %s", e)
            return bad(str(e))
        publish()
        return jsonify(control.snapshot(alive))
