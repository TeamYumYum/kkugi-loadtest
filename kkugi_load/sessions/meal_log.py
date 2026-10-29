"""식사 기록 세션. 앱을 열고, 음식을 검색해 한 끼를 저장하고, 바뀐 요약을 확인한다.

요청 순서: 요약, 목표, 성장, 식사 목록, 음식 검색(음식 수만큼), 식사 저장, 요약.

식사 clientUuid는 일정을 만들 때가 아니라 세션을 실행할 때 그 시점의 이메일로 정한다. 하루 중간에
탈퇴되어 이메일이 바뀌어도 식사 목록 검사(steps.meals_of_day)와 같은 이메일을 쓰게 하기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kkugi_load.accounts.identity import eaten_at, meal_uuid
from kkugi_load.client.api import Expect
from kkugi_load.sessions import steps
from kkugi_load.sessions.base import SessionContext, SessionSkipped, ValidationFailed

# 하루 요약의 섭취 칼로리는 정수로 반올림되어 온다. 저장 전후 차이와 식사 칼로리의 허용 오차
KCAL_TOLERANCE = 1.0


@dataclass(frozen=True)
class MealInputs:
    meal_type: str
    planned_at: datetime
    keywords: tuple[str, ...]
    fallback_keywords: tuple[str, ...]
    grams: tuple[int, ...]
    # 검색 결과에서 고를 순번의 기준값. 결과 수로 나눈 나머지를 쓴다
    picks: tuple[int, ...]


def _check_saved(saved: dict, meal: MealInputs, client_uuid: str, foods: list[dict], at: datetime) -> None:
    problems = []
    if saved.get("clientUuid") != client_uuid:
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


def run(ctx: SessionContext, meal: MealInputs) -> dict:
    """한 끼를 기록하고 검증 결과를 돌려준다. 이 끼니를 이미 기록했으면 SessionSkipped."""
    api = ctx.api
    day = meal.planned_at.date()
    client_uuid = meal_uuid(api.email, day, meal.meal_type)

    # 앱을 열면 화면이 자동으로 부르는 조회. 사이에 생각 시간을 두지 않는다
    before = steps.open_home(api, day.isoformat())
    meals = steps.meals_of_day(api, day)
    if any(m["clientUuid"] == client_uuid for m in meals):
        raise SessionSkipped(f"{meal.meal_type}을 이미 기록함")

    # 음식 검색. 사용자가 검색어를 입력하고 결과에서 고르는 시간을 둔다
    foods = []
    candidates = list(meal.keywords) + list(meal.fallback_keywords)
    for pick in meal.picks:
        while candidates:
            ctx.think(ctx.config.search_think)
            items = steps.search_foods(api, candidates.pop(0))
            if items:
                ctx.think(ctx.config.select_think)
                foods.append(items[pick % len(items)])
                break
        else:
            raise ValidationFailed("검색어 후보의 결과가 모두 비어 있음")

    at = eaten_at(meal.planned_at, ctx.now())
    body = {
        "clientUuid": client_uuid,
        "clientRevision": 1,
        "eatenAt": at.isoformat(),
        "mealType": meal.meal_type,
        "items": [{"foodId": food["id"], "grams": grams} for food, grams in zip(foods, meal.grams)],
    }
    # 200은 같은 clientUuid와 같은 내용을 다시 보낸 경우다(응답이 유실되어 재실행했을 때)
    saved = api.call("POST", "/meals", name="POST /meals", json_body=body, expect=Expect(ok=(201, 200))).data
    _check_saved(saved, meal, client_uuid, foods, at)

    # 저장 뒤 요약. 섭취 칼로리가 저장한 식사만큼 늘어야 한다
    after = steps.daily_summary(api, day.isoformat())
    meal_kcal = float(saved["totalNutrition"]["kcal"])
    delta = after["intakeKcal"] - before["intakeKcal"]
    if abs(delta - meal_kcal) > KCAL_TOLERANCE:
        raise ValidationFailed(f"요약 섭취 칼로리 증가 {delta}가 식사 칼로리 {meal_kcal}와 다름")

    return {"meal_id": saved["id"], "kcal": meal_kcal, "foods": [food["name"] for food in foods]}
