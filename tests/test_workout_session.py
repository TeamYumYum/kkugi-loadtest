"""운동 기록 세션. 준비·일정 예약·건너뛰기 분기, 요청 순서, 업로드 본문이 서버 규칙을 지키는지."""

import base64
import json
import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.sessions import workout
from kkugi_load.sessions.base import SessionContext, SessionSkipped, ValidationFailed
from kkugi_load.sessions.workout import WorkoutInputs

CONFIG = config_module.load({})
PLANNED_AT = datetime(2026, 10, 28, 20, 30, tzinfo=CONFIG.tz)
NOW = PLANNED_AT + timedelta(minutes=3, seconds=7, microseconds=123456)
DAY = "2026-10-28"
HOME = ["/daily-summaries/2026-10-28", "/daily-goals/2026-10-28", "/growth/me"]

CATALOG = [
    {"id": 1, "nameKo": "바벨 벤치프레스", "recordType": "WEIGHT_REPS", "weightType": "EXTERNAL", "bodyPart": "CHEST"},
    {"id": 2, "nameKo": "바벨 스쿼트", "recordType": "WEIGHT_REPS", "weightType": "EXTERNAL", "bodyPart": "LOWER_BODY"},
    {"id": 3, "nameKo": "턱걸이", "recordType": "WEIGHT_REPS", "weightType": "ADDITIONAL", "bodyPart": "BACK"},
    {"id": 4, "nameKo": "어시스트 딥스", "recordType": "WEIGHT_REPS", "weightType": "ASSISTANCE", "bodyPart": "CHEST"},
    {"id": 5, "nameKo": "푸시업", "recordType": "REPS", "weightType": "NONE", "bodyPart": "CHEST"},
    {"id": 6, "nameKo": "크런치", "recordType": "REPS", "weightType": "NONE", "bodyPart": "CORE"},
    {"id": 7, "nameKo": "플랭크", "recordType": "DURATION", "weightType": "NONE", "bodyPart": "CORE"},
]
CATALOG_BY_ID = {e["id"]: e for e in CATALOG}


def _inputs(**overrides) -> WorkoutInputs:
    values = dict(
        planned_at=PLANNED_AT, weekly_target=3, routine_count=2,
        routine_seeds=(0.1, 0.5, 0.9, 0.3, 0.7, 0.2, 0.6, 0.95), routine_pick=1, browse_body_part=None,
        duration_minutes=55, set_rolls=(0.05, 0.4, 0.8, 0.99, 0.2, 0.6, 0.1, 0.5, 0.3),
    )
    return WorkoutInputs(**(values | overrides))


