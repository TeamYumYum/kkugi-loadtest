"""운영 중 조절: Master 상태(Control), Worker 적용(Applier), 웹 UI 경로."""

from datetime import date, datetime, timedelta

import pytest
from flask import Flask

from kkugi_load import config as config_module
from kkugi_load.control import Applier, Control, add_routes
from kkugi_load.personas import PERSONAS, build_account
from kkugi_load.scheduler import REALTIME, Scheduler

DEFAULT_TEXT = config_module.DEFAULT_CONFIG.read_text(encoding="utf-8")
CONFIG = config_module.loads(DEFAULT_TEXT, {})
DAY = date(2026, 10, 28)
THINK = DEFAULT_TEXT.replace("search_min = 2.0", "search_min = 3.0")
FROZEN = DEFAULT_TEXT.replace('api_prefix = "/api/v1"', 'api_prefix = "/api/v2"')


def _control(tmp_path, text: str = DEFAULT_TEXT) -> tuple[Control, object]:
    """설정 파일을 임시 경로에 두고 그 파일로 시작한 Control. 파일을 고쳐 다시 읽기를 시험한다."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return Control(1.0, text, {"KKUGI_CONFIG": str(path)}), path


def _scheduler(now: datetime, env=None) -> Scheduler:
    config = config_module.loads(DEFAULT_TEXT, env or {})
    accounts = [build_account(config, i) for i in range(config.pool.start, config.pool.start + 20)]
    return Scheduler(config, accounts, REALTIME, timedelta(minutes=30), lambda: now, config.pool)


# --- Control ---

def test_payload_has_scale_version_and_no_config_before_reload(tmp_path):
    control, _ = _control(tmp_path)
    assert control.payload() == {"scale": 1.0, "version": 0, "config": None}
    control.set_requested_scale("0.25")
    assert control.payload()["scale"] == 0.25


@pytest.mark.parametrize("bad", ["-0.1", "1.01", "nan", "inf", "abc", "", None, True])
def test_invalid_scale_is_rejected_and_keeps_current(tmp_path, bad):
    control, _ = _control(tmp_path)
    control.set_requested_scale(0.5)
    with pytest.raises(ValueError):
        control.set_requested_scale(bad)
    assert control.requested_scale == control.scale == 0.5


def test_automatic_scale_never_exceeds_requested_scale(tmp_path):
    control, _ = _control(tmp_path)
    control.set_requested_scale(0.6)
    assert control.limit_scale(0.3) == 0.3
    assert control.requested_scale == 0.6
    assert control.limit_scale(1.0) == 0.6
    # 사람이 다시 정하면 실제 배율도 그 값이 된다
    control.set_requested_scale(0.8)
    assert control.scale == 0.8


def test_reload_sends_new_text_and_bumps_version(tmp_path):
    control, path = _control(tmp_path)
    path.write_text(THINK, encoding="utf-8")
    assert control.reload() == 1
    assert control.payload() == {"scale": 1.0, "version": 1, "config": THINK}


@pytest.mark.parametrize("text, reason", [
    (FROZEN, "api_prefix"),
    (DEFAULT_TEXT.replace("[pools.peak-2]\nstart = 11000", "[pools.peak-2]\nstart = 11500"), "pools"),
    (DEFAULT_TEXT.replace('header_value = "1"', 'header_value = "2"'), "synthetic"),
    (DEFAULT_TEXT.replace("search_min = 2.0", "search_min = "), "설정을 읽지 못함"),
    (DEFAULT_TEXT.replace("activity = 1.0\n", "activity = 1.5\n", 1), "1을 넘는다"),
])
def test_reload_rejects_invalid_or_frozen_changes_and_sends_nothing(tmp_path, text, reason):
    control, path = _control(tmp_path)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        control.reload()
    assert control.payload() == {"scale": 1.0, "version": 0, "config": None}


# --- Applier ---

def test_applier_sets_scale_and_replans_with_new_config_keeping_pool():
    now = datetime.combine(DAY, datetime.min.time(), CONFIG.tz) + timedelta(hours=12)
    env = {"KKUGI_ACCOUNT_START": "5", "KKUGI_ACCOUNT_COUNT": "20"}
    scheduler = _scheduler(now, env)
    scheduler.plan_if_needed()
    applier = Applier(scheduler, DEFAULT_TEXT, env)

    status = applier.apply({"scale": 0.5, "version": 0, "config": None})
    assert status == {"scale": 0.5, "version": 0, "error": None}
    assert scheduler.config.search_think == (2.0, 6.0)

    # 받은 설정의 풀 범위가 아니라 시작할 때 환경 변수로 덮어쓴 범위를 유지한다
    status = applier.apply({"scale": 0.5, "version": 3, "config": THINK})
    assert status == {"scale": 0.5, "version": 3, "error": None}
    assert scheduler.config.search_think == (3.0, 6.0)
    assert scheduler.config.pool.start == 5 and scheduler.config.pool.count == 20
    # 다시 만든 일정에는 지금 이후의 세션만 있다
    expected = [
        p for a in scheduler.accounts for p in PERSONAS[a.persona].plan_day(CONFIG, a, DAY) if p.at >= now
    ]
    assert scheduler.pending() == len(expected)


def test_applier_rejects_config_with_frozen_changes_and_keeps_schedule():
    now = datetime.combine(DAY, datetime.min.time(), CONFIG.tz) + timedelta(hours=12)
    scheduler = _scheduler(now)
    scheduler.plan_if_needed()
    before = scheduler.pending()
    applier = Applier(scheduler, DEFAULT_TEXT, {})
    status = applier.apply({"scale": 0.2, "version": 1, "config": FROZEN})
    assert status["scale"] == 0.2 and status["version"] == 0
    assert "api_prefix" in status["error"]
    assert scheduler.config.api_prefix == "/api/v1" and scheduler.pending() == before
    # 배율만 바뀐 다음 메시지에도 같은 원문이 실려 오면 다시 시도하지 않고 같은 이유를 알린다
    again = applier.apply({"scale": 0.3, "version": 1, "config": FROZEN})
    assert again["scale"] == 0.3 and again["error"] == status["error"]


def test_applier_does_not_replan_for_the_same_text():
    now = datetime.combine(DAY, datetime.min.time(), CONFIG.tz) + timedelta(hours=12)
    scheduler = _scheduler(now)
    scheduler.plan_if_needed()
    plans = []
    scheduler.on_plan = plans.append
    applier = Applier(scheduler, DEFAULT_TEXT, {})
    applier.apply({"scale": 1.0, "version": 1, "config": THINK})
    applier.apply({"scale": 0.5, "version": 1, "config": THINK})
    assert len(plans) == 1


def test_applier_reports_invalid_scale_without_raising():
    scheduler = _scheduler(datetime.combine(DAY, datetime.min.time(), CONFIG.tz))
    status = Applier(scheduler, DEFAULT_TEXT, {}).apply({"scale": 2, "version": 0, "config": None})
    assert status["scale"] == 1.0 and "배율" in status["error"]


# --- 웹 UI 경로 ---

class FakeWebUI:
    def __init__(self):
        self.app = Flask(__name__)

    @staticmethod
    def auth_required_if_enabled(view):
        return view


@pytest.fixture
def web(tmp_path):
    control, path = _control(tmp_path)
    published = []
    ui = FakeWebUI()
    add_routes(ui, control, lambda: published.append(control.payload()))
    return ui.app.test_client(), control, path, published


def test_scale_route_sets_scale_and_publishes(web):
    client, control, _, published = web
    response = client.post("/kkugi/scale", data={"scale": "0.4"})
    assert response.status_code == 200
    assert response.get_json()["scale"] == 0.4
    assert published == [{"scale": 0.4, "version": 0, "config": None}]


@pytest.mark.parametrize("data", [{"scale": "1.5"}, {"scale": "-1"}, {"scale": "x"}, {}])
def test_scale_route_rejects_invalid_value_with_400(web, data):
    client, control, _, published = web
    response = client.post("/kkugi/scale", data=data)
    assert response.status_code == 400
    assert response.get_json()["error"]
    assert published == [] and control.scale == 1.0


def test_stop_route_sets_scale_zero(web):
    client, control, _, published = web
    assert client.post("/kkugi/stop").status_code == 200
    assert control.requested_scale == control.scale == 0.0
    assert published[-1]["scale"] == 0.0


def test_reload_route_publishes_new_text_or_returns_400(web):
    client, control, path, published = web
    path.write_text(FROZEN, encoding="utf-8")
    response = client.post("/kkugi/reload")
    assert response.status_code == 400 and "api_prefix" in response.get_json()["error"]
    assert published == []

    path.write_text(THINK, encoding="utf-8")
    response = client.post("/kkugi/reload")
    assert response.status_code == 200 and response.get_json()["config_version"] == 1
    assert published == [{"scale": 1.0, "version": 1, "config": THINK}]


def test_control_route_shows_worker_status(web):
    client, control, _, _ = web
    control.record("w1", {"scale": 1.0, "version": 0, "error": None})
    body = client.get("/kkugi/control").get_json()
    assert body["requested_scale"] == body["scale"] == 1.0
    assert body["config_version"] == 0
    assert body["workers"]["w1"]["scale"] == 1.0 and "reported_at" in body["workers"]["w1"]
