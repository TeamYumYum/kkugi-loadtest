import base64
import json

import pytest

from kkugi_load import config as config_module
from kkugi_load.client.api import jwt_expires_at
from kkugi_load.accounts.state import AccountState, StateStore


def test_state_round_trip(tmp_path):
    store = StateStore(tmp_path / "s.db")
    state = store.load("a@x.test", 1, "dieter")
    assert state == AccountState(email="a@x.test", account_index=1, persona="dieter")
    state.access_token, state.refresh_token, state.onboarded = "acc", "ref", True
    state.breakfast_ready_on = "2026-10-28"
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


def test_config_reads_account_range_and_header(tmp_path):
    config = config_module.load({"KKUGI_ACCOUNT_START": "100", "KKUGI_ACCOUNT_COUNT": "5"})
    assert (config.account_start, config.account_count) == (100, 5)
    assert config.synthetic_header is None
    with pytest.raises(ValueError):
        config_module.load({"KKUGI_ACCOUNT_COUNT": "0"})