def _jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def success(self):
        pass

    def failure(self, message):
        raise AssertionError(f"요청 실패 {message}")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeServer:
    """루틴·일정·운동 기록·목표를 메모리에 두고 응답한다. 요청은 (메서드, 경로, 통계 이름)으로 기록한다."""

    def __init__(self, routines=None, schedules=None, sessions=None, catalog=CATALOG, goal=3):
        self.routines = list(routines or [])
        self.schedules = list(schedules or [])
        self.sessions = list(sessions or [])
        self.catalog = catalog
        self.goal = goal
        self.sent = []
        self.bodies = {}
        self.params = {}
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def request(self, method, url, name=None, json=None, params=None, **_kwargs):
        path = url.removeprefix(CONFIG.api_prefix)
        self.sent.append((method, path, name))
        self.bodies.setdefault((method, path), []).append(json)
        self.params.setdefault((method, path), []).append(params)
        status, data = self._handle(method, path, json, params or {})
        return _FakeResponse(status, {"success": True, "data": data})

    def _handle(self, method, path, body, params):
        if (method, path) == ("GET", "/routines"):
            return 200, {"items": self.routines}
        if (method, path) == ("POST", "/routines"):
            routine = {
                "id": self._id(), "name": body["name"],
                "exercises": [
                    {
                        "routineExerciseId": self._id(), "exerciseId": e["exerciseId"], "sortOrder": i,
                        "restSeconds": e["restSeconds"] if e["restSeconds"] is not None else 90,
                        "recordType": CATALOG_BY_ID[e["exerciseId"]]["recordType"],
                        "weightType": CATALOG_BY_ID[e["exerciseId"]]["weightType"],
                        "targets": [t | {"setNo": n} for n, t in enumerate(e["targets"], start=1)],
                    }
                    for i, e in enumerate(body["exercises"], start=1)
                ],
            }
            self.routines.append(routine)
            return 201, routine
        if (method, path) == ("PUT", "/weekly-workout-goal/me"):
            self.goal = body["targetCount"]
            return 200, {"configured": True}
        if (method, path) == ("GET", "/weekly-workout-goal/me"):
            return 200, {"configured": self.goal is not None, "thisWeek": None, "nextWeek": None, "progress": None}
        if (method, path) == ("GET", "/workout-schedules"):
            return 200, {"items": [s for s in self.schedules if params["from"] <= s["scheduledDate"] <= params["to"]]}
        if (method, path) == ("POST", "/workout-schedules"):
            schedule = {"id": self._id(), "routineId": body["routineId"], "scheduledDate": body["scheduledDate"],
                        "completedSessionId": None}
            self.schedules.append(schedule)
            return 201, schedule
        if (method, path) == ("GET", "/exercises"):
            items = [e for e in self.catalog if params.get("bodyPart") in (None, e["bodyPart"])]
            page, size = params.get("page", 0), params.get("size", 20)
            return 200, {"items": items[page * size:(page + 1) * size], "page": page, "size": size,
                         "totalElements": len(items)}
        if (method, path) == ("GET", "/workout-sessions"):
            return 200, {"items": self.sessions}
        if (method, path) == ("POST", "/workout-sessions"):
            for saved in self.sessions:
                if saved["clientUuid"] == body["clientUuid"]:
                    assert saved["body"] == body, "같은 clientUuid에 다른 내용"
                    return 200, {"id": saved["id"], "clientUuid": body["clientUuid"]}
            session_id = self._id()
            self.sessions.append({"id": session_id, "clientUuid": body["clientUuid"], "body": body})
            for schedule in self.schedules:
                if schedule["id"] == body.get("scheduleId"):
                    assert schedule["completedSessionId"] is None, "이미 완료된 일정"
                    schedule["completedSessionId"] = session_id
            return 201, {"id": session_id, "clientUuid": body["clientUuid"]}
        return 200, {}


def _routine(routine_id=10, name="상체"):
    """서버가 돌려주는 루틴 모양. 세 기록 방식을 모두 담는다."""
    def ex(sort, exercise_id, targets):
        entry = CATALOG_BY_ID[exercise_id]
        return {"routineExerciseId": 1000 + sort, "exerciseId": exercise_id, "sortOrder": sort, "restSeconds": 60,
                "recordType": entry["recordType"], "weightType": entry["weightType"],
                "targets": [t | {"setNo": n} for n, t in enumerate(targets, start=1)]}

    return {"id": routine_id, "name": name, "exercises": [
        ex(1, 1, [{"targetWeightKg": 40.0, "targetReps": 10, "targetDurationSeconds": None}] * 3),
        ex(2, 5, [{"targetWeightKg": None, "targetReps": 15, "targetDurationSeconds": None}] * 2),
        ex(3, 7, [{"targetWeightKg": None, "targetReps": None, "targetDurationSeconds": 60}] * 2),
    ]}


@pytest.fixture(autouse=True)
def _fresh_catalog():
    workout.clear_catalog()
    yield
    workout.clear_catalog()


def _api(tmp_path, client):
    store = StateStore(tmp_path / "s.db")
    account = build_account(CONFIG, 42)
    state = store.load(42, account.persona)
    state.member_id, state.access_token, state.refresh_token, state.onboarded = 1, _jwt(4102444800), "r", True
    store.save(state)
    return KkugiApi(client, CONFIG, account, state, store)


def _api_in(tmp_path, name, client):
    path = tmp_path / name
    path.mkdir()
    return _api(path, client)


def _ctx(api, thinks=None) -> SessionContext:
    return SessionContext(
        api=api, config=CONFIG, now=lambda: NOW,
        sleep=(thinks.append if thinks is not None else lambda _s: None), rng=random.Random(0),
    )


def _names(server):
    return [name for _, _, name in server.sent]


# --- 서버 규칙 검사 ---


def _check_values(record_type: str, values: dict, where: str) -> None:
    weight, reps, duration = values.get("weightKg"), values.get("reps"), values.get("durationSeconds")
    if record_type == "WEIGHT_REPS":
        assert weight is not None and 0 <= weight <= 1000, where
        assert Decimal(str(weight)).as_tuple().exponent >= -2, f"{where} 소수 둘째 자리까지"
        assert reps is not None and 1 <= reps <= 999 and duration is None, where
    elif record_type == "REPS":
        assert weight is None and duration is None and reps is not None and 1 <= reps <= 999, where
    else:
        assert weight is None and reps is None and duration is not None and 1 <= duration <= 86400, where


