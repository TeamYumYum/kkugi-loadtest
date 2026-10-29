"""바깥에서 탈퇴된 인공 계정을 새 이메일로 다시 만드는 처리."""

import base64
import json
import random
from datetime import date, datetime

import pytest

from kkugi_load import config as config_module
from kkugi_load import runner
from kkugi_load.accounts.identity import email_of, meal_uuid
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import AccountWithdrawn, KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.personas.base import PlannedSession
from kkugi_load.sessions import meal_log
from kkugi_load.sessions.base import SessionContext, SessionSkipped
from kkugi_load.sessions.meal_log import MealInputs

CONFIG = config_module.load({})
DAY = date(2026, 10, 28)
WITHDRAWN = (401, {"error": {"code": "MEMBER_WITHDRAWN", "message": "탈퇴한 회원"}})


def _jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


VALID = _jwt(4102444800)
EXPIRED = _jwt(1)


class _FakeResponse:
    def __init__(self, status: int, body: dict, outcomes: list):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self._outcomes = outcomes

    def json(self):
        return self._body

    def success(self):
        self._outcomes.append("success")

    def failure(self, message):
        self._outcomes.append(f"failure {message}")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeClient:
    """요청을 기록하고, routes에서 (메서드, 경로)에 맞는 응답을 돌려준다. 없으면 200 빈 data."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.sent = []
        # 요청마다 Locust에 남긴 결과(success 또는 failure ...)
        self.outcomes = []

    def request(self, method, url, *, json=None, params=None, **_kwargs):
        path = url.removeprefix(CONFIG.api_prefix)
        self.sent.append((method, path, json))
        if path == "/auth/login/dev" and (method, path) not in self.routes:
            return _FakeResponse(200, {"data": {"memberId": 2, "accessToken": VALID, "refreshToken": "r2",
                                                "onboarded": False}}, self.outcomes)
        route = self.routes.get((method, path), (200, {"data": {}}))
        status, body = route(json, params) if callable(route) else route
        return _FakeResponse(status, body, self.outcomes)


def _api(tmp_path, client, *, index=42, generation=0, access=VALID, refresh="r1", onboarded=True):
    store = StateStore(tmp_path / "s.db")
    account = build_account(CONFIG, index)
    state = store.load(index, account.persona)
    state.generation, state.member_id = generation, 1
    state.access_token, state.refresh_token, state.onboarded = access, refresh, onboarded
    store.save(state)
    return KkugiApi(client, CONFIG, account, state, store), store


def _assert_moved_to_next_generation(api, store, generation):
    state = api.state
    assert (state.generation, state.member_id, state.access_token, state.refresh_token, state.onboarded) == (
        generation, None, None, None, False,
    )
    assert store.load(state.account_index, state.persona) == state


def test_protected_api_withdrawn_moves_account_to_next_email(tmp_path):
    client = _FakeClient({("GET", "/growth/me"): WITHDRAWN})
    api, store = _api(tmp_path, client)
    assert api.email == "lt-00042@synthetic.kkugi.test"

    with pytest.raises(AccountWithdrawn) as e:
        api.call("GET", "/growth/me", name="GET /growth/me")

    assert (e.value.old_email, e.value.new_email) == (
        "lt-00042@synthetic.kkugi.test", "lt-1000042@synthetic.kkugi.test",
    )
    _assert_moved_to_next_generation(api, store, 1)
    # 서버가 정상 동작한 것이므로 요청을 실패로 세지 않고, 재발급도 시도하지 않는다
    assert client.outcomes == ["success"]
    assert [path for _, path, _ in client.sent] == ["/growth/me"]

    # 다음 요청은 새 이메일로 로그인한다
    client.routes.clear()
    api.call("GET", "/growth/me", name="GET /growth/me")
    assert client.sent[1] == ("POST", "/auth/login/dev", {"email": "lt-1000042@synthetic.kkugi.test"})
    assert api.state.member_id == 2 and api.state.access_token == VALID


def test_refresh_withdrawn_moves_account_to_next_email(tmp_path):
    client = _FakeClient({("POST", "/auth/refresh"): WITHDRAWN})
    api, store = _api(tmp_path, client, generation=1, access=EXPIRED)

    with pytest.raises(AccountWithdrawn):
        api.call("GET", "/growth/me", name="GET /growth/me")

    _assert_moved_to_next_generation(api, store, 2)
    assert [path for _, path, _ in client.sent] == ["/auth/refresh"]
    assert client.outcomes == ["success"]
    assert api.email == "lt-2000042@synthetic.kkugi.test"


def test_other_401_still_renews_and_fails_normally(tmp_path):
    """UNAUTHORIZED는 재발급 후 다시 보내고, 재발급의 AUTH_REFRESH_INVALID는 다시 로그인한다."""
    calls = []

    def growth(_json, _params):
        calls.append(1)
        return (401, {"error": {"code": "UNAUTHORIZED"}}) if len(calls) == 1 else (200, {"data": {}})

    client = _FakeClient({
        ("GET", "/growth/me"): growth,
        ("POST", "/auth/refresh"): (401, {"error": {"code": "AUTH_REFRESH_INVALID"}}),
    })
    api, _ = _api(tmp_path, client)
    api.call("GET", "/growth/me", name="GET /growth/me")
    assert [path for _, path, _ in client.sent] == ["/growth/me", "/auth/refresh", "/auth/login/dev", "/growth/me"]
    assert api.state.generation == 0


class _Events:
    class _Request:
        def __init__(self):
            self.fired = []

        def fire(self, **kwargs):
            self.fired.append(kwargs)

    def __init__(self):
        self.request = self._Request()


def _planned(index: int) -> PlannedSession:
    return PlannedSession(index, "dieter_lunch", datetime(2026, 10, 28, 12, 0, tzinfo=CONFIG.tz), "fake", None)


def _growth_session(ctx, _inputs):
    ctx.api.call("GET", "/growth/me", name="GET /growth/me")
    return {}


@pytest.mark.parametrize("stage", ["setup", "session"])
def test_runner_reports_withdrawal_as_account_withdrawn(tmp_path, monkeypatch, stage):
    monkeypatch.setitem(runner.SESSIONS, "fake", _growth_session)
    if stage == "setup":
        client = _FakeClient({("POST", "/members/me/onboarding"): WITHDRAWN})
        api, _ = _api(tmp_path, client, onboarded=False)
    else:
        client = _FakeClient({("GET", "/growth/me"): WITHDRAWN})
        api, _ = _api(tmp_path, client)
    events = _Events()

    outcome = runner.run_planned(events, api, CONFIG, _planned(42), random.Random(0))

    assert outcome == runner.WITHDRAWN
    fired = events.request.fired
    assert [(f["request_type"], f["name"], f["exception"]) for f in fired] == [("ACCOUNT", "withdrawn", None)]
    assert api.state.generation == 1


def _meal() -> MealInputs:
    return MealInputs(
        meal_type="LUNCH", planned_at=datetime(2026, 10, 28, 12, 0, tzinfo=CONFIG.tz), keywords=("밥",),
        fallback_keywords=(), grams=(100,), picks=(0,),
    )


def _ctx(api) -> SessionContext:
    return SessionContext(
        api=api, config=CONFIG, now=lambda: datetime(2026, 10, 28, 13, 0, tzinfo=CONFIG.tz),
        sleep=lambda _s: None, rng=random.Random(0),
    )


def test_meal_uuid_comes_from_email_at_session_time(tmp_path):
    """하루 중간에 탈퇴되어 generation이 바뀌면, 그 뒤 세션은 새 이메일의 UUID로 저장하고 검사한다."""
    kcal = {"value": 0.0}

    def save(body, _params):
        kcal["value"] = 300.0
        return 201, {"data": {
            "id": 9, "clientUuid": body["clientUuid"], "mealType": body["mealType"], "eatenAt": body["eatenAt"],
            "items": [{"foodId": 5, "grams": 100}], "totalNutrition": {"kcal": 300.0},
        }}

    client = _FakeClient({
        ("GET", f"/daily-summaries/{DAY}"): lambda _j, _p: (200, {"data": {"intakeKcal": kcal["value"]}}),
        ("GET", "/meals"): (200, {"data": {"meals": []}}),
        ("GET", "/foods"): (200, {"data": {"items": [{"id": 5, "name": "밥"}]}}),
        ("POST", "/meals"): save,
    })
    api, _ = _api(tmp_path, client, generation=1)

    meal_log.run(_ctx(api), _meal())

    body = next(b for m, p, b in client.sent if (m, p) == ("POST", "/meals"))
    assert body["clientUuid"] == meal_uuid(email_of(CONFIG, 42, 1), DAY, "LUNCH")
    assert body["clientUuid"] != meal_uuid(email_of(CONFIG, 42, 0), DAY, "LUNCH")


def test_meals_saved_by_current_email_are_recognized(tmp_path):
    """새 이메일로 이미 저장한 끼니는 알 수 없는 식사가 아니라 이미 기록한 끼니다."""
    uuid = meal_uuid(email_of(CONFIG, 42, 1), DAY, "LUNCH")
    client = _FakeClient({
        ("GET", f"/daily-summaries/{DAY}"): (200, {"data": {"intakeKcal": 300.0}}),
        ("GET", "/meals"): (200, {"data": {"meals": [{"clientUuid": uuid}]}}),
    })
    api, _ = _api(tmp_path, client, generation=1)

    with pytest.raises(SessionSkipped):
        meal_log.run(_ctx(api), _meal())
