"""몰아 기록 세션. 밤에 앱을 한 번 열어 그날의 끼니, 체중, 운동을 차례로 기록한다.

순서: 끼니마다 식사 기록(meal_log), 체중 기록(weight_log), 운동 기록(workout). 각 부분은 기존 세션을 그대로
실행하므로 요청 흐름과 검증도 같다. 부분마다 계획 시각(planned_at)이 실제로 먹고 잰 시각이라, 밤에 기록해도 식사
시각(eatenAt)과 운동 시각은 그 시각으로 들어간다.

이미 기록한 부분(SessionSkipped)은 건너뛰고 다음 부분을 계속한다. 모든 부분을 건너뛰었을 때만 세션 전체를 건너뛴 것으로
본다. 그 밖의 실패는 그 자리에서 세션 전체의 실패다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kkugi_load.sessions import meal_log, weight_log, workout
from kkugi_load.sessions.base import SessionContext, SessionSkipped
from kkugi_load.sessions.meal_log import MealInputs
from kkugi_load.sessions.weight_log import WeightInputs
from kkugi_load.sessions.workout import WorkoutInputs


@dataclass(frozen=True)
class BatchInputs:
    # 앱을 여는 시각. 세션 일정과 배율 판정에 쓴다
    planned_at: datetime
    # 끼니 시각순
    meals: tuple[MealInputs, ...]
    weight: WeightInputs | None
    workout: WorkoutInputs | None


def run(ctx: SessionContext, inputs: BatchInputs) -> dict:
    """그날의 기록을 몰아서 올리고 부분별 결과를 돌려준다. 모든 부분이 이미 기록되어 있으면 SessionSkipped."""
    outcome: dict = {"meals": {}, "skipped": []}
    parts = [(meal.meal_type, meal_log.run, meal) for meal in inputs.meals]
    if inputs.weight is not None:
        parts.append(("WEIGHT", weight_log.run, inputs.weight))
    if inputs.workout is not None:
        parts.append(("WORKOUT", workout.run, inputs.workout))
    for name, run_part, part_inputs in parts:
        try:
            result = run_part(ctx, part_inputs)
        except SessionSkipped:
            outcome["skipped"].append(name)
            continue
        if name == "WEIGHT":
            outcome["weight"] = result
        elif name == "WORKOUT":
            outcome["workout"] = result
        else:
            outcome["meals"][name] = result
    if parts and len(outcome["skipped"]) == len(parts):
        raise SessionSkipped("그날의 기록을 이미 모두 올림")
    return outcome