def _check_routine_body(body: dict, catalog: dict) -> None:
    assert 1 <= len(body["name"]) <= 50 and body["name"].strip()
    assert body["exercises"]
    for e in body["exercises"]:
        assert e["exerciseId"] in catalog, "제공 중인 종목만"
        assert e["restSeconds"] is None or 0 <= e["restSeconds"] <= 3600
        assert e["targets"]
        record_type = catalog[e["exerciseId"]]["recordType"]
        for t in e["targets"]:
            _check_values(record_type, {"weightKg": t["targetWeightKg"], "reps": t["targetReps"],
                                        "durationSeconds": t["targetDurationSeconds"]}, f"루틴 목표 {e}")


def _parse_time(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    assert at.tzinfo is not None, f"{text} 오프셋 필요"
    return at


def _check_upload(body: dict, now: datetime, known_exercises: set[int]) -> None:
    """WorkoutSessionUploadRequest가 서버 검증을 통과하는지. 브리프에 적힌 백엔드 규칙을 그대로 옮겼다."""
    uuid.UUID(body["clientUuid"])
    assert ("routineId" in body) == ("routineName" in body)
    assert body.get("routineName") is None or 1 <= len(body["routineName"]) <= 100
    assert ("scheduleId" in body) == ("scheduledDate" in body)
    if "scheduleId" in body:
        assert body.get("routineId") is not None
    started, ended = _parse_time(body["startedAt"]), _parse_time(body["endedAt"])
    assert started <= ended <= now

    starts = {}
    for e in body["startExercises"]:
        assert e["sortOrder"] > 0 and e["sortOrder"] not in starts
        starts[e["sortOrder"]] = e
        assert "originStartOrder" not in e
        assert e["skipped"] is False
        assert e["sets"]
        set_nos = [s["setNo"] for s in e["sets"]]
        assert all(n > 0 for n in set_nos) and len(set(set_nos)) == len(set_nos)
        for s in e["sets"]:
            assert s["status"] == "PENDING"
            assert "actual" not in s and "completedAt" not in s and "originStartSetNo" not in s
            _check_values(e["recordType"], s["target"], f"시작 목표 {e['sortOrder']}")

    used_origins = set()
    orders = set()
    completed = 0
    for e in body["performedExercises"]:
        assert e["sortOrder"] > 0 and e["sortOrder"] not in orders
        orders.add(e["sortOrder"])
        assert e["exerciseId"] in known_exercises
        assert 1 <= len(e["exerciseName"]) <= 100
        assert 0 <= e["restSeconds"] <= 3600
        assert (e["recordType"] == "WEIGHT_REPS") == (e["weightType"] != "NONE")
        origin = e.get("originStartOrder")
        if origin is not None:
            assert origin not in used_origins, "시작 종목은 한 번만 대응"
            used_origins.add(origin)
            start = starts[origin]
            for key in ("exerciseId", "recordType", "weightType"):
                assert start[key] == e[key]
        if not e["skipped"]:
            assert e["sets"], "건너뛰지 않은 종목은 세트가 있어야 함"
        set_nos = [s["setNo"] for s in e["sets"]]
        assert all(n > 0 for n in set_nos) and len(set(set_nos)) == len(set_nos)
        for s in e["sets"]:
            assert s["status"] in ("PENDING", "COMPLETED", "SKIPPED")
            if e["skipped"]:
                assert s["status"] != "PENDING"
            if s.get("originStartSetNo") is not None:
                assert origin is not None
                assert s["originStartSetNo"] in {x["setNo"] for x in starts[origin]["sets"]}
            _check_values(e["recordType"], s["target"], f"목표 {e['sortOrder']}")
            if s["status"] == "COMPLETED":
                completed += 1
                _check_values(e["recordType"], s["actual"], f"실제 {e['sortOrder']}")
                assert started <= _parse_time(s["completedAt"]) <= ended
            else:
                assert s.get("actual") is None and s.get("completedAt") is None
    assert completed >= 1, "완료 세트가 하나 이상"


# --- 분기와 요청 순서 ---


def test_setup_when_no_routines(tmp_path):
    server = _FakeServer(goal=None)

    outcome = workout.run(_ctx(_api(tmp_path, server)), _inputs())

    assert _names(server) == [
        "GET /daily-summaries/{date}", "GET /daily-goals/{goalDate}", "GET /growth/me",
        "GET /routines",
        "[setup] GET /exercises",
        "[setup] PUT /weekly-workout-goal/me",
        "[setup] POST /routines", "[setup] POST /routines",
        "GET /workout-schedules",
        "POST /workout-schedules", "POST /workout-schedules", "POST /workout-schedules",
        "GET /workout-sessions",
        "POST /workout-sessions",
        "GET /weekly-workout-goal/me",
    ]
    assert server.bodies[("PUT", "/weekly-workout-goal/me")] == [{"targetCount": 3}]
    for body in server.bodies[("POST", "/routines")]:
        _check_routine_body(body, CATALOG_BY_ID)
        record_types = [CATALOG_BY_ID[e["exerciseId"]]["recordType"] for e in body["exercises"]]
        assert len(body["exercises"]) == 4 and record_types.count("WEIGHT_REPS") == 3
        assert all(len(e["targets"]) == 3 for e in body["exercises"])
        assert len({e["exerciseId"] for e in body["exercises"]}) == 4
    last_types = {CATALOG_BY_ID[b["exercises"][-1]["exerciseId"]]["recordType"] for b in server.bodies[("POST", "/routines")]}
    assert last_types == {"REPS", "DURATION"}
    # 오늘 일정이 생겼으므로 그 일정에 연결한다
    assert outcome["schedule_id"] is not None
    assert server.goal == 3


def test_catalog_is_fetched_once_per_process(tmp_path):
    server, server2 = _FakeServer(goal=None), _FakeServer(goal=None)
    workout.run(_ctx(_api_in(tmp_path, "a", server)), _inputs())
    workout.run(_ctx(_api_in(tmp_path, "b", server2)), _inputs())

    assert "[setup] GET /exercises" in _names(server)
    assert "[setup] GET /exercises" not in _names(server2)


def test_catalog_paging(tmp_path):
    many = [dict(CATALOG[i % len(CATALOG)], id=i + 1) for i in range(230)]
    server = _FakeServer(catalog=many)

    workout._load_catalog(_api(tmp_path, server))

    assert [p["page"] for p in server.params[("GET", "/exercises")]] == [0, 1, 2]
    assert all(p["size"] == 100 for p in server.params[("GET", "/exercises")])
    assert len(workout._catalog) == 230


def test_no_setup_when_routines_exist(tmp_path):
    schedules = [{"id": 50, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}]
    server = _FakeServer(routines=[_routine()], schedules=schedules)

    outcome = workout.run(_ctx(_api(tmp_path, server)), _inputs())

    names = _names(server)
    assert not any(n.startswith("[setup] ") and n != "[setup] GET /exercises" for n in names)
    assert "POST /workout-schedules" not in names
    # 종목 이름을 채우려고 카탈로그는 받는다(프로세스에 사본이 없을 때)
    assert names == HOME_NAMES + ["GET /routines", "GET /workout-schedules", "GET /workout-sessions",
                                  "[setup] GET /exercises", "POST /workout-sessions", "GET /weekly-workout-goal/me"]
    # 오늘 일정이 없으니 연결하지 않는다
    assert outcome["schedule_id"] is None
    body = server.bodies[("POST", "/workout-sessions")][0]
    assert "scheduleId" not in body and "scheduledDate" not in body


HOME_NAMES = ["GET /daily-summaries/{date}", "GET /daily-goals/{goalDate}", "GET /growth/me"]


def test_schedule_creation_spreads_over_window(tmp_path):
    routines = [_routine(10, "상체"), _routine(11, "하체")]
    server = _FakeServer(routines=routines)
    thinks = []

    workout.run(_ctx(_api(tmp_path, server), thinks), _inputs(weekly_target=4))

    created = server.bodies[("POST", "/workout-schedules")]
    assert [b["scheduledDate"] for b in created] == ["2026-10-28", "2026-10-29", "2026-10-31", "2026-11-02"]
    assert [b["routineId"] for b in created] == [10, 11, 10, 11]
    assert server.params[("GET", "/workout-schedules")] == [{"from": "2026-10-28", "to": "2026-11-03"}]
    # 일정을 고르는 생각 시간 한 번
    assert len(thinks) == 1


@pytest.mark.parametrize("target", range(1, 8))
def test_schedule_offsets_stay_in_window(target):
    offsets = workout.schedule_offsets(target)
    assert len(offsets) == target == len(set(offsets))
    assert offsets[0] == 0 and all(0 <= o < 7 for o in offsets)


def test_skip_when_already_uploaded(tmp_path):
    api = _api(tmp_path, None)
    mine = workout.workout_uuid(api.email, PLANNED_AT.date())
    schedules = [{"id": 50, "routineId": 10, "scheduledDate": DAY, "completedSessionId": 77}]
    server = _FakeServer(routines=[_routine()], schedules=schedules, sessions=[{"id": 77, "clientUuid": mine}])
    api.client = server

    with pytest.raises(SessionSkipped):
        workout.run(_ctx(api), _inputs())

    assert ("POST", "/workout-sessions") not in server.bodies
    assert _names(server)[-1] == "GET /workout-sessions"
    assert server.params[("GET", "/workout-sessions")] == [{"from": "2026-10-22", "to": "2026-10-28"}]


def test_browse_body_part(tmp_path):
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 50, "routineId": 10, "scheduledDate": "2026-10-29", "completedSessionId": None}])
    thinks = []

    workout.run(_ctx(_api(tmp_path, server), thinks), _inputs(browse_body_part="CORE"))

    names = _names(server)
    browse = names.index("GET /exercises")
    assert names[browse - 1] == "GET /workout-schedules" and names[browse + 1] == "GET /workout-sessions"
    assert server.params[("GET", "/exercises")][0] == {"bodyPart": "CORE", "page": 0, "size": 20}
    assert len(thinks) == 1


