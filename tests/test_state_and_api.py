import base64
import json
from datetime import time

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import Account
from kkugi_load.client.api import KkugiApi, jwt_expires_at
from kkugi_load.accounts.state import AccountState, StateStore, StaleStateDb


def test_state_round_trip(tmp_path):
    store = StateStore(tmp_path / "s.db")
    state = store.load(1, "dieter")
    assert state == AccountState(account_index=1, persona="dieter", generation=0)
    state.access_token, state.refresh_token, state.onboarded = "acc", "ref", True
    state.generation = 2
    store.save(state)
    store.close()
    assert StateStore(tmp_path / "s.db").load(1, "dieter") == state


def test_state_rejects_mismatched_assignment(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.load(1, "dieter")
    with pytest.raises(ValueError):
        store.load(1, "other")


def test_old_state_db_stops_start(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    # 이메일이 기본 키이고 generation이 없던 형식
    conn.execute(
        "CREATE TABLE account_state (email TEXT PRIMARY KEY, account_index INTEGER NOT NULL, persona TEXT NOT NULL,"
        " member_id INTEGER, access_token TEXT, refresh_token TEXT, onboarded INTEGER NOT NULL DEFAULT 0,"
        " updated_at TEXT NOT NULL)"
    )
    conn.close()
    with pytest.raises(StaleStateDb) as e:
        StateStore(path)
    assert str(path) in str(e.value) and "지우" in str(e.value)


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


def test_jwt_expiry_is_read_without_verification():
    assert jwt_expires_at(_jwt({"exp": 1793171897})) == 1793171897
    assert jwt_expires_at("not-a-jwt") is None


def test_config_reads_pool_and_overrides():
    base = config_module.load({})
    assert (base.pool.name, base.pool.start, base.pool.end) == ("base", 0, 1000)
    assert base.synthetic_header == ("X-Synthetic-Traffic", "1")
    peak = config_module.load({"KKUGI_POOL": "peak-1"})
    assert peak.pool.start == 10000 and peak.pool.only_between
    small = config_module.load({"KKUGI_ACCOUNT_START": "100", "KKUGI_ACCOUNT_COUNT": "5"})
    assert (small.pool.start, small.pool.end) == (100, 105)
    with pytest.raises(ValueError):
        config_module.load({"KKUGI_ACCOUNT_COUNT": "0"})
    with pytest.raises(ValueError):
        config_module.load({"KKUGI_POOL": "missing"})


def test_default_pools_do_not_overlap():
    import tomllib

    with config_module.DEFAULT_CONFIG.open("rb") as f:
        pools = tomllib.load(f)["pools"]
    ranges = sorted((p["start"], p["start"] + p["count"]) for p in pools.values())
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(ranges, ranges[1:]))


def test_pool_allows_only_its_hours():
    peak = config_module.load({"KKUGI_POOL": "peak-1"}).pool
    assert peak.allows(time(12, 0)) and not peak.allows(time(15, 0)) and not peak.allows(time(13, 30))


class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def success(self):
        pass

    def failure(self, _message):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeClient:
    """요청 헤더를 기록하고 정해진 응답을 돌려준다."""

    def __init__(self):
        self.sent = []

    def request(self, method, url, *, headers, **_kwargs):
        self.sent.append((method, url, dict(headers)))
        if url.endswith("/auth/login/dev"):
            token = _jwt({"exp": 4102444800})
            return _FakeResponse(200, {"data": {"memberId": 1, "accessToken": token, "refreshToken": "r",
                                                "onboarded": True}})
        return _FakeResponse(200, {"data": {}})


def test_every_request_carries_synthetic_header(tmp_path):
    config = config_module.load({})
    store = StateStore(tmp_path / "s.db")
    account = Account(index=0, persona="dieter", profile=None)
    client = _FakeClient()
    api = KkugiApi(client, config, account, store.load(0, "dieter"), store)

    api.call("GET", "/growth/me", name="GET /growth/me")

    assert [url for _, url, _ in client.sent] == ["/api/v1/auth/login/dev", "/api/v1/growth/me"]
    for _, _, headers in client.sent:
        assert headers["X-Synthetic-Traffic"] == "1"
    assert "Authorization" not in client.sent[0][2]
    assert client.sent[1][2]["Authorization"].startswith("Bearer ")
