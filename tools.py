"""에이전트가 부르는 도구. 수량 계산은 모두 여기(코드)에서 하며, 모델이 만든 수량은 쓰지 않는다.
도구는 조회 · 계산만 하고 재고(Kitchen.fridge)를 바꾸지 않는다."""
import json
from fractions import Fraction

ALIASES = {"계란": "달걀", "파": "대파"}
VERDICT_RANK = {"가능": 0, "부족": 1, "보류": 2}
MAX_CANDIDATES = 3


class ToolError(Exception):
    """도구 실패. 모델에는 is_error 결과로 전달되고 실행 기록에 실패로 남는다."""


def _frac(v):
    return Fraction(str(v))


def _num(x: Fraction):
    return int(x) if x.denominator == 1 else float(x)


def _norm(name: str) -> str:
    name = (name or "").strip()
    return ALIASES.get(name, name)


def _servings(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
        raise ToolError(f"인원(servings)은 1 이상의 정수여야 합니다 (받은 값: {value!r}).")
    return int(value)


class Kitchen:
    def __init__(self, fridge_items, recipes):
        self.fridge = list(fridge_items)
        self.recipes = list(recipes)

    # ---- 조회 헬퍼 ----
    def stock_for(self, ing):
        for it in self.fridge:
            if it["id"] == ing["재료ID"]:
                return it
        for it in self.fridge:
            if it["name"] == ing["이름"]:
                return it
        return None

    def find_recipe(self, key):
        key = str(key or "").strip()
        for r in self.recipes:
            if r["레시피ID"].lower() == key.lower():
                return r
        for r in self.recipes:
            if r["이름"] == key:
                return r
        hits = [r for r in self.recipes if key and key in r["이름"]]
        if len(hits) == 1:
            return hits[0]
        raise ToolError(f"등록된 레시피를 찾을 수 없습니다: {key!r}. list_recipes로 등록 메뉴를 확인하세요.")

    # ---- 계산 ----
    def calculate(self, recipe, servings):
        base = recipe["기준인원"]
        rows, shopping, hold = [], [], []
        for ing in recipe["재료"]:
            need = _frac(ing["수량"]) * servings / base
            stock = self.stock_for(ing)
            row = {"ingredient": ing["이름"], "재료ID": ing["재료ID"], "unit": ing["단위"], "need": _num(need)}
            if stock is None:
                row.update(have=0, short=_num(need), status="unregistered", note="미등록, 보유 0으로 비교")
            elif stock["unit"] != ing["단위"]:
                hold.append(ing["이름"])
                row.update(have=stock["qty"], short=None, status="hold", fridge_unit=stock["unit"],
                           note=f"단위 불일치(냉장고 {stock['unit']} ≠ 레시피 {ing['단위']}), 계산 보류")
            else:
                short = max(need - _frac(stock["qty"]), Fraction(0))
                row.update(have=stock["qty"], short=_num(short), status="short" if short > 0 else "ok", note="")
            if row["short"]:
                shopping.append({"ingredient": ing["이름"], "amount": row["short"], "unit": ing["단위"]})
            rows.append(row)
        verdict = "부족" if shopping else ("보류" if hold else "가능")
        return {
            "recipe_id": recipe["레시피ID"], "recipe_name": recipe["이름"],
            "servings": servings, "base_servings": base,
            "verdict": verdict, "rows": rows, "shopping_list": shopping,
            "hold_ingredients": hold, "shopping_confirmed": not hold,
            "inventory_changed": False,
        }

    # ---- 도구 ----
    def get_inventory(self, item_name=None):
        if item_name and item_name.strip():
            key = _norm(item_name)
            items = [it for it in self.fridge if key in it["name"]]
            if not items:
                return {"items": [], "message": f"냉장고에 '{item_name}' 재료가 없습니다(보유 0)."}
        else:
            items = self.fridge
        return {"items": [{"name": it["name"], "qty": it["qty"], "unit": it["unit"]} for it in items]}

    def list_recipes(self):
        return {"recipes": [{
            "recipe_id": r["레시피ID"], "name": r["이름"], "base_servings": r["기준인원"],
            "ingredients": [{"name": i["이름"], "qty": i["수량"], "unit": i["단위"]} for i in r["재료"]],
        } for r in self.recipes]}

    def get_recipe(self, recipe_id):
        r = self.find_recipe(recipe_id)
        return {
            "recipe_id": r["레시피ID"], "name": r["이름"], "base_servings": r["기준인원"],
            "ingredients": [{"name": i["이름"], "qty": i["수량"], "unit": i["단위"]} for i in r["재료"]],
            "steps": list(r["조리순서"]),
        }

    def calculate_requirements(self, recipe_id, servings):
        return self.calculate(self.find_recipe(recipe_id), _servings(servings))

    def plan_shopping(self, items):
        """먹고 싶은 메뉴 여러 개를 합산해 재료별 필요 · 보유 · 부족과 장보기 목록을 만든다.
        같은 재료는 메뉴 사이에서 합쳐서 한 번에 비교한다(메뉴별 부족량을 단순히 더하지 않는다)."""
        if not isinstance(items, list) or not items:
            raise ToolError("items에 먹고 싶은 메뉴를 {recipe_id, servings} 형태로 1개 이상 넣어야 합니다.")
        dishes, agg = [], {}
        for it in items:
            if not isinstance(it, dict):
                raise ToolError("items의 각 항목은 {recipe_id, servings} 객체여야 합니다.")
            recipe = self.find_recipe(it.get("recipe_id"))
            servings = _servings(it.get("servings"))
            dishes.append({"recipe_id": recipe["레시피ID"], "recipe_name": recipe["이름"], "servings": servings})
            for ing in recipe["재료"]:
                a = agg.setdefault(ing["재료ID"], {"ing": ing, "need": Fraction(0), "units": set(), "used_in": []})
                a["need"] += _frac(ing["수량"]) * servings / recipe["기준인원"]
                a["units"].add(ing["단위"])
                if recipe["이름"] not in a["used_in"]:
                    a["used_in"].append(recipe["이름"])
        rows, shopping, hold = [], [], []
        for a in agg.values():
            ing, unit = a["ing"], a["ing"]["단위"]
            stock = self.stock_for(ing)
            row = {"ingredient": ing["이름"], "재료ID": ing["재료ID"], "unit": unit,
                   "need": _num(a["need"]), "used_in": a["used_in"]}
            if len(a["units"]) > 1:
                hold.append(ing["이름"])
                row.update(have=stock["qty"] if stock else 0, short=None, status="hold",
                           note="메뉴마다 단위가 달라 합산 보류")
            elif stock is None:
                row.update(have=0, short=_num(a["need"]), status="unregistered", note="미등록, 보유 0으로 비교")
            elif stock["unit"] != unit:
                hold.append(ing["이름"])
                row.update(have=stock["qty"], short=None, status="hold", fridge_unit=stock["unit"],
                           note=f"단위 불일치(냉장고 {stock['unit']} ≠ 레시피 {unit}), 계산 보류")
            else:
                short = max(a["need"] - _frac(stock["qty"]), Fraction(0))
                row.update(have=stock["qty"], short=_num(short), status="short" if short > 0 else "ok", note="")
            if row["short"]:
                shopping.append({"ingredient": ing["이름"], "amount": row["short"], "unit": unit})
            rows.append(row)
        return {
            "dishes": dishes, "rows": rows, "shopping_list": shopping,
            "hold_ingredients": hold, "shopping_confirmed": not hold,
            "verdict": "부족" if shopping else ("보류" if hold else "가능"),
            "inventory_changed": False,
        }

    def search_recipes(self, servings, exclude_ingredients=None, only_available=False):
        servings = _servings(servings)
        exclude = {_norm(n) for n in (exclude_ingredients or []) if str(n).strip()}
        candidates, excluded_names = [], []
        for order, r in enumerate(self.recipes):
            if exclude & {i["이름"] for i in r["재료"]}:
                excluded_names.append(r["이름"])
                continue
            calc = self.calculate(r, servings)
            if only_available and calc["verdict"] != "가능":
                continue
            candidates.append((VERDICT_RANK[calc["verdict"]], len(calc["shopping_list"]), order, calc))
        candidates.sort(key=lambda c: c[:3])
        shown = [c[3] for c in candidates[:MAX_CANDIDATES]]
        result = {
            "conditions": {"servings": servings, "exclude_ingredients": sorted(exclude), "only_available": bool(only_available)},
            "excluded_recipes": excluded_names,
            "total_matched": len(candidates),
            "candidates": [{
                "recipe_id": c["recipe_id"], "name": c["recipe_name"], "servings": servings,
                "verdict": c["verdict"],
                "ingredients": [{"name": r["ingredient"], "need": r["need"], "unit": r["unit"]} for r in c["rows"]],
                "shortages": c["shopping_list"],
                "hold_ingredients": c["hold_ingredients"],
            } for c in shown],
            "inventory_changed": False,
        }
        if not shown:
            result["message"] = "조건에 맞는 후보가 없습니다. 조건을 바꿀지 사용자에게 물어보세요."
        return result


# ---- 모델에 알려 줄 도구 정의 ----
TOOLS = [
    {
        "name": "get_inventory",
        "description": "냉장고 재고 수량을 조회한다. item_name을 주면 그 재료만, 생략하면 전체를 돌려준다.",
        "input_schema": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "재료 이름(예: 쌀). 전체 조회면 생략."}}},
    },
    {
        "name": "list_recipes",
        "description": "등록된 모든 메뉴(레시피)의 ID, 이름, 기준인원, 재료와 분량을 돌려준다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_recipes",
        "description": ("인원과 조건에 맞는 한 끼 메뉴 후보를 최대 3개 찾는다. 제외 재료가 든 레시피는 빼고, "
                        "후보마다 코드로 계산한 판정(가능/부족/보류)과 부족 재료를 함께 돌려준다."),
        "input_schema": {"type": "object", "properties": {
            "servings": {"type": "integer", "description": "먹는 사람 수(1 이상)"},
            "exclude_ingredients": {"type": "array", "items": {"type": "string"},
                                    "description": "빼고 싶은 재료 이름들. 대화에서 이미 나온 제외 조건도 모두 포함한다."},
            "only_available": {"type": "boolean", "description": "true면 지금 재료만으로 가능한(부족량 0) 후보만"}},
            "required": ["servings"]},
    },
    {
        "name": "get_recipe",
        "description": "레시피 한 개의 상세(재료, 원문 조리 순서)를 돌려준다. 조리 순서를 물을 때 쓴다.",
        "input_schema": {"type": "object", "properties": {
            "recipe_id": {"type": "string", "description": "레시피ID(예: R01). 이름만 알면 이름도 가능."}},
            "required": ["recipe_id"]},
    },
    {
        "name": "calculate_requirements",
        "description": ("레시피와 인원으로 재료별 필요량 · 보유량 · 부족량과 장보기 목록을 계산한다. "
                        "원본 레시피와 냉장고 데이터로 코드가 계산하며 재고는 바뀌지 않는다."),
        "input_schema": {"type": "object", "properties": {
            "recipe_id": {"type": "string", "description": "레시피ID(예: R01). 이름만 알면 이름도 가능."},
            "servings": {"type": "integer", "description": "먹는 사람 수(1 이상)"}},
            "required": ["recipe_id", "servings"]},
    },
    {
        "name": "plan_shopping",
        "description": ("사용자가 먹고 싶은 메뉴(1개 이상)를 받아, 같은 재료를 합산한 재료별 필요 · 보유 · 부족과 "
                        "장보기 목록을 코드로 계산한다. 메뉴가 여러 개일 때는 이 도구로 한 번에 계산한다. 재고는 바뀌지 않는다."),
        "input_schema": {"type": "object", "properties": {
            "items": {"type": "array", "description": "먹고 싶은 메뉴 목록", "items": {
                "type": "object", "properties": {
                    "recipe_id": {"type": "string", "description": "레시피ID(예: R01). 이름만 알면 이름도 가능."},
                    "servings": {"type": "integer", "description": "그 메뉴를 먹는 사람 수(1 이상)"}},
                "required": ["recipe_id", "servings"]}}},
            "required": ["items"]},
    },
]