# --- 업로드 본문 ---


def test_upload_links_todays_schedule_and_satisfies_server_rules(tmp_path):
    routines = [_routine(10, "상체"), _routine(11, "하체")]
    schedules = [
        {"id": 49, "routineId": 10, "scheduledDate": DAY, "completedSessionId": 5},
        {"id": 50, "routineId": 11, "scheduledDate": DAY, "completedSessionId": None},
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None},
    ]
    server = _FakeServer(routines=routines, schedules=schedules)
    api = _api(tmp_path, server)

    outcome = workout.run(_ctx(api), _inputs())

    body = server.bodies[("POST", "/workout-sessions")][0]
    _check_upload(body, NOW, set(CATALOG_BY_ID))
    assert body["scheduleId"] == 50 and body["scheduledDate"] == DAY
    assert body["routineId"] == 11 and body["routineName"] == "하체"
    assert body["clientUuid"] == workout.workout_uuid(api.email, PLANNED_AT.date())
    assert body["endedAt"] == PLANNED_AT.isoformat()
    assert _parse_time(body["endedAt"]) - _parse_time(body["startedAt"]) == timedelta(minutes=55)
    assert [e["exerciseName"] for e in body["performedExercises"]] == ["바벨 벤치프레스", "푸시업", "플랭크"]
    statuses = [s["status"] for e in body["performedExercises"] for s in e["sets"]]
    # 굴림값 0.05, 0.1이 SKIP_RATE보다 작다
    assert statuses == ["SKIPPED", "COMPLETED", "COMPLETED", "COMPLETED", "COMPLETED", "COMPLETED", "SKIPPED"]
    assert outcome == {"session_id": server.sessions[0]["id"], "routine_id": 11, "schedule_id": 50, "sets": 7,
                       "completed_sets": 5}
    times = [s["completedAt"] for e in body["performedExercises"] for s in e["sets"] if s["status"] == "COMPLETED"]
    assert times == sorted(times)


