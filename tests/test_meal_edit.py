"""식사 기록 세션의 수정 분기. 저장 직후 양을 고치고 요약 변화를 검증한다."""

import base64
import json
import random
from dataclasses import replace
from datetime import date, datetime

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.sessions import meal_log
from kkugi_load.sessions.base import SessionContext, ValidationFailed
from kkugi_load.sessions.meal_log import MealInputs

CONFIG = config_module.load({})
DAY = date(2026, 10, 28)


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
        raise AssertionError(f"요청 실패로 기록됨: {message}")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeClient:
    """요청을 기록하고, routes에서 (메서드, 경로)에 맞는 응답을 돌려준다."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.sent = []

    def request(self, method, url, *, json=None, params=None, name=None, **_kwargs):
        path = url.removeprefix(CONFIG.api_prefix)
        self.sent.append((method, path, json, name))
        status, body = self.routes[(method, path)](json, params)
        return _FakeResponse(status, body)


class _Server:
    """식사 하나와 하루 섭취 칼로리를 들고 있는 가짜 서버. 칼로리는 그램당 kcal_per_gram이다."""

    KCAL_PER_GRAM = 1.5

    def __init__(self):
        self.intake = 0.0
        self.meal = None
        # 시험에서 응답이나 요약을 일부러 틀리게 만드는 손잡이
        self.patch_grams_override = None
        self.summary_offset = 0.0

    def _kcal(self, items):
        return sum(item["grams"] for item in items) * self.KCAL_PER_GRAM

    def summary(self, _json, _params):
        return 200, {"data": {"intakeKcal": self.intake}}

    def save(self, body, _params):
        items = [dict(item) for item in body["items"]]
        self.meal = {
            "id": 9, "clientUuid": body["clientUuid"], "mealType": body["mealType"], "eatenAt": body["eatenAt"],
            "items": items, "totalNutrition": {"kcal": self._kcal(items)},
        }
        self.intake += self._kcal(items)
        return 201, {"data": self.meal}

    def change(self, body, _params):
        old = self._kcal(self.meal["items"])
        items = [dict(item) for item in body["items"]]
        self.meal = {**self.meal, "eatenAt": body["eatenAt"], "mealType": body["mealType"], "items": items,
                     "totalNutrition": {"kcal": self._kcal(items)}}
        self.intake += self._kcal(items) - old + self.summary_offset
        data = self.meal
        if self.patch_grams_override is not None:
            data = {**data, "items": [{**item, "grams": self.patch_grams_override} for item in items]}
        return 200, {"data": data}

    def routes(self):
        return {
            ("GET", f"/daily-summaries/{DAY}"): self.summary,
            ("GET", f"/daily-goals/{DAY}"): lambda _j, _p: (200, {"data": {}}),
            ("GET", "/growth/me"): lambda _j, _p: (200, {"data": {}}),
            ("GET", "/meals"): lambda _j, _p: (200, {"data": {"meals": []}}),
            ("GET", "/foods"): lambda _j, _p: (200, {"data": {"items": [{"id": 5, "name": "밥"},
                                                                          {"id": 7, "name": "국"}]}}),
            ("POST", "/meals"): self.save,
            ("PATCH", "/meals/9"): self.change,
        }


def _run(tmp_path, server: _Server, meal: MealInputs):
    client = _FakeClient(server.routes())
    store = StateStore(tmp_path / "s.db")
    account = build_account(CONFIG, 42)
    state = store.load(42, account.persona)
    state.member_id, state.access_token, state.refresh_token, state.onboarded = 1, _jwt(4102444800), "r", True
    store.save(state)
    api = KkugiApi(client, CONFIG, account, state, store)
    thinks = []
    ctx = SessionContext(
        api=api, config=CONFIG, now=lambda: datetime(2026, 10, 28, 13, 0, tzinfo=CONFIG.tz),
        sleep=thinks.append, rng=random.Random(0),
    )
    return meal_log.run(ctx, meal), client, thinks


def _meal(**kwargs) -> MealInputs:
    return MealInputs(
        meal_type="LUNCH", planned_at=datetime(2026, 10, 28, 12, 0, tzinfo=CONFIG.tz), keywords=("밥", "국"),
        fallback_keywords=(), grams=(100, 200), picks=(0, 1), **kwargs,
    )


def _requests(client):
    return [(method, path) for method, path, _, _ in client.sent]


TODAY_FLOW = [
    ("GET", f"/daily-summaries/{DAY}"), ("GET", f"/daily-goals/{DAY}"), ("GET", "/growth/me"),
    ("GET", "/meals"), ("GET", "/foods"), ("GET", "/foods"), ("POST", "/meals"),
    ("GET", f"/daily-summaries/{DAY}"),
]


def test_no_edit_when_field_absent(tmp_path):
    outcome, client, thinks = _run(tmp_path, _Server(), _meal())

    assert _requests(client) == TODAY_FLOW
    assert "edited_kcal" not in outcome
    # 검색 2번, 고르기 2번
    assert len(thinks) == 4


def test_edit_sends_patch_and_checks_summary(tmp_path):
    server = _Server()
    outcome, client, thinks = _run(tmp_path, server, _meal(edit_grams=(150, 200)))

    assert _requests(client) == TODAY_FLOW + [("PATCH", "/meals/9"), ("GET", f"/daily-summaries/{DAY}")]
    method, path, body, name = client.sent[-2]
    assert name == CONFIG.route_prefix + "PATCH /meals/{mealId}"
    saved = next(b for m, p, b, _ in client.sent if (m, p) == ("POST", "/meals"))
    assert body == {
        "eatenAt": saved["eatenAt"], "mealType": "LUNCH",
        "items": [{"foodId": 5, "grams": 150}, {"foodId": 7, "grams": 200}],
    }
    assert outcome["kcal"] == 300 * _Server.KCAL_PER_GRAM
    assert outcome["edited_kcal"] == 350 * _Server.KCAL_PER_GRAM
    # 수정 전에 생각 시간을 한 번 더 둔다
    assert len(thinks) == 5


def test_edit_rejects_wrong_grams_in_response(tmp_path):
    server = _Server()
    server.patch_grams_override = 999
    with pytest.raises(ValidationFailed, match="수정 결과 불일치"):
        _run(tmp_path, server, _meal(edit_grams=(150, 200)))


def test_edit_rejects_wrong_summary_delta(tmp_path):
    server = _Server()
    server.summary_offset = 50.0
    with pytest.raises(ValidationFailed, match="수정 뒤 요약"):
        _run(tmp_path, server, _meal(edit_grams=(150, 200)))


def test_edit_grams_count_must_match_foods(tmp_path):
    with pytest.raises(ValueError):
        _run(tmp_path, _Server(), _meal(edit_grams=(150,)))


def test_edit_grams_default_is_none():
    assert _meal().edit_grams is None
    assert replace(_meal(), edit_grams=(1, 2)).edit_grams == (1, 2)
