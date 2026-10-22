"""API 키 없이 돌리는 코드 검산. 실행: python test_core.py
LLM 도구 선택(C2~C5의 채팅 시험)은 앱에서 실제 Claude 호출로 확인한다."""
import sys
from pathlib import Path

import data_loader
import tools

sys.stdout.reconfigure(encoding="utf-8")
DATA = Path(__file__).parent / "data"
ERR = DATA / "오류"


def load(fridge=DATA / "냉장고.csv", recipes=DATA / "레시피.json"):
    f, fe = data_loader.parse_fridge(fridge.read_bytes(), fridge.name)
    r, re_ = data_loader.parse_recipes(recipes.read_bytes(), recipes.name)
    return f, r, fe + re_


fridge, recipes, errs = load()
assert not errs and len(fridge) == 12 and len(recipes) == 8, "C1-1"
k = tools.Kitchen(fridge, recipes)
before = [dict(i) for i in k.fridge]


def rows(calc):
    return {r["ingredient"]: (r["need"], r["have"], r["short"]) for r in calc["rows"]}


# C4-1 / C5-1
c2 = k.calculate_requirements("R01", 2)
assert rows(c2) == {"쌀": (200, 150, 50), "당근": (100, 120, 0), "물": (300, 1000, 0)}, rows(c2)
assert c2["shopping_list"] == [{"ingredient": "쌀", "amount": 50, "unit": "g"}]
c3 = k.calculate_requirements("당근밥", 3)
assert rows(c3) == {"쌀": (300, 150, 150), "당근": (150, 120, 30), "물": (450, 1000, 0)}, rows(c3)
# C4-3 / C4-4
c1 = k.calculate_requirements("R01", 1)
assert c1["shopping_list"] == [] and c1["verdict"] == "가능"
assert rows(k.calculate_requirements("R07", 3))["감자"] == (4.5, 2, 2.5)
# C1-2 : 냉장고에 없는 돼지고기
pork = [r for r in k.calculate_requirements("R05", 2)["rows"] if r["ingredient"] == "돼지고기"][0]
assert pork["status"] == "unregistered" and pork["have"] == 0 and "미등록" in pork["note"]

# C3-2 : 두 명 · 달걀 제외
res = k.search_recipes(2, ["달걀"])
names = [c["name"] for c in res["candidates"]]
assert len(names) <= 3 and not set(names) & {"달걀볶음밥", "두부부침", "양파달걀국"}, names
verdicts = {c["name"]: c["verdict"] for c in k.search_recipes(2, ["달걀"], False)["candidates"]}
assert k.calculate_requirements("R03", 2)["verdict"] == "가능"
# C3-4 : 있는 재료만
assert sorted(c["name"] for c in k.search_recipes(2, ["달걀"], True)["candidates"]) == ["감자조림", "채소죽"]
# C3-5 : 후보 없음
none = k.search_recipes(1, ["달걀", "감자", "쌀"], True)
assert none["candidates"] == [] and "message" in none
# C5-2 : 3명 · 달걀+당근 제외 -> 당근밥 없음
c52 = sorted(c["name"] for c in k.search_recipes(3, ["달걀", "당근"])["candidates"])
assert c52 == ["감자전", "감자조림", "김치찌개"], c52
# 계란 동의어
assert "달걀볶음밥" not in [c["name"] for c in k.search_recipes(2, ["계란"])["candidates"]]
# C4-2 : 조리 순서 원문
assert k.get_recipe("당근밥")["steps"] == recipes[0]["조리순서"]
# C2-1
assert k.get_inventory("쌀")["items"] == [{"name": "쌀", "qty": 150, "unit": "g"}]
assert len(k.list_recipes()["recipes"]) == 8

# C4-5 : 단위 불일치
f2, _, e2 = load(fridge=ERR / "냉장고_단위불일치.csv")
assert not e2
cal = tools.Kitchen(f2, recipes).calculate_requirements("R01", 2)
rice = cal["rows"][0]
assert rice["status"] == "hold" and rice["short"] is None and not cal["shopping_confirmed"]
assert rows(cal)["당근"] == (100, 120, 0) and rows(cal)["물"] == (300, 1000, 0)

