from dataclasses import replace
from datetime import date, datetime, timedelta

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import eaten_at, meal_uuid, persona_of, possible_meal_uuids
from kkugi_load.personas import PERSONAS, build_account

CONFIG = config_module.load({})
DAY = date(2026, 10, 28)


def _plan(index: int, day: date = DAY):
    account = build_account(CONFIG, index)
    return account, PERSONAS[account.persona].plan_day(CONFIG, account, day)


def test_account_is_deterministic():
    assert build_account(CONFIG, 7) == build_account(CONFIG, 7)
    assert build_account(CONFIG, 7).email == "lt-00007@synthetic.kkugi.test"
    assert build_account(CONFIG, 7) != build_account(CONFIG, 8)


def test_persona_follows_weights():
    two = replace(CONFIG, personas={"a": {"weight": 3}, "b": {"weight": 1}})
    counts = {"a": 0, "b": 0}
    for index in range(4000):
        counts[persona_of(two, index)] += 1
    assert 0.70 < counts["a"] / 4000 < 0.80
    assert persona_of(two, 5) == persona_of(two, 5)


def test_onboarding_body_is_within_server_limits():
    for index in range(200):
        body = build_account(CONFIG, index).profile.body.onboarding_body()
        assert 2 <= len(body["nickname"]) <= 12
        assert 100 <= body["heightCm"] <= 250
        assert 20 <= body["weightKg"] <= 300
        assert date.fromisoformat(body["birthDate"]).year <= date.today().year - 14


def test_day_plan_is_deterministic_and_sorted():
    account, plan = _plan(3)
    assert plan == PERSONAS[account.persona].plan_day(CONFIG, account, DAY)
    assert [p.at for p in plan] == sorted(p.at for p in plan)


def test_dieter_meals_follow_settings():
    settings = CONFIG.personas["dieter"]
    active_days = 0
    for index in range(300):
        account, plan = _plan(index)
        active_days += bool(plan)
        for session in plan:
            meal = session.inputs
            window = settings["meals"][meal.meal_type]
            assert session.name == f"dieter_{meal.meal_type.lower()}"
            assert window["from"] <= meal.planned_at.strftime("%H:%M") <= window["to"]
            assert meal.planned_at.tzinfo == CONFIG.tz
            assert meal.client_uuid == meal_uuid(account.email, DAY, meal.meal_type)
            assert len(meal.keywords) == settings["items_per_meal"]
            assert not set(meal.keywords) & set(meal.fallback_keywords)
            assert all(settings["grams_min"] <= g <= settings["grams_max"] for g in meal.grams)
            if meal.meal_type != "BREAKFAST":
                assert set(meal.keywords) <= set(account.profile.preferred_keywords)
    # 활동 확률 0.8(가정)
    assert 0.7 < active_days / 300 < 0.9


def test_meal_uuid_changes_per_day_and_type():
    email = "lt-00001@synthetic.kkugi.test"
    assert meal_uuid(email, DAY, "LUNCH") != meal_uuid(email, DAY + timedelta(days=1), "LUNCH")
    assert len(possible_meal_uuids(email, DAY)) == 4


def test_eaten_at_never_goes_past_now_or_before_midnight():
    tz = CONFIG.tz
    now = datetime(2026, 10, 28, 10, 0, 30, 123, tzinfo=tz)
    assert eaten_at(datetime(2026, 10, 28, 12, 30, tzinfo=tz), now) == now.replace(microsecond=0)
    assert eaten_at(datetime(2026, 10, 28, 8, 0, tzinfo=tz), now) == datetime(2026, 10, 28, 8, 0, tzinfo=tz)
    assert eaten_at(datetime(2026, 10, 27, 23, 0, tzinfo=tz), now) == datetime(2026, 10, 28, 0, 0, tzinfo=tz)