def test_upload_without_schedule_uses_routine_pick(tmp_path):
    routines = [_routine(10, "상체"), _routine(11, "하체"), _routine(12, "전신")]
    server = _FakeServer(routines=routines, schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}])

    workout.run(_ctx(_api(tmp_path, server)), _inputs(routine_pick=5))

    body = server.bodies[("POST", "/workout-sessions")][0]
    _check_upload(body, NOW, set(CATALOG_BY_ID))
    assert body["routineId"] == 12


def test_ended_at_is_pulled_back_to_now(tmp_path):
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}])
    api = _api(tmp_path, server)
    early = PLANNED_AT - timedelta(minutes=10, microseconds=500)
    ctx = SessionContext(api=api, config=CONFIG, now=lambda: early, sleep=lambda _s: None, rng=random.Random(0))

    workout.run(ctx, _inputs())

    body = server.bodies[("POST", "/workout-sessions")][0]
    assert body["endedAt"] == early.replace(microsecond=0).isoformat()
    _check_upload(body, early, set(CATALOG_BY_ID))


def test_at_least_one_completed_set_when_all_rolls_skip(tmp_path):
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}])

    outcome = workout.run(_ctx(_api(tmp_path, server)), _inputs(set_rolls=(0.0,)))

    body = server.bodies[("POST", "/workout-sessions")][0]
    _check_upload(body, NOW, set(CATALOG_BY_ID))
    assert outcome["completed_sets"] == 1
    assert body["performedExercises"][0]["sets"][0]["status"] == "COMPLETED"


