"""냉장고 CSV · 레시피 JSON을 읽고 검사한다. 오류는 위치와 함께 모아서 돌려주며,
오류가 있으면 빈 재고나 0으로 바꾸지 않는다(호출한 쪽이 판단 · 계산을 멈춘다)."""
import csv
import io
import json
import math

VALID_UNITS = ("g", "개")
FRIDGE_COLUMNS = ["재료ID", "이름", "수량", "단위"]


def _decode(raw: bytes, source: str):
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, {"위치": source, "내용": "UTF-8 문자 인코딩으로 읽을 수 없습니다."}


def _number(value):
    """문자열/숫자를 유한한 수로 바꾼다. 실패하면 None. 정수 값은 int로 둔다."""
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return int(num) if num == int(num) else num


def parse_fridge(raw: bytes, source: str):
    """-> (items, errors). items: [{id, name, qty, unit}]"""
    text, err = _decode(raw, source)
    if err:
        return [], [err]
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in FRIDGE_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [], [{"위치": f"{source} 1행", "내용": f"열이 없습니다: {', '.join(missing)}"}]

    items, errors, first_line = [], [], {}
    for row in reader:
        line = reader.line_num
        if not any((v or "").strip() for v in row.values()):
            continue
        item_id = (row["재료ID"] or "").strip()
        name = (row["이름"] or "").strip()
        unit = (row["단위"] or "").strip()
        where = f"{source} {line}행"
        if not item_id or not name:
            errors.append({"위치": where, "내용": "재료ID 또는 이름이 비어 있습니다."})
            continue
        if item_id in first_line:
            errors.append({"위치": where, "내용": f"재료ID {item_id}가 중복되었습니다 ({first_line[item_id]}행, {line}행)."})
            continue
        first_line[item_id] = line
        qty = _number(row["수량"])
        if qty is None:
            errors.append({"위치": where, "내용": f"{name}의 수량 '{row['수량']}'은(는) 숫자가 아닙니다."})
        elif qty < 0:
            errors.append({"위치": where, "내용": f"{name}의 수량이 음수입니다 ({qty}{unit}). 0 이상이어야 합니다."})
        if unit not in VALID_UNITS:
            errors.append({"위치": where, "내용": f"{name}의 단위 '{unit}'은(는) g 또는 개가 아닙니다."})
        if qty is not None and qty >= 0 and unit in VALID_UNITS:
            items.append({"id": item_id, "name": name, "qty": qty, "unit": unit})
    if not items and not errors:
        errors.append({"위치": source, "내용": "재료가 한 줄도 없습니다."})
    return items, errors


def parse_recipes(raw: bytes, source: str):
    """-> (recipes, errors). recipes는 레시피.json의 구조를 그대로 둔다."""
    text, err = _decode(raw, source)
    if err:
        return [], [err]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [], [{"위치": f"{source} {e.lineno}행 {e.colno}열", "내용": f"JSON을 읽을 수 없습니다 ({e.msg})."}]
    if not isinstance(data, list):
        return [], [{"위치": source, "내용": "최상위 구조가 레시피 목록(배열)이 아닙니다."}]

    errors, seen = [], set()
    for idx, rec in enumerate(data, start=1):
        if not isinstance(rec, dict):
            errors.append({"위치": f"{source} {idx}번째 항목", "내용": "레시피가 객체가 아닙니다."})
            continue
        rid, name = rec.get("레시피ID"), rec.get("이름")
        where = f"{source} {rid or f'{idx}번째 항목'}" + (f" {name}" if name else "")
        if not rid or not name:
            errors.append({"위치": where, "내용": "레시피ID 또는 이름이 없습니다."})
        elif rid in seen:
            errors.append({"위치": where, "내용": f"레시피ID {rid}가 중복되었습니다."})
        seen.add(rid)
        base = rec.get("기준인원")
        if isinstance(base, bool) or not isinstance(base, int) or base < 1:
            errors.append({"위치": where, "내용": f"기준인원이 1 이상의 정수가 아닙니다 (값: {base!r})."})
        ings = rec.get("재료")
        if not isinstance(ings, list) or not ings:
            errors.append({"위치": where, "내용": "재료 목록이 없거나 비어 있습니다."})
            ings = []
        for j, ing in enumerate(ings, start=1):
            if not isinstance(ing, dict):
                errors.append({"위치": f"{where} 재료 {j}번째", "내용": "재료가 객체가 아닙니다."})
                continue
            label = f"{where} 재료 {ing.get('이름') or j}"
            if not ing.get("재료ID") or not ing.get("이름"):
                errors.append({"위치": label, "내용": "재료ID 또는 이름이 없습니다."})
            qty = ing.get("수량")
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty < 0:
                errors.append({"위치": label, "내용": f"수량이 0 이상 숫자가 아닙니다 (값: {qty!r})."})
            if ing.get("단위") not in VALID_UNITS:
                errors.append({"위치": label, "내용": f"단위 {ing.get('단위')!r}은(는) g 또는 개가 아닙니다."})
        steps = rec.get("조리순서")
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            errors.append({"위치": where, "내용": "조리순서가 문자열 목록이 아닙니다."})
    return ([] if errors else data), errors
