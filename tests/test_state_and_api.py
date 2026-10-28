import base64
import json
from datetime import time

import pytest

from kkugi_load import config as config_module
from kkugi_load.client.api import jwt_expires_at
from kkugi_load.accounts.state import AccountState, StateStore


def test_state_round_trip(tmp_path):
    store = StateStore(tmp_path / "s.db")
    state = store.load("a@x.test", 1, "dieter")
    assert state == AccountState(email="a@x.test", account_index=1, persona="dieter")
    state.access_token, state.refresh_token, state.onboarded = "acc", "ref", True
    store.save(state)
    store.close()
    assert StateStore(tmp_path / "s.db").load("a@x.test", 1, "dieter") == state


def test_state_rejects_mismatched_assignment(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.load("a@x.test", 1, "dieter")
    with pytest.raises(ValueError):
        store.load("a@x.test", 2, "dieter")


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


def test_jwt_expiry_is_read_without_verification():
    assert jwt_expires_at(_jwt({"exp": 1793171897})) == 1793171897
    assert jwt_expires_at("not-a-jwt") is None


def test_config_reads_pool_and_overrides():
    base = config_module.load({})
    assert (base.pool.name, base.pool.start, base.pool.end) == ("base", 0, 1000)
    assert base.synthetic_header is None
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