def run_tool(kitchen: Kitchen, name: str, args: dict):
    args = args or {}
    try:
        if name == "get_inventory":
            return kitchen.get_inventory(args.get("item_name"))
        if name == "list_recipes":
            return kitchen.list_recipes()
        if name == "search_recipes":
            return kitchen.search_recipes(args.get("servings"), args.get("exclude_ingredients"), args.get("only_available", False))
        if name == "get_recipe":
            return kitchen.get_recipe(args.get("recipe_id"))
        if name == "calculate_requirements":
            return kitchen.calculate_requirements(args.get("recipe_id"), args.get("servings"))
        if name == "plan_shopping":
            return kitchen.plan_shopping(args.get("items"))
    except ToolError:
        raise
    except Exception as e:  # 예상 밖 오류도 도구 실패로 다룬다
        raise ToolError(f"{name} 실행 중 오류: {e}") from e
    raise ToolError(f"알 수 없는 도구: {name}")


def fmt(v, unit=""):
    if v is None:
        return "-"
    return f"{v}{unit}"


def summarize(name: str, result: dict) -> str:
    """실행 기록에 보일 반환 요약."""
    if name == "get_inventory":
        items = result["items"]
        return ", ".join(f"{i['name']} {fmt(i['qty'], i['unit'])}" for i in items) or result.get("message", "결과 없음")
    if name == "list_recipes":
        return f"메뉴 {len(result['recipes'])}개: " + ", ".join(r["name"] for r in result["recipes"])
    if name == "search_recipes":
        cands = result["candidates"]
        if not cands:
            return "후보 없음"
        return f"후보 {len(cands)}개: " + ", ".join(f"{c['name']}({c['verdict']})" for c in cands)
    if name == "get_recipe":
        return f"{result['name']} 조리 순서 {len(result['steps'])}단계"
    if name == "calculate_requirements":
        shop = ", ".join(f"{s['ingredient']} {fmt(s['amount'], s['unit'])}" for s in result["shopping_list"]) or "추가 필요 없음"
        hold = f" / 보류: {', '.join(result['hold_ingredients'])}" if result["hold_ingredients"] else ""
        return f"{result['recipe_name']} {result['servings']}명 — 장보기: {shop}{hold}"
    if name == "plan_shopping":
        dishes = ", ".join(f"{d['recipe_name']} {d['servings']}명" for d in result["dishes"])
        shop = ", ".join(f"{s['ingredient']} {fmt(s['amount'], s['unit'])}" for s in result["shopping_list"]) or "추가 필요 없음"
        hold = f" / 보류: {', '.join(result['hold_ingredients'])}" if result["hold_ingredients"] else ""
        return f"{dishes} — 장보기: {shop}{hold}"
    return json.dumps(result, ensure_ascii=False)[:200]
