"""_placeholderize_tool_outputs 单测：tool 输出占位符化（幂等、10 轮保护、达标即停）。

消息轮结构（与 _build_messages 一致）：
  idx0 system / idx1 user(任务) / [assistant(tool_calls) + tool* + user(继续k)] * n_rounds
  user 总数 = 1（初始指令）+ n_rounds；tool idx = 3, 6, 9, ...（每轮 1 个，3k 模式）。
默认 protect_turns=10：总 user ≤ 10 → 全保护返回 0；否则最早 (user_total - 10) 轮可替换。
"""
from agent.generic.agent_loop import _placeholderize_tool_outputs, count_messages_tokens


def _tool(tcid: str, content: str, name: str = "") -> dict:
    m = {"role": "tool", "tool_call_id": tcid, "content": content}
    if name:
        m["name"] = name
    return m


def _assistant_with_calls(*calls) -> dict:
    """calls: (id, name) 元组列表 → OpenAI 嵌套格式 tool_calls（function.name）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": cname, "arguments": "{}"}}
            for cid, cname in calls
        ],
    }


def _build_messages(n_rounds: int, tool_names: list[str] | None = None) -> list[dict]:
    """构造 n_rounds 轮对话。tool_names 长度 = 每轮 tool 数；None = 每轮 1 个（read）。"""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "任务"}]
    for r in range(n_rounds):
        names = tool_names or ["read"]
        calls = [(f"call-{r}-{i}", n) for i, n in enumerate(names)]
        msgs.append(_assistant_with_calls(*calls))
        for i, n in enumerate(names):
            msgs.append(_tool(f"call-{r}-{i}", f"{n} 输出内容 第{r}轮 第{i}个 很长" * 50, n))
        msgs.append({"role": "user", "content": f"继续第{r + 1}轮"})
    return msgs


def test_basic_replacement_with_name():
    """基本替换：tool content 变 [read 输出已裁剪]，tool_call_id 保留，assistant.tool_calls 原样。"""
    msgs = _build_messages(3, ["read"])
    replaced = _placeholderize_tool_outputs(msgs, 1, protect_turns=1)  # 只保护最近 1 轮
    assert replaced == 3  # 3 轮全部可替换（user=4，保护 1 轮 → 最早 3 轮）
    for m in msgs:
        if m.get("role") == "tool":
            assert m["content"] == "[read 输出已裁剪]"
            assert m["tool_call_id"]  # tool_call_id 保留
    # assistant.tool_calls 原样（name/arguments 未动）
    asst = [m for m in msgs if m.get("role") == "assistant"][0]
    assert asst["tool_calls"][0]["function"]["name"] == "read"


def test_stop_when_target_reached():
    """达标即停：只替换到 token ≤ target，不裁到边界。"""
    import copy

    msgs = _build_messages(3, ["read"])
    # 精确 target：用同一 count 函数量出"替换第 1 条后"的 token 量
    probe = copy.deepcopy(msgs)
    probe[3]["content"] = "[read 输出已裁剪]"
    target = count_messages_tokens(probe)
    replaced = _placeholderize_tool_outputs(msgs, target, protect_turns=1)
    assert replaced == 1
    assert msgs[3]["content"] == "[read 输出已裁剪]"  # 最早的 tool 被替换
    assert msgs[6]["content"] != "[read 输出已裁剪]"  # 第二条未动（达标即停）


def test_protect_recent_turns():
    """默认 10 轮保护：12 轮（13 user）→ 最早 3 轮 tool 可替换，其余 9 轮保护。"""
    msgs = _build_messages(12, ["read"])
    replaced = _placeholderize_tool_outputs(msgs, 1)
    assert replaced == 3  # idx 3, 6, 9（最早 3 轮）
    assert msgs[3]["content"] == "[read 输出已裁剪]"
    assert msgs[6]["content"] == "[read 输出已裁剪]"
    assert msgs[9]["content"] == "[read 输出已裁剪]"
    assert msgs[12]["content"] != "[read 输出已裁剪]"  # 第 4 轮起保护内


def test_idempotent_skips_already_placeholderized():
    """幂等：已占位符化跳过、继续匹配下一条；全替换后再调用返回 0。

    12 轮（user=13）→ protect_start=10 → 可替换 tool idx 3,6,9。
    target=1 极低 → 替换到保护边界为止（2 条）。R1-P0-1 修正：原断言 replaced==1
    漏算 idx9 也会被替换（target=1 达不到 → 循环到 i=10 break），实际返回 2。
    """
    msgs = _build_messages(12, ["read"])
    msgs[3]["content"] = "[read 输出已裁剪]"  # 模拟第 1 轮已被替换
    replaced = _placeholderize_tool_outputs(msgs, 1)
    assert replaced == 2  # 跳过 idx3（已占位符），替换 idx6 与 idx9
    assert msgs[6]["content"] == "[read 输出已裁剪]"
    assert msgs[9]["content"] == "[read 输出已裁剪]"
    assert msgs[3]["content"] == "[read 输出已裁剪]"  # 未被二次替换
    # 全部可替换的已占位符化 → 返回 0（幂等）
    assert _placeholderize_tool_outputs(msgs, 1) == 0


def test_name_fallback_from_assistant_calls():
    """无 name 字段时回退：从 assistant.tool_calls 按 tool_call_id 匹配 function.name（OpenAI 嵌套）。"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        _assistant_with_calls(("call-0", "grep")),
        {"role": "tool", "tool_call_id": "call-0", "content": "grep 结果很长" * 100},  # 无 name 字段
        {"role": "user", "content": "继续"},
    ]
    replaced = _placeholderize_tool_outputs(msgs, 1, protect_turns=1)
    assert replaced == 1
    assert msgs[3]["content"] == "[grep 输出已裁剪]"


