"""다이어터와 운동 초보자가 공유하는 운동 입력 생성기. 기존 난수 태그와 순서를 유지한다."""

from datetime import date, datetime

from kkugi_load.accounts.identity import Account, seeded_uniforms
from kkugi_load.config import Config
from kkugi_load.sessions.workout import (
    BODY_PARTS, EXERCISES_PER_ROUTINE, FEMALE_STRENGTH, MISSING_EXERCISES, PARTS_PER_WORKOUT, POOL_SEEDS_PER_PART,
    SETS_PER_EXERCISE, WorkoutInputs,
)


def workout_inputs(config: Config, account: Account, day: date, at: datetime, *, scheduled: bool = False) -> WorkoutInputs | None:
    """입력과 주간 목표를 생성한다. scheduled=True면 이미 정한 운동일이므로 일일 확률 검사를 생략한다."""
    workout = config.personas[account.persona]["workout"]
    # 루틴 값: 부위 섞기에 부위 수만큼, 부위별 종목 고르기에 루틴 종목 수만큼
    routine_slots = len(BODY_PARTS) + EXERCISES_PER_ROUTINE
    pool_slots = len(BODY_PARTS) * POOL_SEEDS_PER_PART
    # 계정 고정 값: 주간 목표, 루틴 수, 루틴 값, 루틴 직접 만들기, 개인차, 종목 풀 순서다
    fixed = seeded_uniforms(config.seed, "workout_plan", account.index, count=4 + routine_slots + pool_slots)
    low, high = int(workout["weekly_target_min"]), int(workout["weekly_target_max"])
    target = low + int(fixed[0] * (high - low + 1))
    # 날짜 값: 기록 여부, 운동 시간, 둘러보기 여부, 부위, 루틴 고르기 각 1개, 세트마다 하나씩, 자유 운동 종목마다
    # 하나씩, 오늘 부위 고르기 2개, 못 찾는 검색의 여부·검색어·대신할 종목 각 1개 순서다
    sets = EXERCISES_PER_ROUTINE * SETS_PER_EXERCISE
    parts_at = 5 + sets + EXERCISES_PER_ROUTINE
    missing_at = parts_at + PARTS_PER_WORKOUT
    u = seeded_uniforms(config.seed, "workout", account.index, day.isoformat(), count=missing_at + 3)
    if not scheduled and u[0] >= target / 7:
        return None
    minutes_min, minutes_max = int(workout["duration_minutes_min"]), int(workout["duration_minutes_max"])
    browse = u[2] < float(workout["browse_probability"])
    body = account.profile.body
    gender = FEMALE_STRENGTH if body.gender == "FEMALE" else 1.0
    strength = float(workout["strength"]) * gender * (0.8 + 0.4 * fixed[3 + routine_slots])
    missing = None
    if u[missing_at] < float(workout["missing_exercise_probability"]):
        keywords = list(MISSING_EXERCISES)
        missing = keywords[int(u[missing_at + 1] * len(keywords))]
    inputs = WorkoutInputs(
        planned_at=at,
        weekly_target=target,
        routine_count=1 + int(fixed[1] * int(workout["routines_max"])),
        routine_seeds=fixed[2:2 + routine_slots],
        routine_pick=int(u[4] * 1000),
        browse_body_part=BODY_PARTS[int(u[3] * len(BODY_PARTS))] if browse else None,
        duration_minutes=minutes_min + int(u[1] * (minutes_max - minutes_min + 1)),
        set_rolls=u[5:5 + sets],
        custom_routine=fixed[2 + routine_slots] < float(workout["custom_routine_probability"]),
        body_weight_kg=body.weight_kg,
        strength=strength,
        reps_range=(int(workout["reps_min"]), int(workout["reps_max"])),
        free_seeds=u[5 + sets:parts_at],
        pool_seeds=fixed[4 + routine_slots:],
        day_parts=pick_parts(u[parts_at:missing_at]),
        missing_keyword=missing,
        missing_seed=u[missing_at + 2],
    )
    return inputs


def pick_parts(uniforms: tuple[float, ...]) -> tuple[str, ...]:
    pool = list(BODY_PARTS)
    for i, x in enumerate(uniforms):
        j = i + int(x * (len(pool) - i))
        pool[i], pool[j] = pool[j], pool[i]
    return tuple(pool[:len(uniforms)])
