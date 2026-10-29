"""조회 세션. 홈 화면 요청 순서와 요약 날짜 검증."""

import base64
import json
import random
from datetime import datetime

import pytest

from kkugi_load import config as config_module
from kkugi_load import runner
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.personas import build_account
from kkugi_load.personas.base import PlannedSession
from kkugi_load.sessions import view
from kkugi_load.sessions.base import SessionContext, ValidationFailed
from kkugi_load.sessions.view import ViewInputs

CONFIG = config_module.load({})
PLANNED_AT = datetime(2026, 10, 28, 15, 30, tzinfo=CONFIG.tz)
DAY = "2026-10-28"
SUMMARY = (200, {"data": {"date": DAY, "intakeKcal": 650}})
HOME = ["/daily-summaries/2026-10-28", "/daily-goals/2026-10-28", "/growth/me"]


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
        self.outcomes = []

    def request(self, method, url, **_kwargs):
        path = url.removeprefix(CONFIG.api_prefix)
        self.sent.append((method, path))
        status, body = self.routes.get((method, path), (200, {"data": {}}))
        return _FakeResponse(status, body, self.outcomes)


def _api(tmp_path, client):
    store = StateStore(tmp_path / "s.db")
    account = build_account(CONFIG, 42)
    state = store.load(42, account.persona)
    state.member_id, state.access_token, state.refresh_token, state.onboarded = 1, _jwt(4102444800), "r", True
    store.save(state)
    return KkugiApi(client, CONFIG, account, state, store)


def _ctx(api) -> SessionContext:
    def no_think(_s):
        raise AssertionError("조회 세션은 생각 시간을 두지 않는다")

    return SessionContext(
        api=api, config=CONFIG, now=lambda: PLANNED_AT, sleep=no_think, rng=random.Random(0),
    )


def test_view_opens_home_in_order(tmp_path):
    client = _FakeClient({("GET", f"/daily-summaries/{DAY}"): SUMMARY})

    outcome = view.run(_ctx(_api(tmp_path, client)), ViewInputs(planned_at=PLANNED_AT))

    assert client.sent == [("GET", path) for path in HOME]
    assert client.outcomes == ["success"] * 3
    assert outcome == {"date": DAY, "intake_kcal": 650}


def test_view_fails_when_summary_date_differs(tmp_path):
    client = _FakeClient({("GET", f"/daily-summaries/{DAY}"): (200, {"data": {"date": "2026-10-27", "intakeKcal": 0}})})

    with pytest.raises(ValidationFailed, match="2026-10-27"):
        view.run(_ctx(_api(tmp_path, client)), ViewInputs(planned_at=PLANNED_AT))


def test_view_succeeds_without_daily_goal(tmp_path):
    """코칭 계획이 아직 없어 목표가 404 NOT_FOUND여도 홈 화면은 열린다."""
    client = _FakeClient({
        ("GET", f"/daily-summaries/{DAY}"): SUMMARY,
        ("GET", f"/daily-goals/{DAY}"): (404, {"error": {"code": "NOT_FOUND", "message": "없음"}}),
    })

    view.run(_ctx(_api(tmp_path, client)), ViewInputs(planned_at=PLANNED_AT))

    assert [path for _, path in client.sent] == HOME
    assert client.outcomes == ["success"] * 3


class _Events:
    class _Request:
        def __init__(self):
            self.fired = []

        def fire(self, **kwargs):
            self.fired.append(kwargs)

    def __init__(self):
        self.request = self._Request()


@pytest.mark.parametrize(("summary", "outcome", "failed"), [
    (SUMMARY, runner.OK, False),
    ((200, {"data": {"date": "2026-10-27", "intakeKcal": 0}}), runner.FAILED, True),
])
def test_runner_records_view_under_planned_name(tmp_path, summary, outcome, failed):
    client = _FakeClient({("GET", f"/daily-summaries/{DAY}"): summary})
    planned = PlannedSession(42, "dieter_view", PLANNED_AT, "view", ViewInputs(planned_at=PLANNED_AT))
    events = _Events()

    assert runner.run_planned(events, _api(tmp_path, client), CONFIG, planned, random.Random(0)) == outcome

    fired = events.request.fired
    assert [(f["request_type"], f["name"]) for f in fired] == [("SESSION", "dieter_view")]
    assert isinstance(fired[0]["exception"], ValidationFailed) if failed else fired[0]["exception"] is None
