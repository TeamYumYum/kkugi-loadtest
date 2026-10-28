from datetime import date, datetime, timedelta

from kkugi_load import config as config_module
from kkugi_load.accounts import build_account, build_day_plan, eaten_at

CONFIG = config_module.load({})


def test_account_is_deterministic():
    assert build_account(CONFIG, 7) == build_account(CONFIG, 7)
    assert build_account(CONFIG, 7).email == "lt-00007@synthetic.kkugi.test"
    assert build_account(CONFIG, 7) != build_account(CONFIG, 8)


def test_onboarding_body_is_within_server_limits():
    for index in range(200):
        body = build_account(CONFIG, index).profile.onboarding_body()
        assert 2 <= len(body["nickname"]) <= 12
        assert 100 <= body["heightCm"] <= 250
        assert 20 <= body["weightKg"] <= 300
        assert date.fromisoformat(body["birthDate"]).year <= date.today().year - 14


def test_day_plan_repeats_for_same_day_and_changes_uuid_per_day():
    account = build_account(CONFIG, 3)
    day = date(2026, 10, 28)
    plan = build_day_plan(CONFIG, account, day)
    assert plan == build_day_plan(CONFIG, account, day)
    assert plan.breakfast.client_uuid != plan.lunch.client_uuid
    other = build_day_plan(CONFIG, account, day + timedelta(days=1))
    assert other.lunch.client_uuid != plan.lunch.client_uuid


def test_lunch_uses_preferred_keywords_and_configured_ranges():
    account = build_account(CONFIG, 11)
    lunch_cfg = CONFIG.diet_lunch
    plan = build_day_plan(CONFIG, account, date(2026, 10, 28))
    assert len(plan.lunch.keywords) == lunch_cfg.items
    assert set(plan.lunch.keywords) <= set(account.profile.preferred_lunch_keywords)
    assert all(lunch_cfg.grams_min <= g <= lunch_cfg.grams_max for g in plan.lunch.grams)
    assert lunch_cfg.lunch_from <= plan.lunch.planned_at.time() <= lunch_cfg.lunch_to
    assert plan.lunch.planned_at.tzinfo == CONFIG.tz


def test_eaten_at_never_goes_past_now_or_before_midnight():
    tz = CONFIG.tz
    now = datetime(2026, 10, 28, 10, 0, 30, 123, tzinfo=tz)
    assert eaten_at(datetime(2026, 10, 28, 12, 30, tzinfo=tz), now) == now.replace(microsecond=0)
    assert eaten_at(datetime(2026, 10, 28, 8, 0, tzinfo=tz), now) == datetime(2026, 10, 28, 8, 0, tzinfo=tz)
    assert eaten_at(datetime(2026, 10, 27, 23, 0, tzinfo=tz), now) == datetime(2026, 10, 28, 0, 0, tzinfo=tz)
