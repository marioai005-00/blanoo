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
