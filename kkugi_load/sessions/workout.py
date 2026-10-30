"""운동 기록 세션. 앱을 열고, 루틴과 이번 주 일정을 받고, 끝난 운동 하나를 올리고, 주간 목표를 확인한다.

요청 순서:
1. 요약, 목표, 성장(홈 화면)
2. 루틴 목록(GET /routines)
   - 비어 있으면 준비: 종목 목록(프로세스에 없을 때만, 페이지 수만큼), 주간 목표 정하기, 루틴 만들기(1-2개)
3. 이번 주 일정(GET /workout-schedules, 오늘부터 6일 뒤까지)
   - 비어 있으면 주간 목표 횟수만큼 일정 예약(POST /workout-schedules)
4. 종목 둘러보기(GET /exercises?bodyPart=..). 입력에 부위가 있을 때만
5. 최근 7일 운동 기록(GET /workout-sessions). 이 세션의 기록이 이미 있으면 SessionSkipped
6. 운동 기록 업로드(POST /workout-sessions). 오늘 미완료 일정이 있으면 그 일정에 연결한다
7. 주간 목표(GET /weekly-workout-goal/me)

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

# 루틴 하나의 종목 수와 종목마다 세트 수
EXERCISES_PER_ROUTINE = 4
SETS_PER_EXERCISE = 3
# 루틴의 종목 칸. 앞 세 칸은 무게·횟수 종목이고 마지막 칸은 루틴 순번에 따라 횟수 또는 시간 종목이다
_SLOT_TYPES = ("WEIGHT_REPS", "WEIGHT_REPS", "WEIGHT_REPS")
_LAST_SLOT_TYPES = ("REPS", "DURATION")
REST_SECONDS = 90
# 세트 굴림값이 이보다 작으면 그 세트를 건너뛴다(가정)
SKIP_RATE = 0.15
# 실제 횟수 = 목표 + 이 중 하나. 굴림값으로 고른다
_REPS_DELTAS = (-2, -1, 0, 0, 1)
CATALOG_PAGE_SIZE = 100
BROWSE_PAGE_SIZE = 20
# 일정 목록과 기록 목록을 보는 기간(오늘 포함 7일)
WINDOW_DAYS = 7
# 종목 둘러보기에 쓰는 부위. 서버의 bodyPart 값이다
BODY_PARTS = ("LOWER_BODY", "BACK", "CHEST", "SHOULDER", "ARM", "CORE")

# 종목 ID → 종목. 프로세스 하나가 한 번 채우고 모든 계정이 함께 쓴다. 폰도 카탈로그 사본을 두고 쓰므로
# 세션마다 다시 받지 않는다. 서버는 제공 중인 종목만 돌려주므로, 루틴에 있는데 여기 없는 종목은 내려간 종목이다
_catalog: dict[int, dict] | None = None


@dataclass(frozen=True)
class WorkoutInputs:
    """운동 세션 입력. 난수는 모두 페르소나가 계정·날짜 해시로 미리 정한다.

    planned_at: 운동을 마친 시각(세션을 여는 시각). 업로드의 endedAt이 된다
    weekly_target: 주간 목표 횟수(1-7). 처음 목표를 정할 때와 일정 예약 수에 쓴다
    routine_count: 처음 만들 루틴 수(1-2). 루틴이 이미 있으면 쓰지 않는다
    routine_seeds: 루틴을 만들 때 종목 칸마다 쓰는 0 이상 1 미만의 값. routine_count × 4개를 권한다. 모자라면 돌려 쓴다
    routine_pick: 오늘 일정이 없을 때 고를 루틴의 순번 기준값. 루틴 수로 나눈 나머지를 쓴다
    browse_body_part: 종목 둘러보기 부위(LOWER_BODY, BACK, CHEST, SHOULDER, ARM, CORE). None이면 둘러보지 않는다
    duration_minutes: 운동 시간(분). startedAt = endedAt - 이 시간
    set_rolls: 세트마다 쓰는 0 이상 1 미만의 값. 건너뛸지와 실제값의 흔들림을 정한다. 모자라면 돌려 쓴다
    """

    planned_at: datetime
    weekly_target: int
    routine_count: int
    routine_seeds: tuple[float, ...]
    routine_pick: int
    browse_body_part: str | None
    duration_minutes: int
    set_rolls: tuple[float, ...]


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


def _target(exercise: dict, seed: float) -> dict:
    """종목의 기록 방식에 맞는 세트 목표. 방식에 없는 값은 null이어야 서버가 받는다."""
    record_type = exercise["recordType"]
    if record_type == "DURATION":
        return {"targetWeightKg": None, "targetReps": None, "targetDurationSeconds": 30 + 15 * int(seed * 5)}
    reps = 8 + int(seed * 5) if record_type == "WEIGHT_REPS" else 10 + 2 * int(seed * 6)
    if record_type != "WEIGHT_REPS":
        return {"targetWeightKg": None, "targetReps": reps, "targetDurationSeconds": None}
    # 무게의 뜻은 weightType마다 다르다. 일반은 원판 무게, 추가는 몸무게에 더하는 무게, 보조는 덜어 주는 무게다
    base, steps_, step = {"ADDITIONAL": (0.0, 5, 2.5), "ASSISTANCE": (10.0, 5, 5.0)}.get(
        exercise.get("weightType"), (20.0, 17, 2.5),
    )
    return {"targetWeightKg": base + step * int(seed * steps_), "targetReps": reps, "targetDurationSeconds": None}


def _routine_body(catalog: dict[int, dict], order: int, seeds: tuple[float, ...]) -> dict:
    """order번째 루틴의 생성 본문. 종목은 기록 방식별 후보(ID 순)에서 seeds로 고르고 루틴 안에서 겹치지 않게 한다."""
    by_id = sorted(catalog.values(), key=lambda e: e["id"])
    slot_types = _SLOT_TYPES + (_LAST_SLOT_TYPES[order % len(_LAST_SLOT_TYPES)],)
    chosen: list[dict] = []
    for slot, record_type in enumerate(slot_types):
        seed = _cycled(seeds, order * EXERCISES_PER_ROUTINE + slot)
        # 그 방식의 종목이 없으면 전체에서 고른다
        pool = [e for e in by_id if e["recordType"] == record_type] or by_id
        start = int(seed * len(pool))
        for step in range(len(pool)):
            candidate = pool[(start + step) % len(pool)]
            if all(candidate["id"] != c["id"] for c in chosen):
                chosen.append(candidate)
                break
    return {
        "name": f"꾸기 루틴 {order + 1}",
        "exercises": [
            {
                "exerciseId": exercise["id"],
                "restSeconds": REST_SECONDS,
                "targets": [_target(exercise, _cycled(seeds, order * EXERCISES_PER_ROUTINE + slot))] * SETS_PER_EXERCISE,
            }
            for slot, exercise in enumerate(chosen)
        ],
    }


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
            json_body=_routine_body(catalog, order, inputs.routine_seeds), expect=Expect(ok=(201,)),
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
    ended_at: datetime,
) -> dict:
    """루틴과 입력으로 업로드 본문을 만든다. 같은 인자면 늘 같은 본문이다."""
    started_at = ended_at - timedelta(minutes=inputs.duration_minutes)
    exercises = sorted(routine["exercises"], key=lambda e: e["sortOrder"])
    total_sets = sum(len(e["targets"]) for e in exercises)
    span = int((ended_at - started_at).total_seconds())

    start_exercises, performed = [], []
    position = 0
    for order, exercise in enumerate(exercises, start=1):
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
            done = {"setNo": set_no, "originStartSetNo": set_no, "target": values}
            if roll < SKIP_RATE:
                done["status"] = "SKIPPED"
            else:
                # 세트 완료 시각은 운동 시간 안에 세트 순서대로 고르게 둔다
                done |= {
                    "status": "COMPLETED",
                    "actual": _actual(exercise["recordType"], values, (roll - SKIP_RATE) / (1 - SKIP_RATE)),
                    "completedAt": (started_at + timedelta(seconds=span * (position + 1) // (total_sets + 1))).isoformat(),
                }
            done_sets.append(done)
            position += 1
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
        "routineId": routine["id"],
        "routineName": routine["name"],
        "startedAt": started_at.isoformat(),
        "endedAt": ended_at.isoformat(),
        "startExercises": start_exercises,
        "performedExercises": performed,
    }
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


def run(ctx: SessionContext, inputs: WorkoutInputs) -> dict:
    """끝난 운동 하나를 올리고 결과를 돌려준다. 이 세션의 기록이 이미 있으면 SessionSkipped."""
    api = ctx.api
    day = inputs.planned_at.date()
    client_uuid = workout_uuid(api.email, day)

    # 앱을 열면 화면이 자동으로 부르는 조회. 사이에 생각 시간을 두지 않는다
    steps.open_home(api, day.isoformat())
    routines = api.call("GET", "/routines", name="GET /routines").data["items"]
    if not routines:
        routines = _setup(api, inputs)
    routines = sorted(routines, key=lambda r: r["id"])
    routines_by_id = {r["id"]: r for r in routines}

    week_end = day + timedelta(days=WINDOW_DAYS - 1)
    schedules = api.call(
        "GET", "/workout-schedules", name="GET /workout-schedules",
        params={"from": day.isoformat(), "to": week_end.isoformat()},
    ).data["items"]
    if not schedules:
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

    schedule = _todays_open_schedule(schedules, day, routines_by_id)
    routine = routines_by_id[schedule["routineId"]] if schedule else routines[inputs.routine_pick % len(routines)]
    # 재실행해도 같은 본문이 나오도록 계획 시각을 쓰고 초 아래는 버린다. 서버 현재 시각을 넘지 않게 지금으로 당긴다
    ended_at = min(inputs.planned_at, ctx.now()).replace(microsecond=0)
    body = build_upload(inputs, client_uuid, routine, schedule, _load_catalog(api), ended_at)

    # 200은 같은 clientUuid와 같은 내용을 다시 보낸 경우다(응답이 유실되어 재실행했을 때)
    saved = api.call(
        "POST", "/workout-sessions", name="POST /workout-sessions", json_body=body, expect=Expect(ok=(201, 200)),
    ).data
    if saved.get("clientUuid") != client_uuid:
        raise ValidationFailed(f"업로드 clientUuid {saved.get('clientUuid')}가 보낸 값 {client_uuid}와 다름")

    goal = api.call("GET", "/weekly-workout-goal/me", name="GET /weekly-workout-goal/me").data
    if not goal.get("configured"):
        raise ValidationFailed("주간 운동 목표가 정해지지 않음")

    sets = [s for e in body["performedExercises"] for s in e["sets"]]
    return {
        "session_id": saved.get("id"),
        "routine_id": routine["id"],
        "schedule_id": schedule["id"] if schedule else None,
        "sets": len(sets),
        "completed_sets": sum(s["status"] == "COMPLETED" for s in sets),
    }
