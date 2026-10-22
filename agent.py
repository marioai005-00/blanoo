"""Claude tool use 루프. 모델이 도구를 고르고, 앱 코드가 실행하며, 실제로 실행한 호출만 기록한다."""
import json
import os
from dataclasses import dataclass, field

import anthropic

import tools

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
DEFAULT_MAX_CALLS = 8
DEFAULT_TIMEOUT = 60.0
TEST_TIMEOUT_BELOW = 5.0  # 이보다 짧은 제한 시간으로 난 시간 초과는 '시험용 실패'로 표시

SYSTEM_PROMPT = """당신은 '냉장고 식단 에이전트'입니다. 사용자의 냉장고 재료와 등록된 레시피(교육용 가상 자료)만 근거로 한 끼 메뉴를 돕습니다. 한국어로 짧게 답합니다.

규칙
1. 재고 수량, 레시피, 조리 순서, 필요량, 부족량은 반드시 도구 결과만 근거로 말합니다. 기억이나 추측으로 숫자를 만들거나 직접 계산하지 않습니다.
2. 질문에 필요한 도구만 호출합니다. 재료 수량만 물으면 get_inventory, 등록 메뉴를 물으면 list_recipes만 부릅니다. 이런 조회 질문에는 인원을 묻거나 식단 계산, 메뉴 추천을 덧붙이지 않습니다.
3. 메뉴 추천 요청에 인원이 없으면 도구를 부르지 않고 몇 명이 먹는지 먼저 한 문장으로 묻습니다. 제외 재료는 선택 사항이니 따로 묻지 않습니다.
4. 추천은 search_recipes로 합니다. '있는 재료만' 요청이면 only_available=true. 후보가 없으면 없다고 알리고 조건을 바꿀지 묻습니다.
5. 특정 메뉴의 필요량 · 부족량은 calculate_requirements(recipe_id, servings)로 얻습니다. 인원이 없으면 먼저 묻습니다.
6. 조리 순서는 get_recipe의 원문 그대로 전달합니다. 재료를 임의로 빼거나 바꾼 조리법을 원래 레시피처럼 제시하지 않습니다.
7. 후속 요청은 대화 앞부분을 이어서 처리합니다. '세 명으로 바꿔줘'는 고른 메뉴를 유지하고 인원만 바꿔 calculate_requirements를 다시 부릅니다. '당근도 빼줘'처럼 조건이 늘면 이전 제외 조건에 더해 같은 인원으로 search_recipes를 다시 부릅니다. '그걸'이 가리키는 메뉴가 대화에 없으면 도구 없이 어떤 메뉴인지 묻습니다.
8. 화면에는 도구가 돌려준 후보, 계산 표, 조리 순서가 자동으로 표시됩니다. 답변은 한두 문장으로 요약하고, 숫자를 말할 때는 도구 결과 그대로 씁니다. 계산 전에는 '보유 재료만으로 가능'이라고 단정하지 말고 도구의 verdict를 따릅니다.
9. 도구가 실패하면 실패했다고 알리고 숫자를 추측하지 않습니다. 단위 불일치로 보류된 재료가 있으면 장보기를 확정할 수 없다고 알립니다.
10. 사용자가 먹고 싶은 메뉴를 직접 말하면(예: '김치찌개랑 감자전 먹고 싶어', '내일 두 명이 달걀볶음밥 먹을래') plan_shopping으로 장보기 계획을 만듭니다. 메뉴가 둘 이상이면 반드시 plan_shopping 한 번으로 합산합니다(메뉴별 부족량을 직접 더하지 않습니다). 인원이 없으면 한 번만 묻고, 인원은 모든 메뉴에 같게 적용합니다. 말한 메뉴가 등록 메뉴인지 애매하면 list_recipes로 확인합니다. 등록 메뉴는 recipe_id로 넣습니다. **등록되지 않은 메뉴(예: 파스타, 카레)도 거절하지 말고** custom_name과 ingredients(그 메뉴의 일반적인 재료와 요청 인원 전체 분량의 수량 추정치, 단위는 g 또는 개)로 같은 plan_shopping 호출에 넣어 장보기 목록을 만듭니다. 이때 수량은 당신의 추정이라 등록 레시피가 아니며 집마다 다를 수 있다고 답변에 한 문장으로 분명히 밝힙니다. 냉장고와의 비교 · 합산 · 부족량은 도구가 계산한 값만 말합니다. 사용자가 재료 구성을 말하면(예: '토마토 파스타') 그에 맞춥니다. 결과의 장보기 목록은 화면 왼쪽 '장보기 목록'에도 표시됩니다.
11. 추천과 계산은 조회일 뿐 재고를 줄이지 않습니다. 재료 제외는 선호 반영이며 알레르기 안전을 보장하지 않습니다."""


