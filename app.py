import csv
import io
import json
import os
from pathlib import Path

import anthropic
import streamlit as st
from dotenv import load_dotenv

import agent
import data_loader
import tools

BASE = Path(__file__).parent
DATA = BASE / "data"
ERR_DIR = DATA / "오류"
load_dotenv(BASE / ".env", override=True)  # 키를 바꾼 뒤 새로고침만 해도 반영된다

st.set_page_config(page_title="냉장고 식단 에이전트", page_icon="🍚", layout="wide")


# ---------------------------------------------------------------- 파일 선택
def pick_source(label, default_name, pattern, key):
    options = [f"기본 파일 ({default_name})"] + [f"오류/{p.name}" for p in sorted(ERR_DIR.glob(pattern))] + ["파일 업로드"]
    choice = st.sidebar.selectbox(label, options, key=key)
    if choice == options[0]:
        return (DATA / default_name).read_bytes(), default_name
    if choice == "파일 업로드":
        up = st.sidebar.file_uploader(f"{label} 업로드", type=[pattern.split(".")[-1]], key=key + "_up")
        return (up.getvalue(), up.name) if up else (None, "업로드 대기")
    name = choice.split("/", 1)[1]
    return (ERR_DIR / name).read_bytes(), name


st.sidebar.header("데이터 파일")
fridge_raw, fridge_name = pick_source("냉장고 CSV", "냉장고.csv", "*.csv", "src_fridge")
recipe_raw, recipe_name = pick_source("레시피 JSON", "레시피.json", "*.json", "src_recipe")

errors = []
fridge, recipes = [], []
if fridge_raw is None or recipe_raw is None:
    errors.append({"위치": "파일 선택", "내용": "교체할 파일을 업로드하세요."})
else:
    fridge, e1 = data_loader.parse_fridge(fridge_raw, fridge_name)
    recipes, e2 = data_loader.parse_recipes(recipe_raw, recipe_name)
    errors = e1 + e2
kitchen = tools.Kitchen([] if errors else fridge, [] if errors else recipes)

# ---------------------------------------------------------------- 상태
ss = st.session_state
ss.setdefault("messages", [])  # API로 보내는 대화
ss.setdefault("display", [])  # 화면용 기록
signature = (fridge_name, len(fridge_raw or b""), recipe_name, len(recipe_raw or b""))
if ss.get("signature") != signature:  # 파일을 바꾸면 이전 대화의 근거가 달라지므로 대화를 비운다
    ss.signature = signature
    ss.messages, ss.display = [], []

st.sidebar.header("대화")
if st.sidebar.button("새 대화", use_container_width=True):
    ss.messages, ss.display = [], []
    st.rerun()

with st.sidebar.expander("설정 · 모의 시험"):
    timeout = st.number_input("응답 제한 시간(초)", min_value=0.001, max_value=300.0,
                              value=agent.DEFAULT_TIMEOUT, format="%.3f")
    max_calls = st.number_input("도구 호출 상한(요청당)", min_value=1, max_value=30, value=agent.DEFAULT_MAX_CALLS)
    sim_fail = st.checkbox("모의 시험: 첫 도구 호출을 일부러 실패시키기")
    st.caption("제한 시간을 아주 짧게 하거나 위 옵션을 켜면 실패 처리를 시험할 수 있습니다. "
               "이때 나는 실패는 기록에 '시험용 실패' · '모의 시험'으로 표시됩니다.")