def test_name_match_failure_unnamed_placeholder():
    """tool_call_id 匹配失败 → 无名占位符 [输出已裁剪]。"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        _assistant_with_calls(("call-0", "read")),
        {"role": "tool", "tool_call_id": "call-unknown", "content": "孤儿 tool 输出很长" * 100},
        {"role": "user", "content": "继续"},
    ]
    replaced = _placeholderize_tool_outputs(msgs, 1, protect_turns=1)
    assert replaced == 1
    assert msgs[3]["content"] == "[输出已裁剪]"


def test_no_tool_messages_returns_zero():
    """无 tool 消息 → 返回 0。"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "纯文本回复"},
        {"role": "user", "content": "继续"},
    ]
    assert _placeholderize_tool_outputs(msgs, 1) == 0


def test_all_rounds_protected_returns_zero():
    """总 user 数 ≤ 10（全在保护内）→ 返回 0，任何 tool 不动。"""
    msgs = _build_messages(8, ["read"])  # user=9 ≤ 10 → 全保护
    assert _placeholderize_tool_outputs(msgs, 1) == 0
    assert all(m["content"] != "[read 输出已裁剪]" for m in msgs if m.get("role") == "tool")


def test_eleven_users_protects_exactly_ten():
    """保护边界：恰好 11 user（10 轮 + 初始指令）→ 最早 1 轮可替换（R1-A P2 补充）。"""
    msgs = _build_messages(10, ["read"])  # user = 1 + 10 = 11 > 10 → 最早 1 轮可替换
    replaced = _placeholderize_tool_outputs(msgs, 1)
    assert replaced == 1
    assert msgs[3]["content"] == "[read 输出已裁剪]"  # 最早轮 tool
    assert msgs[6]["content"] != "[read 输出已裁剪]"  # 第 2 轮起保护


def test_multiple_tools_same_round():
    """一轮多个 tool：总 user ≤ 10 全保护；12 轮时最早 3 轮全部按顺序替换。"""
    msgs = _build_messages(2, ["read", "grep", "write"])  # user=3 ≤ 10 → 全保护
    assert _placeholderize_tool_outputs(msgs, 1) == 0
    msgs12 = _build_messages(12, ["read", "grep", "write"])  # user=13 → 最早 3 轮（9 条）可替换
    replaced = _placeholderize_tool_outputs(msgs12, 1)
    assert replaced == 9
    round1_tools = [m for m in msgs12[2:6] if m.get("role") == "tool"]  # 最早轮 3 条
    assert [m["content"] for m in round1_tools] == [
        "[read 输出已裁剪]", "[grep 输出已裁剪]", "[write 输出已裁剪]",
    ]