@dataclass
class ToolCall:
    name: str
    input: dict
    ok: bool
    summary: str
    result: dict | None = None
    simulated: bool = False  # 모의 시험으로 일부러 낸 실패


@dataclass
class TurnResult:
    ok: bool
    text: str = ""
    calls: list = field(default_factory=list)
    failure_kind: str = ""  # "model" | "limit" | "timeout" | "auth" | ""
    failure_message: str = ""
    test_failure: bool = False  # 시험용 실패(아주 짧은 제한 시간 등)


class _CallLimit(Exception):
    pass


def run_turn(client, kitchen, messages, user_text, *, max_calls=DEFAULT_MAX_CALLS,
             timeout=DEFAULT_TIMEOUT, simulate_tool_failure=False) -> TurnResult:
    """한 번의 사용자 요청을 처리한다. 실패하면 messages를 요청 전 상태로 되돌려 다시 시도해도 중복되지 않는다."""
    snapshot = len(messages)
    messages.append({"role": "user", "content": user_text})
    calls: list[ToolCall] = []
    api = client.with_options(timeout=timeout, max_retries=0)

    def fail(kind, message, test=False):
        del messages[snapshot:]
        return TurnResult(False, calls=calls, failure_kind=kind, failure_message=message, test_failure=test)

    try:
        while True:
            resp = api.messages.create(
                model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT, tools=tools.TOOLS,
                messages=messages, output_config={"effort": "medium"},
            )
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return fail("model", "모델이 이 요청에 답하지 않았습니다(refusal).")
            if resp.stop_reason == "max_tokens":
                return fail("model", "모델 답변이 길이 제한에서 잘렸습니다.")
            if resp.stop_reason != "tool_use":
                text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return TurnResult(True, text=text or "(모델이 빈 답변을 돌려주었습니다.)", calls=calls)

            uses = [b for b in resp.content if b.type == "tool_use"]
            if len(calls) + len(uses) > max_calls:
                raise _CallLimit
            results = []
            for use in uses:
                results.append(_execute(kitchen, use, calls, simulate_tool_failure and not calls))
            messages.append({"role": "user", "content": results})
    except _CallLimit:
        return fail("limit", f"도구 호출 상한({max_calls}회)을 넘기기 전에 중단했습니다. 재고는 바뀌지 않았습니다.")
    except anthropic.APITimeoutError:
        return fail("timeout", f"응답 제한 시간({timeout:g}초)을 넘겼습니다.", test=timeout < TEST_TIMEOUT_BELOW)
    except anthropic.AuthenticationError:
        return fail("auth", "API 키가 올바르지 않습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.")
    except anthropic.RateLimitError:
        return fail("model", "요청이 너무 많아 잠시 제한되었습니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIConnectionError:
        return fail("model", "Claude API에 연결하지 못했습니다. 네트워크를 확인하세요.")
    except anthropic.APIStatusError as e:
        return fail("model", f"Claude API 오류({e.status_code}): {e.message}")


def _execute(kitchen, use, calls, force_failure):
    args = use.input if isinstance(use.input, dict) else {}
    try:
        if force_failure:
            raise tools.ToolError("시험용 실패: 모의 시험으로 이 도구 호출을 일부러 실패시켰습니다.")
        result = tools.run_tool(kitchen, use.name, args)
    except tools.ToolError as e:
        calls.append(ToolCall(use.name, args, False, str(e), simulated=force_failure))
        return {"type": "tool_result", "tool_use_id": use.id, "is_error": True, "content": str(e)}
    calls.append(ToolCall(use.name, args, True, tools.summarize(use.name, result), result))
    return {"type": "tool_result", "tool_use_id": use.id,
            "content": json.dumps(result, ensure_ascii=False)}