def read_api_key():
    """로컬은 .env(환경변수), 배포(Streamlit Cloud)는 Secrets에서 키를 읽는다. 값은 화면에 내지 않는다."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        return str(st.secrets.get("ANTHROPIC_API_KEY", "")).strip()
    except Exception:  # secrets 파일이 없는 로컬 실행
        return ""


api_key = read_api_key()


# ---------------------------------------------------------------- 표시 도구
def n(v, unit=""):
    return "-" if v is None else f"{v}{unit}"


def render_inventory_and_recipes():
    with st.expander("보유 재료 표", expanded=False):
        st.table([{"재료ID": i["id"], "재료": i["name"], "보유량": i["qty"], "단위": i["unit"]} for i in fridge])
    with st.expander("등록 메뉴 목록 (재료와 분량 포함)", expanded=False):
        for r in recipes:
            st.markdown(f"**{r['레시피ID']} {r['이름']}** — 기준 {r['기준인원']}명")
            rows = []
            for ing in r["재료"]:
                stock = kitchen.stock_for(ing)
                rows.append({"재료": ing["이름"], "분량": n(ing["수량"], ing["단위"]),
                             "비고": "미등록, 보유 0으로 비교" if stock is None else ""})
            st.table(rows)


def render_calc(c, label, entry_key):
    st.markdown(f"##### {label} — {c['recipe_name']} {c['servings']}명 기준 (레시피 기준 {c['base_servings']}명)")
    st.table([{"재료": r["ingredient"],
               "필요": n(r["need"], r["unit"]),
               "보유": n(r["have"], r.get("fridge_unit", r["unit"])),
               "부족": n(r["short"], r["unit"]) if r["short"] is not None else "계산 보류",
               "비고": r["note"]} for r in c["rows"]])
    if c["shopping_list"]:
        st.markdown("**장보기 목록:** " + ", ".join(f"{s['ingredient']} {n(s['amount'], s['unit'])}" for s in c["shopping_list"]))
    elif not c["hold_ingredients"]:
        st.markdown("**추가로 필요한 재료가 없습니다.**")
    if c["hold_ingredients"]:
        st.warning(f"단위가 달라 계산을 보류한 재료: {', '.join(c['hold_ingredients'])}. 장보기 목록을 확정할 수 없습니다.")
    st.caption("현재 재고는 변경되지 않습니다.")
    if c["shopping_list"] and label.startswith("최신"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["재료", "부족량", "단위"])
        w.writerows([[s["ingredient"], s["amount"], s["unit"]] for s in c["shopping_list"]])
        st.download_button("장보기 목록 CSV", buf.getvalue().encode("utf-8-sig"), "장보기목록.csv", "text/csv",
                           key=f"dl_{entry_key}")


def render_plan(p, label):
    dishes = " · ".join(f"{d['recipe_name']}{' (AI 추정)' if d.get('source') == 'AI 추정' else ''} {d['servings']}명"
                        for d in p["dishes"])
    st.markdown(f"##### {label} — {dishes}")
    for d in p["dishes"]:
        if d.get("source") == "AI 추정":
            with st.expander(f"AI가 추정한 '{d['recipe_name']}' 재료량 ({d['servings']}명 분량, 등록 레시피 아님)"):
                st.table([{"재료": i["name"], "추정 수량": n(i["qty"], i["unit"])} for i in d["ingredients"]])
    st.table([{"재료": r["ingredient"],
               "필요(합산)": n(r["need"], r["unit"]),
               "보유": n(r["have"], r.get("fridge_unit", r["unit"])),
               "부족": n(r["short"], r["unit"]) if r["short"] is not None else "계산 보류",
               "쓰이는 메뉴": ", ".join(r["used_in"]),
               "비고": r["note"]} for r in p["rows"]])
    if p["shopping_list"]:
        st.markdown("**장보기 목록:** " + ", ".join(f"{s['ingredient']} {n(s['amount'], s['unit'])}" for s in p["shopping_list"]))
    elif not p["hold_ingredients"]:
        st.markdown("**추가로 필요한 재료가 없습니다.** 지금 재료로 모두 만들 수 있습니다.")
    if p["hold_ingredients"]:
        st.warning(f"단위가 달라 계산을 보류한 재료: {', '.join(p['hold_ingredients'])}. 장보기 목록을 확정할 수 없습니다.")
    if p.get("has_estimates"):
        st.info("'AI 추정' 메뉴의 재료량은 등록된 레시피가 아니라 AI가 일반적인 양으로 추정한 값입니다. "
                "집마다 다를 수 있으니 참고용으로 보세요. 냉장고와의 비교 · 합산 계산은 코드가 했습니다.")
    st.caption("같은 재료는 메뉴 사이에서 합산해 비교했습니다. 현재 재고는 변경되지 않습니다.")


def render_cart(src):
    """사이드바 장보기 목록: 가장 최근 계산(메뉴 계산 또는 장보기 계획)의 부족 재료."""
    st.sidebar.header("🛒 장보기 목록")
    if src is None:
        st.sidebar.caption("메뉴를 정하거나 먹고 싶은 메뉴를 말하면 여기에 장보기 목록이 생깁니다.")
        return
    key, result = src
    if result.get("dishes"):
        title = " · ".join(f"{d['recipe_name']} {d['servings']}명" for d in result["dishes"])
    else:
        title = f"{result['recipe_name']} {result['servings']}명"
    st.sidebar.caption(title)
    if result["shopping_list"]:
        for s in result["shopping_list"]:
            est = " (AI 추정)" if s.get("estimated") else ""
            st.sidebar.checkbox(f"{s['ingredient']} {n(s['amount'], s['unit'])}{est}", key=f"buy_{key}_{s['ingredient']}")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["재료", "부족량", "단위"])
        w.writerows([[s["ingredient"], s["amount"], s["unit"]] for s in result["shopping_list"]])
        st.sidebar.download_button("장보기 목록 CSV", buf.getvalue().encode("utf-8-sig"), "장보기목록.csv",
                                   "text/csv", key=f"cart_dl_{key}")
    elif not result["hold_ingredients"]:
        st.sidebar.success("추가로 필요한 재료가 없습니다.")
    if result["hold_ingredients"]:
        st.sidebar.warning(f"계산 보류: {', '.join(result['hold_ingredients'])} (단위 불일치) — 장보기를 확정할 수 없습니다.")


def render_candidates(res, entry_key):
    cond = res["conditions"]
    parts = [f"{cond['servings']}명"]
    if cond["exclude_ingredients"]:
        parts.append("제외: " + ", ".join(cond["exclude_ingredients"]))
    if cond["only_available"]:
        parts.append("있는 재료만")
    st.markdown("##### 메뉴 후보 — " + " · ".join(parts))
    if not res["candidates"]:
        st.info("조건에 맞는 후보가 없습니다.")
        return
    st.table([{"메뉴": c["name"], "인원": f"{c['servings']}명",
               "재료(필요량)": ", ".join(f"{i['name']} {n(i['need'], i['unit'])}" for i in c["ingredients"]),
               "판정": c["verdict"] + (
                   " — 부족: " + ", ".join(f"{s['ingredient']} {n(s['amount'], s['unit'])}" for s in c["shortages"])
                   if c["shortages"] else "")} for c in res["candidates"]])
    cols = st.columns(len(res["candidates"]))
    for col, c in zip(cols, res["candidates"]):
        if col.button(f"{c['name']} 선택", key=f"pick_{entry_key}_{c['recipe_id']}"):
            ss.pending = f"{c['name']}(으)로 할게. {c['servings']}명 분량으로 알려줘."
            st.rerun()


def render_steps(res):
    st.markdown(f"##### {res['name']} 조리 순서 (레시피 원문)")
    for i, s in enumerate(res["steps"], 1):
        st.markdown(f"{i}. {s}")


def render_log(calls, note=""):
    if not calls and not note:
        return
    with st.expander(f"도구 실행 기록 ({len(calls)}건)"):
        if note:
            st.caption(note)
        if not calls:
            st.caption("실행한 도구가 없습니다.")
        for c in calls:
            mark = "✅ 성공" if c.ok else "❌ 실패"
            sim = " · **모의 시험**" if c.simulated else ""
            st.markdown(f"**{c.name}** — {mark}{sim}  \n인자: `{json.dumps(c.input, ensure_ascii=False)}`  \n반환 요약: {c.summary}")


def render_artifacts(calls, entry_key, latest_key, latest_plan_key):
    for ci, c in enumerate(calls):
        if not c.ok or c.result is None:
            continue
        k = f"{entry_key}_{ci}"
        if c.name == "search_recipes":
            render_candidates(c.result, k)
        elif c.name == "get_recipe":
            render_steps(c.result)
        elif c.name == "calculate_requirements":
            render_calc(c.result, "최신 결과" if k == latest_key else "이전 결과", k)
        elif c.name == "plan_shopping":
            render_plan(c.result, "최신 장보기 계획" if k == latest_plan_key else "이전 장보기 계획")


# ---------------------------------------------------------------- 화면
st.title("🍚 냉장고 식단 에이전트")
st.caption("냉장고 재료로 한 끼 메뉴를 찾고, 인원에 맞춰 필요량과 부족량을 계산합니다. "
           "교육용 가상 자료이며 조리 순서는 예시입니다. 식품 안전·영양 적합성을 검증하지 않았고, "
           "재료 제외는 선호 반영일 뿐 알레르기 안전을 보장하지 않습니다.")

blocked = False
if errors:
    blocked = True
    st.error("데이터 파일에 문제가 있어 메뉴 판단과 계산을 멈췄습니다. 재고를 비우거나 0으로 바꾸지 않았습니다. "
             "사이드바에서 정상 파일(기본 파일)로 바꾸면 다시 사용할 수 있습니다.")
    st.table(errors)
else:
    st.success(f"재료 {len(fridge)}종 · 메뉴 {len(recipes)}개를 불러왔습니다.")
    render_inventory_and_recipes()

if not api_key:
    blocked = True
    st.warning("Claude API 키가 설정되지 않았습니다. 내 컴퓨터에서 실행 중이면 프로젝트 폴더의 `.env` 파일에 "
               "`ANTHROPIC_API_KEY=...` 를 넣고, Streamlit Cloud에 배포했다면 앱 설정의 Secrets에 "
               "`ANTHROPIC_API_KEY = \"...\"` 를 저장한 뒤 이 페이지를 새로고침하세요. "
               "키를 설정하기 전에는 질문을 보낼 수 없습니다.")

# 재시도 / 후보 선택으로 들어온 요청
pending = ss.pop("pending", None)
retry = ss.pop("retry", False)
if retry and ss.display and ss.display[-1]["kind"] == "error":
    ss.display.pop()
    pending = next(e["text"] for e in reversed(ss.display) if e["kind"] == "user")
elif retry:
    retry = False

# 가장 마지막 계산 결과를 '최신'으로 표시
latest_key = latest_plan_key = None
cart_src = None  # 사이드바 장보기 목록의 근거: 가장 최근의 메뉴 계산 또는 장보기 계획
for ei, e in enumerate(ss.display):
    for ci, c in enumerate(e.get("calls", [])):
        if c.ok and c.name == "calculate_requirements":
            latest_key = f"{ei}_{ci}"
            cart_src = (latest_key, c.result)
        elif c.ok and c.name == "plan_shopping":
            latest_plan_key = f"{ei}_{ci}"
            cart_src = (latest_plan_key, c.result)
render_cart(cart_src)

for ei, e in enumerate(ss.display):
    if e["kind"] == "user":
        with st.chat_message("user"):
            st.write(e["text"])
    else:
        with st.chat_message("assistant"):
            if e["kind"] == "assistant":
                st.write(e["text"])
                failed = [c for c in e["calls"] if not c.ok]
                if failed:
                    st.warning(f"도구 실패 {len(failed)}건이 있었습니다. 실패한 호출의 결과는 사용하지 않았습니다.")
                render_artifacts(e["calls"], ei, latest_key, latest_plan_key)
            else:
                label = {"model": "모델 호출 실패", "timeout": "모델 호출 실패(시간 초과)", "limit": "도구 호출 상한 도달",
                         "auth": "인증 실패"}[e["failure_kind"]]
                st.error(f"{label}: {e['message']}")
                if e["test_failure"]:
                    st.caption("🧪 시험용 실패 — 제한 시간을 아주 짧게 설정해 일부러 낸 실패입니다. 실제 응답이 아닙니다.")
                if ei == len(ss.display) - 1 and st.button("다시 시도", key=f"retry_{ei}"):
                    ss.retry = True
                    st.rerun()
            render_log(e["calls"], e.get("note", ""))

typed = st.chat_input("예: 두 명이 먹을 저녁 메뉴 추천해줘. 달걀은 빼줘. / 김치찌개랑 감자전 먹고 싶어, 두 명이야.", disabled=blocked)
if typed:
    pending = typed

if pending and not blocked:
    if not retry:
        ss.display.append({"kind": "user", "text": pending})
        with st.chat_message("user"):
            st.write(pending)
    client = anthropic.Anthropic(api_key=api_key)
    with st.chat_message("assistant"), st.spinner("Claude가 도구를 고르는 중..."):
        res = agent.run_turn(client, kitchen, ss.messages, pending, max_calls=int(max_calls),
                             timeout=float(timeout), simulate_tool_failure=sim_fail)
    note = "모의 시험으로 첫 도구 호출을 실패시켰습니다." if any(c.simulated for c in res.calls) else ""
    if res.ok:
        ss.display.append({"kind": "assistant", "text": res.text, "calls": res.calls, "note": note})
    else:
        ss.display.append({"kind": "error", "failure_kind": res.failure_kind, "message": res.failure_message,
                           "test_failure": res.test_failure, "calls": res.calls, "note": note})
    st.rerun()
