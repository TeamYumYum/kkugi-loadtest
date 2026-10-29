"""조회 세션. 앱을 열어 홈 화면만 보고 닫는다.

요청 순서: 요약, 목표, 성장.

모두 화면이 자동으로 부르는 조회라 생각 시간을 두지 않는다. 요약의 date가 요청한 날짜인지 확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kkugi_load.sessions import steps
from kkugi_load.sessions.base import SessionContext, ValidationFailed


@dataclass(frozen=True)
class ViewInputs:
    planned_at: datetime


def run(ctx: SessionContext, view: ViewInputs) -> dict:
    """홈 화면을 열고 요약을 검증한다."""
    day = view.planned_at.date().isoformat()
    summary = steps.open_home(ctx.api, day)
    if summary.get("date") != day:
        raise ValidationFailed(f"요약 날짜 {summary.get('date')}가 요청한 날짜 {day}와 다름")
    return {"date": day, "intake_kcal": summary.get("intakeKcal")}
