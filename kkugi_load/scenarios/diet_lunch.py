"""다이어터의 점심 기록 세션.

아침 한 끼(음식 두 개)를 기록한 계정이 음식 두 개를 검색해 점심을 저장하고, 바뀐 요약을 확인한다.
계정 준비(로그인, 온보딩, 당일 아침 기록)는 측정 세션과 통계 이름을 나눈다([setup] 접두어).
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Callable

from kkugi_load.accounts import DayPlan, MealPlan, eaten_at
from kkugi_load.api import Expect, KkugiApi
from kkugi_load.config import Config

SETUP = "[setup] "
# 하루 요약의 섭취 칼로리는 정수로 반올림되어 온다. 저장 전후 차이와 식사 칼로리의 허용 오차
KCAL_TOLERANCE = 1.0


class SessionSkipped(Exception):
    """세션을 실행할 필요가 없다. 실패가 아니다."""


class ValidationFailed(Exception):
    """HTTP 응답은 정상이지만 업무 결과가 기대와 다르다."""


def _search(api: KkugiApi, keyword: str, name_prefix: str) -> list[dict]:
    result = api.call(
        "GET", "/foods", name=f"{name_prefix}GET /foods", params={"keyword": keyword, "page": 0, "size": 20},
    )
    return result.data["items"]


def _pick(items: list[dict], pick: int) -> dict:
    return items[pick % len(items)]


def _meals_of_day(api: KkugiApi, day: str, name_prefix: str) -> list[dict]:
    return api.call("GET", "/meals", name=f"{name_prefix}GET /meals", params={"date": day}).data["meals"]


def _save_meal(api: KkugiApi, meal: MealPlan, foods: list[dict], at: datetime, name: str) -> dict:
    body = {
        "clientUuid": meal.client_uuid,
        "clientRevision": 1,
        "eatenAt": at.isoformat(),
        "mealType": meal.meal_type,
        "items": [{"foodId": food["id"], "grams": grams} for food, grams in zip(foods, meal.grams)],
    }
    # 200은 같은 clientUuid와 같은 내용을 다시 보낸 경우다(응답이 유실되어 재실행했을 때)
    saved = api.call("POST", "/meals", name=name, json_body=body, expect=Expect(ok=(201, 200))).data
    _check_saved(saved, meal, foods, at)
    return saved


def _check_saved(saved: dict, meal: MealPlan, foods: list[dict], at: datetime) -> None:
    problems = []
    if saved.get("clientUuid") != meal.client_uuid:
        problems.append(f"clientUuid {saved.get('clientUuid')}")
    if saved.get("mealType") != meal.meal_type:
        problems.append(f"mealType {saved.get('mealType')}")
    if datetime.fromisoformat(saved["eatenAt"]).date() != at.date():
        problems.append(f"eatenAt {saved['eatenAt']}")
    got = [(item.get("foodId"), item.get("grams")) for item in saved.get("items", [])]
    want = [(food["id"], grams) for food, grams in zip(foods, meal.grams)]
    if got != want:
        problems.append(f"items {got} != {want}")
    if problems:
        raise ValidationFailed("저장 결과 불일치: " + ", ".join(problems))


def _check_own_meals(meals: list[dict], plan: DayPlan) -> None:
    """인공 계정의 식사는 발생기만 쓴다. 모르는 UUID가 있으면 다른 데이터가 섞인 것이다."""
    known = {plan.breakfast.client_uuid, plan.lunch.client_uuid}
    unknown = [meal.get("clientUuid") for meal in meals if meal.get("clientUuid") not in known]
    if unknown:
        raise ValidationFailed(f"알 수 없는 식사 {unknown}")


def prepare(api: KkugiApi, plan: DayPlan, now: datetime) -> None:
    """측정 세션 전에 계정을 준비한다. 이미 준비되었으면 요청하지 않는다."""
    state = api.state
    api.ensure_token()
    if not state.onboarded:
        api.call(
            "POST", "/members/me/onboarding", name=f"{SETUP}POST /members/me/onboarding",
            json_body=api.account.profile.onboarding_body(),
            expect=Expect(allowed={409: {"MEMBER_ALREADY_ONBOARDED"}}),
        )
        state.onboarded = True
        api.store.save(state)

    day = plan.day.isoformat()
    if state.breakfast_ready_on == day:
        return
    meals = _meals_of_day(api, day, SETUP)
    _check_own_meals(meals, plan)
    if not any(meal["clientUuid"] == plan.breakfast.client_uuid for meal in meals):
        foods = []
        for keyword, pick in zip(plan.breakfast.keywords, plan.breakfast.picks):
            items = _search(api, keyword, SETUP)
            if not items:
                raise ValidationFailed(f"아침 검색 결과 없음: {keyword}")
            foods.append(_pick(items, pick))
        _save_meal(api, plan.breakfast, foods, eaten_at(plan.breakfast.planned_at, now), f"{SETUP}POST /meals")
    state.breakfast_ready_on = day
    api.store.save(state)


def run(
    api: KkugiApi, config: Config, plan: DayPlan, now: Callable[[], datetime], sleep: Callable[[float], None],
    rng: random.Random,
) -> dict:
    """점심 기록 세션 8단계를 실행하고 검증 결과를 돌려준다. 이미 점심을 기록했으면 SessionSkipped."""
    day = plan.day.isoformat()
    lunch = plan.lunch

    def think(bounds: tuple[float, float]) -> None:
        sleep(rng.uniform(*bounds))

    # 1-4. 앱을 열면 화면이 자동으로 부르는 조회. 사이에 생각 시간을 두지 않는다
    before = api.call("GET", f"/daily-summaries/{day}", name="GET /daily-summaries/{date}").data
    api.call(
        "GET", f"/daily-goals/{day}", name="GET /daily-goals/{goalDate}",
        # 코칭 계획이 아직 없으면 404이고 폰은 목표 준비 중으로 보인다
        expect=Expect(allowed={404: {"NOT_FOUND"}}),
    )
    api.call("GET", "/growth/me", name="GET /growth/me")
    meals = _meals_of_day(api, day, "")
    _check_own_meals(meals, plan)
    if not any(meal["clientUuid"] == plan.breakfast.client_uuid for meal in meals):
        raise ValidationFailed("준비한 아침 기록이 목록에 없음")
    if any(meal["clientUuid"] == lunch.client_uuid for meal in meals):
        raise SessionSkipped("오늘 점심을 이미 기록함")

    # 5-6. 음식 검색. 사용자가 검색어를 입력하고 결과에서 고르는 시간을 둔다
    foods = []
    candidates = list(lunch.keywords) + [
        k for k in api.account.profile.preferred_lunch_keywords if k not in lunch.keywords
    ]
    for pick in lunch.picks:
        while candidates:
            keyword = candidates.pop(0)
            think(config.search_think)
            items = _search(api, keyword, "")
            if items:
                think(config.select_think)
                foods.append(_pick(items, pick))
                break
        else:
            raise ValidationFailed("선호 음식 검색 결과가 모두 비어 있음")

    # 7. 점심 저장
    at = eaten_at(lunch.planned_at, now())
    saved = _save_meal(api, lunch, foods, at, "POST /meals")

    # 8. 저장 뒤 요약. 섭취 칼로리가 저장한 식사만큼 늘어야 한다
    after = api.call("GET", f"/daily-summaries/{day}", name="GET /daily-summaries/{date}").data
    meal_kcal = float(saved["totalNutrition"]["kcal"])
    delta = after["intakeKcal"] - before["intakeKcal"]
    if abs(delta - meal_kcal) > KCAL_TOLERANCE:
        raise ValidationFailed(f"요약 섭취 칼로리 증가 {delta}가 식사 칼로리 {meal_kcal}와 다름")

    return {"meal_id": saved["id"], "kcal": meal_kcal, "foods": [food["name"] for food in foods]}
