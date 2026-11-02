"""체중 기록 세션. 요청 순서, 저장 본문, 201·200 허용, 저장 결과 검증, 체중 계산 도우미."""

import base64
import json
import random
from datetime import datetime

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.sessions import weight_log
from kkugi_load.sessions.base import SessionContext, ValidationFailed
from kkugi_load.sessions.weight_log import WeightInputs, drifted_weight

CONFIG = config_module.load({})
PLANNED_AT = datetime(2026, 10, 28, 7, 30, tzinfo=CONFIG.tz)
DAY = "2026-10-28"
HOME = ["/daily-summaries/2026-10-28", "/daily-goals/2026-10-28", "/growth/me"]
PUT_PATH = f"/weight-records/{DAY}"


def _jwt(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


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

    def __init__(self, routes: dict):
        self.routes = routes
        self.sent = []
        self.kwargs = []
        self.outcomes = []

    def request(self, method, url, **kwargs):
        path = url.removeprefix(CONFIG.api_prefix)
        self.sent.append((method, path))
        self.kwargs.append(kwargs)
        status, body = self.routes.get((method, path), (200, {"data": {}}))
        return _FakeResponse(status, body, self.outcomes)


def _api(tmp_path, client):
    store = StateStore(tmp_path / "s.db")
    account = build_account(CONFIG, 42)
    state = store.load(42, account.persona)
    state.member_id, state.access_token, state.refresh_token, state.onboarded = 1, _jwt(4102444800), "r", True
    store.save(state)
    return KkugiApi(client, CONFIG, account, state, store)


def _ctx(api, thinks: list) -> SessionContext:
    return SessionContext(
        api=api, config=CONFIG, now=lambda: PLANNED_AT, sleep=thinks.append, rng=random.Random(0),
    )


def _routes(put_status=201, put_weight=70.4, list_weight=70.4, list_day=DAY):
    return {
        ("GET", f"/daily-summaries/{DAY}"): (200, {"data": {"date": DAY, "intakeKcal": 0}}),
        ("GET", "/weight-records/latest"): (200, {"data": {"weightKg": 71.0, "recordedOn": None}}),
        ("PUT", PUT_PATH): (put_status, {"data": {"recordedOn": DAY, "weightKg": put_weight}}),
        ("GET", "/weight-records"): (200, {"data": {"items": [
            {"recordedOn": "2026-10-20", "weightKg": 70.9},
            {"recordedOn": list_day, "weightKg": list_weight},
        ]}}),
    }


def test_weight_log_requests_in_order(tmp_path):
    client = _FakeClient(_routes())
    thinks = []

    outcome = weight_log.run(_ctx(_api(tmp_path, client), thinks), WeightInputs(PLANNED_AT, 70.4))

    assert client.sent == [("GET", p) for p in HOME] + [
        ("GET", "/weight-records/latest"), ("PUT", PUT_PATH), ("GET", "/weight-records"),
    ]
    assert client.outcomes == ["success"] * 6
    # 생각 시간은 체중 입력 한 번뿐이다
    assert len(thinks) == 1 and CONFIG.select_think[0] <= thinks[0] <= CONFIG.select_think[1]
    assert outcome == {"date": DAY, "weight_kg": 70.4}


def test_weight_log_sends_body_and_30_day_range(tmp_path):
    client = _FakeClient(_routes())

    weight_log.run(_ctx(_api(tmp_path, client), []), WeightInputs(PLANNED_AT, 70.4))

    put = client.kwargs[4]
    assert put["json"] == {"weightKg": 70.4}
    assert put["name"].endswith("PUT /weight-records/{recordedOn}")
    graph = client.kwargs[5]
    assert graph["params"] == {"from": "2026-09-29", "to": DAY}
    assert graph["name"].endswith("GET /weight-records")
    assert client.kwargs[3]["name"].endswith("GET /weight-records/latest")


@pytest.mark.parametrize("status", [201, 200])
def test_weight_log_accepts_created_and_replaced(tmp_path, status):
    client = _FakeClient(_routes(put_status=status))

    weight_log.run(_ctx(_api(tmp_path, client), []), WeightInputs(PLANNED_AT, 70.4))

    assert client.outcomes == ["success"] * 6


def test_weight_log_fails_when_saved_weight_differs(tmp_path):
    client = _FakeClient(_routes(put_weight=70.5))

    with pytest.raises(ValidationFailed, match="70.5"):
        weight_log.run(_ctx(_api(tmp_path, client), []), WeightInputs(PLANNED_AT, 70.4))


def test_weight_log_fails_when_list_misses_today(tmp_path):
    client = _FakeClient(_routes(list_day="2026-10-27"))

    with pytest.raises(ValidationFailed, match=DAY):
        weight_log.run(_ctx(_api(tmp_path, client), []), WeightInputs(PLANNED_AT, 70.4))


def test_weight_log_fails_when_list_weight_differs(tmp_path):
    client = _FakeClient(_routes(list_weight=69.0))

    with pytest.raises(ValidationFailed, match="69.0"):
        weight_log.run(_ctx(_api(tmp_path, client), []), WeightInputs(PLANNED_AT, 70.4))


def test_drifted_weight_applies_trend_and_noise_and_rounds():
    assert drifted_weight(80.0, 10, -0.05, 0.23) == 79.7
    assert drifted_weight(65.0, 0, 0.0, -0.04) == 65.0


@pytest.mark.parametrize(("start", "expected"), [(21.0, 20.0), (299.5, 300.0)])
def test_drifted_weight_clamps_to_api_range(start, expected):
    trend = -0.05 if start < 100 else 0.05
    assert drifted_weight(start, 100, trend, 0.0) == expected
