"""운동 기록 세션. 앱을 열고, 루틴과 이번 주 일정을 받고, 끝난 운동 하나를 올리고, 주간 목표를 확인한다.

루틴은 회원이 직접 만드는 방법(POST /routines)밖에 없다. AI 추천 루틴은 서비스의 MVP2 범위라 아직 없다. 그래서
루틴을 직접 만드는 회원(custom_routine)만 루틴을 만들어 그대로 수행하고, 나머지는 루틴 없이 그날 고른 종목으로
자유 운동을 기록한다. 무게와 횟수는 회원의 몸무게와 근력으로 정한다(target_weight).

종목은 회원마다 다르다. 회원은 부위마다 3-4개씩 자주 하는 종목을 갖고(member_pool), 루틴과 자유 운동은 그 안에서
고른다. 루틴은 루틴마다 부위 2개를 맡고, 자유 운동은 그날 고른 부위 2개(day_parts)에서 2개씩 한다. 극히 일부는
하고 싶은 종목을 검색해도 찾지 못하고(missing_keyword), 그 부위 목록을 둘러본 뒤 대신할 종목을 운동 중에 더한다.
서비스에 회원이 종목을 직접 만드는 기능은 없다.

요청 순서:
1. 요약, 목표, 성장(홈 화면)
2. 루틴 목록(GET /routines)
   - 비어 있고 루틴을 직접 만드는 회원이면 준비: 종목 목록(프로세스에 없을 때만, 페이지 수만큼), 주간 목표 정하기,
     루틴 만들기(1-2개)
   - 비어 있고 루틴을 만들지 않는 회원이면 자유 운동이다
3. 이번 주 일정(GET /workout-schedules, 오늘부터 6일 뒤까지)
   - 루틴이 있고 일정이 비어 있으면 주간 목표 횟수만큼 일정 예약(POST /workout-schedules)
4. 종목 둘러보기(GET /exercises?bodyPart=..). 입력에 부위가 있을 때만
5. 최근 7일 운동 기록(GET /workout-sessions). 이 세션의 기록이 이미 있으면 SessionSkipped
   - 못 찾는 검색이면 종목 검색(GET /exercises?keyword=..)과 그 부위 둘러보기(GET /exercises?bodyPart=..)
6. 종목별 이전 기록(GET /workout-sessions/recent-sets). 앱이 세트를 기록하는 동안 보여 주는 이전 기록이다
7. 운동 기록 업로드(POST /workout-sessions). 오늘 미완료 일정이 있으면 그 일정에 연결한다
8. 주간 목표(GET /weekly-workout-goal/me)
   - 자유 운동 회원은 목표가 없으면 이때 정한다(PUT /weekly-workout-goal/me, 준비 요청)

준비 단계의 요청은 통계 이름 앞에 [setup]을 붙인다. 주간 목표를 루틴보다 먼저 정한다. 준비를 다시 하는 조건이
"루틴 목록이 비었다" 하나뿐이라, 루틴을 만든 뒤 목표 요청이 실패하면 목표가 영영 정해지지 않기 때문이다. 목표
PUT은 같은 값을 다시 보내도 결과가 같으므로 먼저 보내도 해가 없다.

세션은 입력과 서버 데이터만으로 결정된다. 같은 입력으로 다시 실행하면 같은 업로드 본문이 나오므로, 응답이 유실된
뒤 재실행해도 서버가 200(같은 내용)으로 받는다. 다만 일정에 연결한 업로드는 그 일정을 완료로 바꾸므로, 다시
올리기 전에 5번 목록에서 이 세션의 clientUuid를 먼저 확인한다(식사 기록 세션의 이미 기록함 검사와 같다).

clientUuid는 식사처럼 세션을 실행할 때 그 시점의 이메일로 정한다(workout_uuid).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from kkugi_load.client.api import Expect, KkugiApi
from kkugi_load.sessions import steps
from kkugi_load.sessions.base import SessionContext, SessionSkipped, ValidationFailed

# 운동 clientUuid를 만드는 이름공간. 바꾸면 이미 올린 운동과 UUID가 달라져 같은 날 두 번 올린다
WORKOUT_UUID_NAMESPACE = uuid.UUID("3d9b7e41-2a6c-4f58-8e1d-5c0a9b4f7e23")

# 루틴과 자유 운동은 부위 2개에서 2종목씩 한다. 종목마다 세트 수
PARTS_PER_WORKOUT = 2
EXERCISES_PER_PART = 2
EXERCISES_PER_ROUTINE = PARTS_PER_WORKOUT * EXERCISES_PER_PART
SETS_PER_EXERCISE = 3
# 회원이 부위마다 갖는 종목 수의 범위. 풀을 정하는 값은 부위마다 크기 1개와 고르기 POOL_MAX개다
POOL_MIN, POOL_MAX = 3, 4
POOL_SEEDS_PER_PART = 1 + POOL_MAX
REST_SECONDS = 90
# 세트 굴림값이 이보다 작으면 그 세트를 건너뛴다(가정)
SKIP_RATE = 0.15
# 실제 횟수 = 목표 + 이 중 하나. 굴림값으로 고른다
_REPS_DELTAS = (-2, -1, 0, 0, 1)
CATALOG_PAGE_SIZE = 100
BROWSE_PAGE_SIZE = 20
# 일정 목록과 기록 목록을 보는 기간(오늘 포함 7일)
WINDOW_DAYS = 7
# 서버의 bodyPart 값이다
BODY_PARTS = ("LOWER_BODY", "BACK", "CHEST", "SHOULDER", "ARM", "CORE")
# 카탈로그에 없는 종목 검색어와, 찾지 못했을 때 대신 둘러볼 부위. 2026-10-30 seed(R__workout_exercise_seed.sql)의
# 한국어·영어 이름에 들어 있지 않음을 확인했다
MISSING_EXERCISES = {
    "케틀벨 스윙": "LOWER_BODY", "배틀 로프": "SHOULDER", "로잉 머신": "BACK", "줄넘기": "LOWER_BODY",
    "클라이밍": "BACK", "필라테스": "CORE",
}

# --- 무게 모델 ---
# 일반 중량(EXTERNAL) 목표 = 몸무게 × 근력 × 장비 비율 × 부위 비율 × (0.9-1.1). 근력 1.0은 운동을 막 시작한 성인
# 남성이다. 덤벨은 한 손 무게다. 비율은 모두 가정이다
EQUIPMENT_RATIO = {
    "BARBELL": 0.6, "SMITH_MACHINE": 0.5, "MACHINE": 0.6, "CABLE": 0.35, "DUMBBELL": 0.15, "PLATE": 0.2,
    "BODYWEIGHT": 0.1,
}
# 카탈로그에 장비가 없거나 모르는 값일 때
DEFAULT_EQUIPMENT_RATIO = 0.3
BODY_PART_RATIO = {"LOWER_BODY": 1.4, "BACK": 1.0, "CHEST": 1.0, "SHOULDER": 0.6, "ARM": 0.4, "CORE": 0.5}
# 여성의 근력 배수(가정). 페르소나가 회원 근력을 정할 때 곱한다
FEMALE_STRENGTH = 0.65
# 바벨은 빈 봉 무게보다 가벼울 수 없다
BARBELL_MIN_KG = 20.0

# 종목 ID → 종목. 프로세스 하나가 한 번 채우고 모든 계정이 함께 쓴다. 폰도 카탈로그 사본을 두고 쓰므로
# 세션마다 다시 받지 않는다. 서버는 제공 중인 종목만 돌려주므로, 루틴에 있는데 여기 없는 종목은 내려간 종목이다
_catalog: dict[int, dict] | None = None


@dataclass(frozen=True)
class WorkoutInputs:
    """운동 세션 입력. 난수는 모두 페르소나가 계정·날짜 해시로 미리 정한다.

    planned_at: 운동을 마친 시각(세션을 여는 시각). 업로드의 endedAt이 된다
    weekly_target: 주간 목표 횟수(1-7). 처음 목표를 정할 때와 일정 예약 수에 쓴다
    routine_count: 처음 만들 루틴 수(1-2). 루틴이 이미 있으면 쓰지 않는다
    routine_seeds: 루틴을 만들 때 쓰는 0 이상 1 미만의 값. 앞에서부터 루틴마다 맡을 부위를 고른다. 모자라면 돌려 쓴다
    routine_pick: 오늘 일정이 없을 때 고를 루틴의 순번 기준값. 루틴 수로 나눈 나머지를 쓴다
    browse_body_part: 종목 둘러보기 부위(LOWER_BODY, BACK, CHEST, SHOULDER, ARM, CORE). None이면 둘러보지 않는다
    duration_minutes: 운동 시간(분). startedAt = endedAt - 이 시간
    set_rolls: 세트마다 쓰는 0 이상 1 미만의 값. 건너뛸지와 실제값의 흔들림을 정한다. 모자라면 돌려 쓴다
    custom_routine: 루틴이 없을 때 직접 만드는지. False면 루틴 없이 자유 운동을 기록한다
    body_weight_kg: 무게 목표의 기준 몸무게
    strength: 회원의 근력 배수. 페르소나 수준, 성별, 개인차를 곱한 값이다
    reps_range: 무게·횟수 종목의 목표 횟수 범위(이상, 이하). 횟수만 세는 종목은 여기에 5를 더한다
    free_seeds: 자유 운동에서 부위마다 풀의 종목을 고르는 0 이상 1 미만의 값. 날마다 다르다
    pool_seeds: 회원의 종목 풀을 정하는 0 이상 1 미만의 값. 부위마다 POOL_SEEDS_PER_PART개. 계정마다 고정이다
    day_parts: 자유 운동에서 오늘 하는 부위. 비어 있으면 BODY_PARTS의 앞 PARTS_PER_WORKOUT개다
    missing_keyword: 찾지 못할 종목 검색어(MISSING_EXERCISES의 키). None이면 검색하지 않는다
    missing_seed: 찾지 못했을 때 그 부위에서 대신할 종목을 고르는 0 이상 1 미만의 값
    """

    planned_at: datetime
    weekly_target: int
    routine_count: int
    routine_seeds: tuple[float, ...]
    routine_pick: int
    browse_body_part: str | None
    duration_minutes: int
    set_rolls: tuple[float, ...]
    custom_routine: bool = True
    body_weight_kg: float = 70.0
    strength: float = 1.0
    reps_range: tuple[int, int] = (8, 12)
    free_seeds: tuple[float, ...] = ()
    pool_seeds: tuple[float, ...] = ()
    day_parts: tuple[str, ...] = ()
    missing_keyword: str | None = None
    missing_seed: float = 0.5


def workout_uuid(email: str, day: date) -> str:
    return str(uuid.uuid5(WORKOUT_UUID_NAMESPACE, f"{email}|{day.isoformat()}|workout"))


def clear_catalog() -> None:
    """종목 사본을 비운다. 시험에서 쓴다."""
    global _catalog
    _catalog = None


def _load_catalog(api: KkugiApi) -> dict[int, dict]:
    """종목 사본. 없으면 전체 목록을 페이지마다 받아 채운다."""
    global _catalog
    if _catalog is not None:
        return _catalog
    catalog: dict[int, dict] = {}
    page = 0
    while True:
        data = api.call(
            "GET", "/exercises", name=f"{steps.SETUP}GET /exercises",
            params={"page": page, "size": CATALOG_PAGE_SIZE},
        ).data
        items = data.get("items") or []
        for item in items:
            catalog[item["id"]] = item
        page += 1
        if not items or len(catalog) >= int(data.get("totalElements") or 0):
            break
    if not catalog:
        raise ValidationFailed("종목 목록이 비어 있음")
    _catalog = catalog
    return catalog


# --- 준비: 루틴 만들기 ---


def _cycled(values: tuple[float, ...], index: int, default: float = 0.5) -> float:
    return values[index % len(values)] if values else default


def target_weight(exercise: dict, body_weight_kg: float, strength: float, seed: float) -> float:
    """무게·횟수 종목의 목표 무게(kg). 무게의 뜻은 weightType마다 다르다.

    일반(EXTERNAL)은 드는 무게, 추가(ADDITIONAL)는 몸무게에 더하는 무게, 보조(ASSISTANCE)는 덜어 주는 무게다.
    근력이 낮으면 추가 무게는 0이고 보조 무게는 크다. 원판 단위로 반올림한다(덤벨 1kg, 보조 5kg, 그 밖 2.5kg).
    """
    weight_type = exercise.get("weightType")
    jitter = 0.9 + 0.2 * seed
    if weight_type == "ADDITIONAL":
        step, low = 2.5, 0.0
        raw = body_weight_kg * max(0.0, strength - 1.0) * 0.3 * jitter
    elif weight_type == "ASSISTANCE":
        step, low = 5.0, 5.0
        raw = body_weight_kg * min(0.8, max(0.1, 1.0 - 0.5 * strength)) * jitter
    else:
        equipment = exercise.get("equipment")
        step = 1.0 if equipment == "DUMBBELL" else 2.5
        low = BARBELL_MIN_KG if equipment == "BARBELL" else step
        ratio = EQUIPMENT_RATIO.get(equipment, DEFAULT_EQUIPMENT_RATIO) * BODY_PART_RATIO.get(exercise.get("bodyPart"), 1.0)
        raw = body_weight_kg * strength * ratio * jitter
    return float(min(1000.0, max(low, round(raw / step) * step)))


def _target(exercise: dict, inputs: WorkoutInputs, seed: float) -> dict:
    """종목의 기록 방식에 맞는 세트 목표. 방식에 없는 값은 null이어야 서버가 받는다."""
    record_type = exercise["recordType"]
    if record_type == "DURATION":
        return {"targetWeightKg": None, "targetReps": None, "targetDurationSeconds": 30 + 15 * int(seed * 3)}
    low, high = inputs.reps_range
    reps = low + int(seed * (high - low + 1))
    if record_type != "WEIGHT_REPS":
        return {"targetWeightKg": None, "targetReps": reps + 5, "targetDurationSeconds": None}
    weight = target_weight(exercise, inputs.body_weight_kg, inputs.strength, seed)
    return {"targetWeightKg": weight, "targetReps": reps, "targetDurationSeconds": None}


def _shuffled(items: list, seeds: tuple[float, ...], count: int) -> list:
    """items에서 겹치지 않게 count개를 고른다. 앞에서부터 섞는 Fisher-Yates를 고를 만큼만 한다."""
    items = list(items)
    count = min(count, len(items))
    for i in range(count):
        j = i + int(_cycled(seeds, i) * (len(items) - i))
        items[i], items[j] = items[j], items[i]
    return items[:count]


def member_pool(catalog: dict[int, dict], pool_seeds: tuple[float, ...]) -> dict[str, list[dict]]:
    """회원이 부위마다 자주 하는 종목 3-4개. 부위 → 종목 목록이다. 그 부위의 종목(ID 순)에서 고른다.

    카탈로그가 같으면 같은 풀이 나온다. 종목이 내려가 카탈로그가 바뀌면 그 부위의 풀은 바뀔 수 있다.
    """
    pool = {}
    for i, part in enumerate(BODY_PARTS):
        seeds = pool_seeds[i * POOL_SEEDS_PER_PART:(i + 1) * POOL_SEEDS_PER_PART]
        size = POOL_MIN + int(_cycled(seeds, 0) * (POOL_MAX - POOL_MIN + 1))
        candidates = sorted((e for e in catalog.values() if e.get("bodyPart") == part), key=lambda e: e["id"])
        pool[part] = _shuffled(candidates, seeds[1:], size)
    return pool


def _pick_from_pool(pool: dict[str, list[dict]], parts: tuple[str, ...], seeds: tuple[float, ...]) -> list[dict]:
    """parts마다 풀에서 EXERCISES_PER_PART개씩 고른다. 그 부위들의 풀이 비면 풀 전체에서 고른다."""
    chosen = []
    for i, part in enumerate(parts):
        part_seeds = seeds[i * EXERCISES_PER_PART:(i + 1) * EXERCISES_PER_PART]
        chosen += _shuffled(pool.get(part, []), part_seeds, EXERCISES_PER_PART)
    if not chosen:
        chosen = [e for part in BODY_PARTS for e in pool.get(part, [])][:EXERCISES_PER_ROUTINE]
    return chosen


def routine_parts(order: int, routine_seeds: tuple[float, ...]) -> tuple[str, ...]:
    """order번째 루틴이 맡는 부위. 루틴끼리 부위가 겹치지 않게 한 번 섞은 부위 순서를 나눠 갖는다."""
    shuffled = _shuffled(list(BODY_PARTS), routine_seeds, len(BODY_PARTS))
    start = (order * PARTS_PER_WORKOUT) % len(shuffled)
    return tuple(shuffled[start:start + PARTS_PER_WORKOUT])


def _routine_body(catalog: dict[int, dict], order: int, inputs: WorkoutInputs) -> dict:
    """order번째 루틴의 생성 본문. 종목은 회원의 풀에서, 목표는 이 회원의 몸무게와 근력으로 정한다."""
    pool = member_pool(catalog, inputs.pool_seeds)
    chosen = _pick_from_pool(pool, routine_parts(order, inputs.routine_seeds), inputs.routine_seeds[len(BODY_PARTS):])
    return {
        "name": f"꾸기 루틴 {order + 1}",
        "exercises": [
            {
                "exerciseId": exercise["id"],
                "restSeconds": REST_SECONDS,
                "targets": [_target(exercise, inputs, _cycled(inputs.routine_seeds, slot))] * SETS_PER_EXERCISE,
            }
            for slot, exercise in enumerate(chosen)
        ],
    }


def _performed_entry(exercise: dict, order: int, inputs: WorkoutInputs, seed: float) -> dict:
    """루틴 응답의 종목 모양. 자유 운동과 운동 중 더한 종목을 업로드 본문 만들기에 넘길 때 쓴다."""
    return {
        "exerciseId": exercise["id"], "sortOrder": order, "restSeconds": REST_SECONDS,
        "recordType": exercise["recordType"], "weightType": exercise["weightType"],
        "targets": [_target(exercise, inputs, seed)] * SETS_PER_EXERCISE,
    }


def free_workout(catalog: dict[int, dict], inputs: WorkoutInputs) -> dict:
    """자유 운동 구성. 오늘 부위마다 회원의 풀에서 고른다. 루틴 응답과 같은 모양이고 id와 이름은 None이다."""
    parts = inputs.day_parts or BODY_PARTS[:PARTS_PER_WORKOUT]
    chosen = _pick_from_pool(member_pool(catalog, inputs.pool_seeds), parts, inputs.free_seeds)
    return {
        "id": None, "name": None,
        "exercises": [
            _performed_entry(exercise, order, inputs, _cycled(inputs.free_seeds, order - 1))
            for order, exercise in enumerate(chosen, start=1)
        ],
    }


def replacement(catalog: dict[int, dict], keyword: str, seed: float, exclude: set[int]) -> dict | None:
    """찾지 못한 종목 대신 그 부위에서 고를 종목. 오늘 하는 종목은 빼고 고른다. 고를 것이 없으면 None."""
    part = MISSING_EXERCISES[keyword]
    candidates = sorted(
        (e for e in catalog.values() if e.get("bodyPart") == part and e["id"] not in exclude), key=lambda e: e["id"],
    )
    return candidates[int(seed * len(candidates))] if candidates else None


def _setup(api: KkugiApi, inputs: WorkoutInputs) -> list[dict]:
    """처음 한 번 하는 준비. 만든 루틴을 돌려준다."""
    catalog = _load_catalog(api)
    api.call(
        "PUT", "/weekly-workout-goal/me", name=f"{steps.SETUP}PUT /weekly-workout-goal/me",
        json_body={"targetCount": inputs.weekly_target},
    )
    routines = []
    for order in range(max(1, inputs.routine_count)):
        routines.append(api.call(
            "POST", "/routines", name=f"{steps.SETUP}POST /routines",
            json_body=_routine_body(catalog, order, inputs), expect=Expect(ok=(201,)),
        ).data)
    return routines


# --- 일정 ---


def schedule_offsets(weekly_target: int) -> list[int]:
    """오늘부터 며칠 뒤에 일정을 둘지. 7일 안에 고르게 펼치며 첫 일정은 오늘이다."""
    count = min(max(weekly_target, 1), WINDOW_DAYS)
    return [i * WINDOW_DAYS // count for i in range(count)]


def _schedule_week(ctx: SessionContext, inputs: WorkoutInputs, day: date, routines: list[dict]) -> list[dict]:
    # 사용자가 이번 주 운동할 날을 고르는 시간
    ctx.think(ctx.config.select_think)
    created = []
    for i, offset in enumerate(schedule_offsets(inputs.weekly_target)):
        body = {
            "routineId": routines[i % len(routines)]["id"],
            "scheduledDate": (day + timedelta(days=offset)).isoformat(),
        }
        created.append(ctx.api.call(
            "POST", "/workout-schedules", name="POST /workout-schedules", json_body=body, expect=Expect(ok=(201,)),
        ).data)
    return created


# --- 업로드 본문 ---


def _values(target: dict) -> dict:
    return {
        "weightKg": target.get("targetWeightKg"),
        "reps": target.get("targetReps"),
        "durationSeconds": target.get("targetDurationSeconds"),
    }


def _actual(record_type: str, target: dict, spread: float) -> dict:
    """목표에 흔들림을 준 실제값. spread는 0 이상 1 미만이다. 방식에 없는 값은 목표처럼 null로 둔다."""
    actual = dict(target)
    if record_type == "DURATION":
        seconds = round(target["durationSeconds"] * (0.8 + 0.4 * spread))
        actual["durationSeconds"] = min(max(seconds, 1), 86400)
    else:
        reps = target["reps"] + _REPS_DELTAS[int(spread * len(_REPS_DELTAS))]
        actual["reps"] = min(max(reps, 1), 999)
    if actual.get("weightKg") is not None:
        actual["weightKg"] = round(float(actual["weightKg"]), 2)
    return actual


def build_upload(
    inputs: WorkoutInputs, client_uuid: str, routine: dict, schedule: dict | None, catalog: dict[int, dict],
    ended_at: datetime, added: tuple[dict, ...] = (),
) -> dict:
    """루틴과 입력으로 업로드 본문을 만든다. 같은 인자면 늘 같은 본문이다.

    routine의 id가 None이면 자유 운동(free_workout)이다. added는 운동 중에 더한 종목이고 루틴 종목 뒤에 붙는다.
    자유 운동과 더한 종목은 시작 구성에 없으므로 시작 종목·세트 대응을 보내지 않고, 직접 더한 세트라 건너뛰지 않는다.
    """
    free = routine["id"] is None
    started_at = ended_at - timedelta(minutes=inputs.duration_minutes)
    exercises = sorted(routine["exercises"], key=lambda e: e["sortOrder"])
    # 시작 구성에 없는 종목 수. 업로드 종목 목록의 뒤쪽이다
    own_from = 0 if free else len(exercises)
    exercises = exercises + list(added)
    total_sets = sum(len(e["targets"]) for e in exercises)
    span = int((ended_at - started_at).total_seconds())

    start_exercises, performed = [], []
    position = 0
    for order, exercise in enumerate(exercises, start=1):
        own = order > own_from
        catalog_entry = catalog.get(exercise["exerciseId"])
        head = {
            "sortOrder": order,
            "exerciseId": exercise["exerciseId"],
            # 내려간 종목은 사본에 없다. 이름은 1-100자이기만 하면 된다
            "exerciseName": catalog_entry["nameKo"] if catalog_entry else f"종목 {exercise['exerciseId']}",
            "recordType": exercise["recordType"],
            "weightType": exercise["weightType"],
            "restSeconds": exercise["restSeconds"],
            "skipped": False,
        }
        start_sets, done_sets = [], []
        for set_no, target in enumerate(exercise["targets"], start=1):
            values = _values(target)
            start_sets.append({"setNo": set_no, "target": values, "status": "PENDING"})
            roll = _cycled(inputs.set_rolls, position)
            done = {"setNo": set_no, "target": values} | ({} if own else {"originStartSetNo": set_no})
            if not own and roll < SKIP_RATE:
                done["status"] = "SKIPPED"
            else:
                spread = roll if own else (roll - SKIP_RATE) / (1 - SKIP_RATE)
                # 세트 완료 시각은 운동 시간 안에 세트 순서대로 고르게 둔다
                done |= {
                    "status": "COMPLETED",
                    "actual": _actual(exercise["recordType"], values, spread),
                    "completedAt": (started_at + timedelta(seconds=span * (position + 1) // (total_sets + 1))).isoformat(),
                }
            done_sets.append(done)
            position += 1
        if own:
            performed.append(head | {"sets": done_sets})
        else:
            start_exercises.append(head | {"sets": start_sets})
            performed.append(head | {"originStartOrder": order, "sets": done_sets})

    # 서버는 완료 세트가 하나도 없는 기록을 받지 않는다. 모두 건너뛰었으면 첫 세트를 완료로 바꾼다
    if not any(s["status"] == "COMPLETED" for e in performed for s in e["sets"]):
        first_exercise = performed[0]
        first = first_exercise["sets"][0]
        first |= {
            "status": "COMPLETED",
            "actual": _actual(first_exercise["recordType"], first["target"], 0.5),
            "completedAt": (started_at + timedelta(seconds=span // (total_sets + 1))).isoformat(),
        }

    body = {
        "clientUuid": client_uuid,
        "startedAt": started_at.isoformat(),
        "endedAt": ended_at.isoformat(),
        "startExercises": start_exercises,
        "performedExercises": performed,
    }
    if not free:
        body["routineId"] = routine["id"]
        body["routineName"] = routine["name"]
    if schedule is not None:
        body["scheduleId"] = schedule["id"]
        body["scheduledDate"] = schedule["scheduledDate"]
    return body


def _todays_open_schedule(schedules: list[dict], day: date, routines_by_id: dict[int, dict]) -> dict | None:
    """오늘 예정이고 아직 완료되지 않았으며 루틴이 남아 있는 일정 중 ID가 가장 작은 것."""
    candidates = [
        s for s in schedules
        if s.get("scheduledDate") == day.isoformat() and s.get("completedSessionId") is None
        and s.get("routineId") in routines_by_id
    ]
    return min(candidates, key=lambda s: s["id"]) if candidates else None


# --- 세션 ---


def _search_missing(ctx: SessionContext, inputs: WorkoutInputs, catalog: dict[int, dict], routine: dict) -> tuple[dict, ...]:
    """하고 싶은 종목을 검색했는데 없어서, 그 부위를 둘러보고 대신할 종목을 운동 중에 더한다. 더한 종목을 돌려준다.

    서비스에 회원이 종목을 직접 만드는 기능이 없으므로 카탈로그의 다른 종목으로 대신한다. 검색어가 카탈로그에
    생겨 결과가 나와도 실패로 보지 않는다(그때는 검색어 목록을 고친다).
    """
    api = ctx.api
    ctx.think(ctx.config.search_think)
    api.call("GET", "/exercises", name="GET /exercises", params={
        "keyword": inputs.missing_keyword, "page": 0, "size": BROWSE_PAGE_SIZE,
    })
    ctx.think(ctx.config.search_think)
    api.call("GET", "/exercises", name="GET /exercises", params={
        "bodyPart": MISSING_EXERCISES[inputs.missing_keyword], "page": 0, "size": BROWSE_PAGE_SIZE,
    })
    doing = {e["exerciseId"] for e in routine["exercises"]}
    chosen = replacement(catalog, inputs.missing_keyword, inputs.missing_seed, doing)
    if chosen is None:
        return ()
    ctx.think(ctx.config.select_think)
    order = len(routine["exercises"]) + 1
    return (_performed_entry(chosen, order, inputs, inputs.missing_seed),)



def run(ctx: SessionContext, inputs: WorkoutInputs) -> dict:
    """끝난 운동 하나를 올리고 결과를 돌려준다. 이 세션의 기록이 이미 있으면 SessionSkipped."""
    api = ctx.api
    day = inputs.planned_at.date()
    client_uuid = workout_uuid(api.email, day)

    # 앱을 열면 화면이 자동으로 부르는 조회. 사이에 생각 시간을 두지 않는다
    steps.open_home(api, day.isoformat())
    routines = api.call("GET", "/routines", name="GET /routines").data["items"]
    if not routines and inputs.custom_routine:
        routines = _setup(api, inputs)
    routines = sorted(routines, key=lambda r: r["id"])
    routines_by_id = {r["id"]: r for r in routines}

    week_end = day + timedelta(days=WINDOW_DAYS - 1)
    schedules = api.call(
        "GET", "/workout-schedules", name="GET /workout-schedules",
        params={"from": day.isoformat(), "to": week_end.isoformat()},
    ).data["items"]
    # 일정은 루틴을 예약하는 것이라 루틴이 없으면 만들 수 없다
    if not schedules and routines:
        schedules = _schedule_week(ctx, inputs, day, routines)

    if inputs.browse_body_part:
        # 사용자가 부위를 골라 종목을 둘러본다
        ctx.think(ctx.config.search_think)
        api.call(
            "GET", "/exercises", name="GET /exercises",
            params={"bodyPart": inputs.browse_body_part, "page": 0, "size": BROWSE_PAGE_SIZE},
        )

    recent = api.call(
        "GET", "/workout-sessions", name="GET /workout-sessions",
        params={"from": (day - timedelta(days=WINDOW_DAYS - 1)).isoformat(), "to": day.isoformat()},
    ).data["items"]
    if any(item.get("clientUuid") == client_uuid for item in recent):
        raise SessionSkipped("오늘 운동을 이미 기록함")

    catalog = _load_catalog(api)
    schedule = _todays_open_schedule(schedules, day, routines_by_id)
    if not routines:
        routine = free_workout(catalog, inputs)
    elif schedule:
        routine = routines_by_id[schedule["routineId"]]
    else:
        routine = routines[inputs.routine_pick % len(routines)]

    added = ()
    if inputs.missing_keyword:
        added = _search_missing(ctx, inputs, catalog, routine)

    # 세트를 기록하는 화면이 종목마다 이전 기록을 보여 준다
    exercise_ids = sorted({e["exerciseId"] for e in (*routine["exercises"], *added)})
    previous = api.call(
        "GET", "/workout-sessions/recent-sets", name="GET /workout-sessions/recent-sets",
        params={"exerciseIds": exercise_ids},
    ).data["items"]
    unknown = {item["exercise"]["exerciseId"] for item in previous} - set(exercise_ids)
    if unknown:
        raise ValidationFailed(f"이전 기록에 요청하지 않은 종목 {sorted(unknown)}")

    # 재실행해도 같은 본문이 나오도록 계획 시각을 쓰고 초 아래는 버린다. 서버 현재 시각을 넘지 않게 지금으로 당긴다
    ended_at = min(inputs.planned_at, ctx.now()).replace(microsecond=0)
    body = build_upload(inputs, client_uuid, routine, schedule, catalog, ended_at, added)

    # 200은 같은 clientUuid와 같은 내용을 다시 보낸 경우다(응답이 유실되어 재실행했을 때)
    saved = api.call(
        "POST", "/workout-sessions", name="POST /workout-sessions", json_body=body, expect=Expect(ok=(201, 200)),
    ).data
    if saved.get("clientUuid") != client_uuid:
        raise ValidationFailed(f"업로드 clientUuid {saved.get('clientUuid')}가 보낸 값 {client_uuid}와 다름")

    goal = api.call("GET", "/weekly-workout-goal/me", name="GET /weekly-workout-goal/me").data
    if not goal.get("configured"):
        if routines:
            raise ValidationFailed("주간 운동 목표가 정해지지 않음")
        # 자유 운동 회원은 루틴 준비를 거치지 않으므로 목표를 처음 볼 때 정한다
        ctx.think(ctx.config.select_think)
        api.call(
            "PUT", "/weekly-workout-goal/me", name=f"{steps.SETUP}PUT /weekly-workout-goal/me",
            json_body={"targetCount": inputs.weekly_target},
        )

    sets = [s for e in body["performedExercises"] for s in e["sets"]]
    return {
        "session_id": saved.get("id"),
        "routine_id": routine["id"],
        "schedule_id": schedule["id"] if schedule else None,
        "free": not routines,
        "added": len(added),
        "sets": len(sets),
        "completed_sets": sum(s["status"] == "COMPLETED" for s in sets),
    }
