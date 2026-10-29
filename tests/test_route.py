"""요청 경로 분할: 공인 경로 계정 풀과 경로 표시(KKUGI_ROUTE). notes/decisions/2026-10-29-route-split.md"""

import tomllib
from datetime import date, datetime, timedelta

import pytest
from locust.stats import RequestStats

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import Account
from kkugi_load.accounts.state import StateStore
from kkugi_load.client.api import KkugiApi
from kkugi_load.control import Applier
from kkugi_load.governor import http_totals
from kkugi_load.personas import build_account
from kkugi_load.scheduler import REALTIME, Scheduler

from tests.test_state_and_api import _FakeClient

DEFAULT_TEXT = config_module.DEFAULT_CONFIG.read_text(encoding="utf-8")
THINK = DEFAULT_TEXT.replace("search_min = 2.0", "search_min = 3.0")


class _NamingClient(_FakeClient):
    """요청마다 통계 이름을 기록한다."""

    def __init__(self):
        super().__init__()
        self.names = []

    def request(self, method, url, *, name, **kwargs):
        self.names.append(name)
        return super().request(method, url, **kwargs)


def _api(tmp_path, env: dict[str, str]) -> tuple[KkugiApi, _NamingClient]:
    config = config_module.load(env)
    store = StateStore(tmp_path / "s.db")
    client = _NamingClient()
    return KkugiApi(client, config, Account(index=0, persona="dieter", profile=None), store.load(0, "dieter"),
                    store), client


def test_base_and_public_pools_split_the_first_thousand_accounts_95_to_5():
    with config_module.DEFAULT_CONFIG.open("rb") as f:
        pools = tomllib.load(f)["pools"]
    assert (pools["base"]["start"], pools["base"]["count"]) == (0, 950)
    assert (pools["public"]["start"], pools["public"]["count"]) == (950, 50)
    public = config_module.load({"KKUGI_POOL": "public"}).pool
    assert (public.start, public.end, public.only_between) == (950, 1000, ())


def test_route_is_empty_without_env_and_read_from_env():
    assert config_module.load({}).route == ""
    assert config_module.load({}).route_prefix == ""
    public = config_module.load({"KKUGI_ROUTE": "public"})
    assert public.route == "public" and public.route_prefix == "[public] "
    assert config_module.load({"KKUGI_ROUTE": "via-nat-2"}).route == "via-nat-2"


@pytest.mark.parametrize("bad", ["Public", "public route", "[public]", "공인", "public_1", " public"])
def test_route_with_other_characters_stops_start(bad):
    with pytest.raises(ValueError, match="KKUGI_ROUTE"):
        config_module.load({"KKUGI_ROUTE": bad})


def test_http_stat_names_carry_route_prefix(tmp_path):
    api, client = _api(tmp_path, {"KKUGI_ROUTE": "public"})
    api.call("GET", "/foods", name="GET /foods")
    assert client.names == ["[public] [auth] POST /auth/login/dev", "[public] GET /foods"]


def test_http_stat_names_are_unchanged_without_route(tmp_path):
    api, client = _api(tmp_path, {})
    api.call("GET", "/foods", name="GET /foods")
    assert client.names == ["[auth] POST /auth/login/dev", "GET /foods"]


def test_governor_counts_route_prefixed_http_entries():
    # 거버너는 이름이 아니라 메서드 칸으로 HTTP 항목을 고르므로 경로 표시가 붙어도 그대로 센다
    stats = RequestStats()
    for _ in range(3):
        stats.log_request("GET", "[public] GET /foods", 100, 10)
        stats.log_request("POST", "[public] [auth] POST /auth/login/dev", 200, 10)
        stats.log_request("GET", "GET /foods", 300, 10)
    stats.log_error("GET", "[public] GET /foods", "HTTP 500")
    stats.log_request("SESSION", "view", 20_000, 0)
    stats.log_request("SCHEDULE", "start_delay", 9_000, 0)
    totals = http_totals(stats)
    assert totals.requests == 9 and totals.failures == 1
    assert totals.response_times == {100: 3, 200: 3, 300: 3}


def test_reload_keeps_route_from_startup_env():
    # Applier는 시작할 때의 환경 변수로 다시 읽으므로 받은 설정이 바뀌어도 경로 표시는 그대로다
    env = {"KKUGI_ROUTE": "public", "KKUGI_POOL": "public"}
    config = config_module.loads(DEFAULT_TEXT, env)
    accounts = [build_account(config, i) for i in range(config.pool.start, config.pool.start + 5)]
    now = datetime.combine(date(2026, 10, 28), datetime.min.time(), config.tz) + timedelta(hours=12)
    scheduler = Scheduler(config, accounts, REALTIME, timedelta(minutes=30), lambda: now, config.pool)
    scheduler.plan_if_needed()
    applier = Applier(scheduler, DEFAULT_TEXT, env)

    status = applier.apply({"scale": 1.0, "version": 1, "config": THINK})
    assert status["error"] is None
    assert scheduler.config.search_think == (3.0, 6.0)
    assert scheduler.config.route == "public"