# 장보기 계획: 여러 메뉴 합산 (김치찌개 2명 + 두부부침 2명 -> 두부 450 필요, 메뉴별로는 못 잡는 부족)
plan = k.plan_shopping([{"recipe_id": "R05", "servings": 2}, {"recipe_id": "두부부침", "servings": 2}])
prow = {r["ingredient"]: (r["need"], r["have"], r["short"]) for r in plan["rows"]}
assert prow["두부"] == (450, 300, 150), prow
assert prow["돼지고기"] == (150, 0, 150) and prow["식용유"] == (20, 200, 0) and prow["달걀"] == (1, 4, 0), prow
assert plan["rows"][[r["ingredient"] for r in plan["rows"]].index("두부")]["used_in"] == ["김치찌개", "두부부침"]
assert sorted((s["ingredient"], s["amount"]) for s in plan["shopping_list"]) == [("돼지고기", 150), ("두부", 150)]
assert plan["verdict"] == "부족" and plan["shopping_confirmed"] and plan["inventory_changed"] is False
# 같은 메뉴를 두 번 넣으면 합산, 재고 충분하면 장보기 없음
dup = k.plan_shopping([{"recipe_id": "R01", "servings": 1}, {"recipe_id": "R01", "servings": 1}])
assert {r["ingredient"]: r["need"] for r in dup["rows"]}["쌀"] == 200 and dup["shopping_list"] == [{"ingredient": "쌀", "amount": 50, "unit": "g", "estimated": False}]
ok = k.plan_shopping([{"recipe_id": "R02", "servings": 1}, {"recipe_id": "R03", "servings": 2}])
assert ok["shopping_list"] == [] and ok["verdict"] == "가능"
# 등록되지 않은 메뉴(AI 추정 재료량) + 등록 메뉴 합산: 비교 · 합산은 코드
pasta = [{"name": "파스타면", "qty": 200, "unit": "g"}, {"name": "양파", "qty": 2, "unit": "개"},
         {"name": "계란", "qty": 2, "unit": "개"}, {"name": "식용유", "qty": 20, "unit": "g"}]
cp = k.plan_shopping([{"custom_name": "파스타", "servings": 2, "ingredients": pasta}, {"recipe_id": "R06", "servings": 2}])
crow = {r["ingredient"]: (r["need"], r["have"], r["short"]) for r in cp["rows"]}
assert crow["파스타면"] == (200, 0, 200), crow           # 냉장고에 없는 재료
assert crow["양파"] == (3, 1, 2), crow                   # 파스타 2 + 양파달걀국 1 합산
assert crow["달걀"] == (4, 4, 0), crow                   # 계란 -> 달걀 동의어 합산(2 + 2)
assert cp["has_estimates"] and cp["dishes"][0]["source"] == "AI 추정" and cp["dishes"][1]["source"] == "등록 레시피"
assert [r for r in cp["rows"] if r["ingredient"] == "파스타면"][0]["estimated"] is True
assert [r for r in cp["rows"] if r["ingredient"] == "물"][0]["estimated"] is False
for bad in ([{"custom_name": "파스타", "servings": 2}],                                   # 재료 없음
            [{"custom_name": "파스타", "servings": 2, "ingredients": [{"name": "면", "qty": 200, "unit": "ml"}]}],
            [{"custom_name": "파스타", "recipe_id": "R01", "servings": 2, "ingredients": pasta}],  # 둘 다
            [{"servings": 2}]):                                                               # 둘 다 없음
    try:
        k.plan_shopping(bad)
        raise AssertionError(f"AI 추정 입력 {bad!r} 허용됨")
    except tools.ToolError:
        pass

# 단위 불일치 합산 보류
holdp = tools.Kitchen(f2, recipes).plan_shopping([{"recipe_id": "R01", "servings": 1}, {"recipe_id": "R08", "servings": 1}])
assert "쌀" in holdp["hold_ingredients"] and not holdp["shopping_confirmed"]
for bad in ([], None, [{"recipe_id": "R99", "servings": 1}], [{"recipe_id": "R01", "servings": 0}]):
    try:
        k.plan_shopping(bad)
        raise AssertionError(f"장보기 입력 {bad!r} 허용됨")
    except tools.ToolError:
        pass

# 인원 검증
for bad in (0, -1, 1.5, "2"):
    try:
        k.calculate_requirements("R01", bad)
        raise AssertionError(f"인원 {bad!r} 허용됨")
    except tools.ToolError:
        pass

# C1-3 ~ C1-6 : 오류 파일
f, _, e = load(fridge=ERR / "냉장고_음수.csv")
assert e and "쌀" in e[0]["내용"] and "음수" in e[0]["내용"], e
_, _, e = load(fridge=ERR / "냉장고_중복ID.csv")
assert e and "I02" in e[0]["내용"], e
_, _, e = load(recipes=ERR / "레시피_손상.json")
assert e and "JSON" in e[0]["내용"], e
_, _, e = load(recipes=ERR / "레시피_기준인원오류.json")
assert e and "R01" in e[0]["위치"] and "0" in e[0]["내용"], e

assert k.fridge == before, "C4-6 재고 불변"
print("모든 코드 검산 통과")
