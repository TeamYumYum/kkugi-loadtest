"""체중 기록 세션. 앱을 열고, 현재 몸무게를 보고, 오늘 체중을 저장한 뒤 최근 30일 그래프를 본다.

요청 순서: 요약, 목표, 성장, 현재 몸무게, 체중 저장, 체중 목록(30일).

체중 저장은 날짜별 덮어쓰기(PUT)라 처음이면 201, 이미 있으면 200이다. 같은 세션을 다시 실행해도 그날
기록은 한 건이므로 건너뛰기 판단을 두지 않는다. 날짜는 계획 시각(한국 시간)의 날짜다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from kkugi_load.client.api import Expect
from kkugi_load.sessions import steps
from kkugi_load.sessions.base import SessionContext, ValidationFailed

# API가 받는 체중 범위(kg). 벗어나면 WEIGHT_OUT_OF_RANGE(400)
MIN_KG = 20.0
MAX_KG = 300.0
# 그래프가 보여 주는 기간. 오늘을 포함한 30일
GRAPH_DAYS = 30
# 서버는 소수 둘째 자리로 반올림해 저장한다. 반올림 방식 차이를 넘는 차이만 불일치로 본다
KG_TOLERANCE = 0.005 + 1e-9


@dataclass(frozen=True)
class WeightInputs:
    planned_at: datetime
    weight_kg: float


def drifted_weight(start_kg: float, days: int, trend_kg_per_day: float, noise: float) -> float:
    """그날 체중. 시작 체중에 날짜만큼의 추세와 흔들림을 더하고, API 범위로 자른 뒤 0.1kg로 반올림한다.

    days는 기준일부터 지난 날 수, noise는 호출하는 쪽이 계정·날짜로 정한 흔들림(kg)이다. 같은 입력이면
    같은 값을 돌려주므로 세션을 다시 실행해도 같은 체중을 보낸다.
    """
    weight = start_kg + days * trend_kg_per_day + noise
    return round(min(MAX_KG, max(MIN_KG, weight)), 1)


def _same_kg(got, want: float) -> bool:
    return got is not None and abs(float(got) - want) <= KG_TOLERANCE


def run(ctx: SessionContext, inputs: WeightInputs) -> dict:
    """오늘 체중을 기록하고 검증 결과를 돌려준다."""
    api = ctx.api
    day = inputs.planned_at.date()
    weight = round(inputs.weight_kg, 2)

    # 앱을 열면 화면이 자동으로 부르는 조회. 사이에 생각 시간을 두지 않는다
    steps.open_home(api, day.isoformat())
    api.call("GET", "/weight-records/latest", name="GET /weight-records/latest")

    # 사용자가 체중을 입력하는 시간
    ctx.think(ctx.config.select_think)
    # 201은 새로 기록, 200은 그날 기록을 바꾼 경우다
    saved = api.call(
        "PUT", f"/weight-records/{day.isoformat()}", name="PUT /weight-records/{recordedOn}",
        json_body={"weightKg": weight}, expect=Expect(ok=(201, 200)),
    ).data
    _check_saved(saved, day, weight)

    # 저장 뒤 화면이 부르는 최근 30일 그래프. 오늘 기록이 저장한 체중으로 있어야 한다
    items = api.call(
        "GET", "/weight-records", name="GET /weight-records",
        params={"from": (day - timedelta(days=GRAPH_DAYS - 1)).isoformat(), "to": day.isoformat()},
    ).data["items"]
    today = [item for item in items if item.get("recordedOn") == day.isoformat()]
    if not today:
        raise ValidationFailed(f"체중 목록에 {day.isoformat()} 기록이 없음")
    if not _same_kg(today[0].get("weightKg"), weight):
        raise ValidationFailed(f"체중 목록의 {day.isoformat()} 체중 {today[0].get('weightKg')}가 보낸 값 {weight}와 다름")

    return {"date": day.isoformat(), "weight_kg": weight}


def _check_saved(saved: dict, day: date, weight: float) -> None:
    problems = []
    if saved.get("recordedOn") != day.isoformat():
        problems.append(f"recordedOn {saved.get('recordedOn')}")
    if not _same_kg(saved.get("weightKg"), weight):
        problems.append(f"weightKg {saved.get('weightKg')} != {weight}")
    if problems:
        raise ValidationFailed("저장 결과 불일치: " + ", ".join(problems))