def test_setup_routines_produce_valid_upload(tmp_path):
    """서버가 만든 루틴(준비 경로)에서 나온 본문도 규칙을 지킨다. 무게 종류 셋을 모두 거치게 한다."""
    weight_types = set()
    for i, seeds in enumerate([(0.0,), (0.3,), (0.5,), (0.99,), (0.1, 0.4, 0.7, 0.2, 0.8, 0.6, 0.35, 0.05)]):
        workout.clear_catalog()
        server = _FakeServer(goal=None)
        workout.run(_ctx(_api_in(tmp_path, f"run{i}", server)), _inputs(routine_seeds=seeds))
        weight_types |= {e["weightType"] for r in server.routines for e in r["exercises"]}
        for body in server.bodies[("POST", "/routines")]:
            _check_routine_body(body, CATALOG_BY_ID)
        _check_upload(server.bodies[("POST", "/workout-sessions")][0], NOW, set(CATALOG_BY_ID))
    assert weight_types == {"EXTERNAL", "ADDITIONAL", "ASSISTANCE", "NONE"}


def test_retired_exercise_gets_fallback_name(tmp_path):
    routine = _routine()
    routine["exercises"][1]["exerciseId"] = 999
    server = _FakeServer(routines=[routine], schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}])

    workout.run(_ctx(_api(tmp_path, server)), _inputs())

    body = server.bodies[("POST", "/workout-sessions")][0]
    assert body["performedExercises"][1]["exerciseName"] == "종목 999"
    _check_upload(body, NOW, set(CATALOG_BY_ID) | {999})


def test_same_inputs_give_identical_body_and_replay_is_200(tmp_path):
    """두 번 실행해도 본문이 같다. 응답이 유실된 재실행이면 서버는 같은 내용으로 보고 200을 준다."""
    def run_once(path):
        workout.clear_catalog()
        server = _FakeServer(routines=[_routine()], schedules=[
            {"id": 50, "routineId": 10, "scheduledDate": DAY, "completedSessionId": None}])
        workout.run(_ctx(_api_in(tmp_path, path, server)), _inputs())
        return server.bodies[("POST", "/workout-sessions")][0]

    first, second = run_once("a"), run_once("b")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    # 같은 서버에 같은 본문을 다시 보내면 200 재전송으로 처리된다
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 50, "routineId": 10, "scheduledDate": DAY, "completedSessionId": None}])
    api = _api(tmp_path, server)
    workout.run(_ctx(api), _inputs())
    replay = server.request("POST", CONFIG.api_prefix + "/workout-sessions", json=first)
    assert replay.status_code == 200


# --- 검증 실패 ---


def test_fails_when_uuid_differs(tmp_path):
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}])
    original = server._handle

    def wrong_uuid(method, path, body, params):
        status, data = original(method, path, body, params)
        if (method, path) == ("POST", "/workout-sessions"):
            data = dict(data, clientUuid=str(uuid.uuid4()))
        return status, data

    server._handle = wrong_uuid
    with pytest.raises(ValidationFailed, match="clientUuid"):
        workout.run(_ctx(_api(tmp_path, server)), _inputs())


def test_fails_when_goal_not_configured(tmp_path):
    server = _FakeServer(routines=[_routine()], schedules=[
        {"id": 51, "routineId": 10, "scheduledDate": "2026-10-30", "completedSessionId": None}], goal=None)

    with pytest.raises(ValidationFailed, match="주간 운동 목표"):
        workout.run(_ctx(_api(tmp_path, server)), _inputs())
