"""여러 세션이 함께 쓰는 단계."""

from __future__ import annotations

from datetime import date

from kkugi_load.accounts.identity import possible_meal_uuids
from kkugi_load.client.api import Expect, KkugiApi
from kkugi_load.sessions.base import ValidationFailed

SETUP = "[setup] "


def ensure_ready(api: KkugiApi) -> None:
    """토큰이 없으면 로그인하고, 온보딩 전이면 온보딩한다. 이미 준비되었으면 요청하지 않는다."""
    state = api.state
    api.ensure_token()
    if state.onboarded:
        return
    api.call(
        "POST", "/members/me/onboarding", name=f"{SETUP}POST /members/me/onboarding",
        json_body=api.account.profile.body.onboarding_body(),
        expect=Expect(allowed={409: {"MEMBER_ALREADY_ONBOARDED"}}),
    )
    state.onboarded = True
    api.store.save(state)


def open_home(api: KkugiApi, day: str) -> dict:
    """앱을 열면 홈 화면이 부르는 조회. 하루 요약을 돌려준다."""
    summary = api.call("GET", f"/daily-summaries/{day}", name="GET /daily-summaries/{date}").data
    api.call(
        "GET", f"/daily-goals/{day}", name="GET /daily-goals/{goalDate}",
        # 코칭 계획이 아직 없으면 404이고 폰은 목표 준비 중으로 보인다
        expect=Expect(allowed={404: {"NOT_FOUND"}}),
    )
    api.call("GET", "/growth/me", name="GET /growth/me")
    return summary


def daily_summary(api: KkugiApi, day: str) -> dict:
    return api.call("GET", f"/daily-summaries/{day}", name="GET /daily-summaries/{date}").data


def search_foods(api: KkugiApi, keyword: str) -> list[dict]:
    result = api.call("GET", "/foods", name="GET /foods", params={"keyword": keyword, "page": 0, "size": 20})
    return result.data["items"]


def meals_of_day(api: KkugiApi, day: date) -> list[dict]:
    """그날 식사 목록. 발생기가 만들 수 없는 UUID가 있으면 다른 데이터가 섞인 것이다."""
    meals = api.call("GET", "/meals", name="GET /meals", params={"date": day.isoformat()}).data["meals"]
    known = possible_meal_uuids(api.email, day)
    unknown = [meal.get("clientUuid") for meal in meals if meal.get("clientUuid") not in known]
    if unknown:
        raise ValidationFailed(f"알 수 없는 식사 {unknown}")
    return meals
