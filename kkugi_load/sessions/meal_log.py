"""식사 기록 세션. 앱을 열고, 음식을 검색해 한 끼를 저장하고, 바뀐 요약을 확인한다.

요청 순서: 요약, 목표, 성장, 식사 목록, 음식 검색(음식 수만큼), 식사 저장, 요약.
수정 분기(MealInputs.misjudge가 있을 때)는 그 뒤에 식사 수정, 요약을 더 보낸다.

음식의 양은 칼로리로 정한다. 이 끼니의 칼로리 = 그날 목표 칼로리 × kcal_share이고, 음식마다 splits의 비율로 나눈 뒤
고른 음식의 100g당 칼로리로 그램을 구한다. 그래서 어떤 음식이 걸려도 하루 섭취가 목표 대비 계획한 비율에 가깝다.
양을 잘못 입력했다가 고치는 분기(misjudge)는 처음에 음식 하나를 다른 양으로 저장하고, 수정으로 의도한 양을 맞춘다.

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
# 코칭 계획이 아직 없어 그날 목표가 없을 때(GET /daily-goals 404) 쓰는 목표 칼로리. 가정
FALLBACK_TARGET_KCAL = 2000.0
# 음식 하나의 양 범위(그램). 칼로리가 아주 낮은 음식(차, 주스)이 수 킬로그램이 되지 않게 자른다. 가정
GRAMS_FLOOR = 10
GRAMS_CAP = 800


@dataclass(frozen=True)
class MealInputs:
    meal_type: str
    planned_at: datetime
    keywords: tuple[str, ...]
    fallback_keywords: tuple[str, ...]
    # 이 끼니의 칼로리를 그날 목표 칼로리의 비율로. 계획할 때 그날 기록할 끼니와 섭취 비율로 정한다
    kcal_share: float
    # 끼니 칼로리를 음식마다 나누는 비율. 합은 1이고 음식 수와 같다
    splits: tuple[float, ...]
    # 검색 결과에서 고를 순번의 기준값. 결과 수로 나눈 나머지를 쓴다
    picks: tuple[int, ...]
    # 양을 잘못 입력했다가 고치는 음식의 순번과, 처음 저장할 때 의도한 양에 곱하는 배수. None이면 고치지 않는다
    misjudge: tuple[int, float] | None = None


def portion_grams(target_kcal: float, meal: MealInputs, foods: list[dict]) -> tuple[int, ...]:
    """고른 음식마다 의도한 양(그램). 음식의 100g당 칼로리가 0 이하이면 상한을 쓴다."""
    grams = []
    for food, split in zip(foods, meal.splits):
        kcal_per_100g = float(food["nutritionPer100g"]["kcal"])
        want = target_kcal * meal.kcal_share * split
        g = GRAMS_CAP if kcal_per_100g <= 0 else round(want * 100 / kcal_per_100g)
        grams.append(min(GRAMS_CAP, max(GRAMS_FLOOR, g)))
    return tuple(grams)


def misjudged_grams(grams: tuple[int, ...], misjudge: tuple[int, float]) -> tuple[int, ...]:
    """처음 저장할 양. 음식 하나만 배수만큼 다르고, 반올림해서 같아지면 10g 늘려 실제로 다르게 한다."""
    i, factor = misjudge
    wrong = min(5000, max(1, round(grams[i] * factor)))
    if wrong == grams[i]:
        wrong += 10
    return grams[:i] + (wrong,) + grams[i + 1:]


def _check_saved(
    saved: dict, meal: MealInputs, client_uuid: str, foods: list[dict], grams: tuple[int, ...], at: datetime,
) -> None:
    problems = []
    if saved.get("clientUuid") != client_uuid:
        problems.append(f"clientUuid {saved.get('clientUuid')}")
    if saved.get("mealType") != meal.meal_type:
        problems.append(f"mealType {saved.get('mealType')}")
    if datetime.fromisoformat(saved["eatenAt"]).date() != at.date():
        problems.append(f"eatenAt {saved['eatenAt']}")
    got = [(item.get("foodId"), item.get("grams")) for item in saved.get("items", [])]
    want = [(food["id"], g) for food, g in zip(foods, grams)]
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
    before, target = steps.open_home_with_goal(api, day.isoformat())
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

    intended = portion_grams(target if target is not None else FALLBACK_TARGET_KCAL, meal, foods)
    grams = misjudged_grams(intended, meal.misjudge) if meal.misjudge is not None else intended
    at = eaten_at(meal.planned_at, ctx.now())
    body = {
        "clientUuid": client_uuid,
        "clientRevision": 1,
        "eatenAt": at.isoformat(),
        "mealType": meal.meal_type,
        "items": [{"foodId": food["id"], "grams": g} for food, g in zip(foods, grams)],
    }
    # 200은 같은 clientUuid와 같은 내용을 다시 보낸 경우다(응답이 유실되어 재실행했을 때)
    saved = api.call("POST", "/meals", name="POST /meals", json_body=body, expect=Expect(ok=(201, 200))).data
    _check_saved(saved, meal, client_uuid, foods, grams, at)

    # 저장 뒤 요약. 섭취 칼로리가 저장한 식사만큼 늘어야 한다
    after = steps.daily_summary(api, day.isoformat())
    meal_kcal = float(saved["totalNutrition"]["kcal"])
    delta = after["intakeKcal"] - before["intakeKcal"]
    if abs(delta - meal_kcal) > KCAL_TOLERANCE:
        raise ValidationFailed(f"요약 섭취 칼로리 증가 {delta}가 식사 칼로리 {meal_kcal}와 다름")

    outcome = {"meal_id": saved["id"], "kcal": meal_kcal, "foods": [food["name"] for food in foods]}
    if meal.misjudge is not None:
        outcome["edited_kcal"] = _edit(ctx, intended, saved, after, day.isoformat())
    return outcome


def _edit(ctx: SessionContext, intended: tuple[int, ...], saved: dict, before: dict, day: str) -> float:
    """저장한 식사의 양을 의도한 양으로 고치고, 요약이 바뀐 칼로리만큼 달라졌는지 확인한다. 고친 식사의 칼로리를 돌려준다."""
    api = ctx.api
    old_items = [(item["foodId"], item["grams"]) for item in saved["items"]]
    if len(intended) != len(old_items):
        raise ValueError(f"고칠 양 {intended}의 수가 음식 수 {len(old_items)}와 다름")

    # 사용자가 저장한 식사를 열어 양을 바꾸는 시간
    ctx.think(ctx.config.select_think)
    want = [(food_id, g) for (food_id, _), g in zip(old_items, intended)]
    # items는 목록 전체를 바꾼다. 시각과 끼니는 저장한 값 그대로 보낸다
    body = {
        "eatenAt": saved["eatenAt"],
        "mealType": saved["mealType"],
        "items": [{"foodId": food_id, "grams": grams} for food_id, grams in want],
    }
    # 같은 본문을 다시 보내도 결과가 같으므로, 재실행되어도 그대로 성공한다
    changed = api.call(
        "PATCH", f"/meals/{saved['id']}", name="PATCH /meals/{mealId}", json_body=body,
    ).data

    problems = []
    if changed.get("id") != saved["id"]:
        problems.append(f"id {changed.get('id')}")
    if changed.get("clientUuid") != saved["clientUuid"]:
        problems.append(f"clientUuid {changed.get('clientUuid')}")
    got = [(item.get("foodId"), item.get("grams")) for item in changed.get("items", [])]
    if got != want:
        problems.append(f"items {got} != {want}")
    if problems:
        raise ValidationFailed("수정 결과 불일치: " + ", ".join(problems))

    # 수정 뒤 요약. 섭취 칼로리가 새 칼로리와 이전 칼로리의 차이만큼 달라져야 한다
    after = steps.daily_summary(api, day)
    old_kcal = float(saved["totalNutrition"]["kcal"])
    new_kcal = float(changed["totalNutrition"]["kcal"])
    delta = after["intakeKcal"] - before["intakeKcal"]
    if abs(delta - (new_kcal - old_kcal)) > KCAL_TOLERANCE:
        raise ValidationFailed(f"수정 뒤 요약 섭취 칼로리 변화 {delta}가 식사 칼로리 변화 {new_kcal - old_kcal}와 다름")
    return new_kcal
