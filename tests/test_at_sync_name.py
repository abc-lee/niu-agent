"""@ 通道同步名兼容 + 任意位置防护 + orphan 反馈。"""
from agent.at_message_parser import extract_at_messages, strip_at_messages


def test_extract_sync_name_no_hex():
    """同步子 Agent 名（无 -4hex）可提取。"""
    reply = "哈哈它叫我老板了。\n\n@nutritionist 你好，先告诉你用户情况：男性30岁"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "nutritionist"
    assert "男性30岁" in msgs[0]["content"]


def test_extract_async_name_with_hex():
    """异步子 Agent 名（-4hex）仍可提取（不回归）。"""
    reply = "@file-processor-a1b2 继续处理"
    msgs = extract_at_messages(reply)
    assert msgs[0]["target"] == "file-processor-a1b2"


def test_extract_multiple_mixed():
    """多个 @ 混合（同步+异步）全部提取。"""
    reply = "@a 任务1\n@b-c1d2 任务2"
    msgs = extract_at_messages(reply)
    assert [m["target"] for m in msgs] == ["a", "b-c1d2"]


def test_strip_sync_name():
    """strip 移除 @ 同步名消息，保留其余文本。"""
    # R4-B P1：新正则 content 懒匹配到串尾——"@nutritionist 你好\n\n以上。" 尾随文本被整体剥除
    # （散文 @ 误伤取舍，R3-A P3-6 已文档化）——断言对齐行为："以上" 不在 stripped
    reply = "先说结论。\n\n@nutritionist 你好\n\n以上。"
    stripped = strip_at_messages(reply)
    assert "先说结论" in stripped
    assert "以上" not in stripped  # 被 @ 消息 content 吞掉（文档化行为）
    assert "@nutritionist" not in stripped


def test_extract_excludes_reserved_markers():
    """保留标记（@end/@niu-agent/@user/@主Agent）不提取（负向前瞻）。"""
    reply = "子Agent 用 @end 结束了任务，它通过 @niu-agent 向你提问，还问了 @user 想要什么"
    assert extract_at_messages(reply) == []
    # 但正常 @子Agent 仍提取
    reply2 = "@nutritionist 你好，继续。它用 @end 结束了"
    msgs = extract_at_messages(reply2)
    assert len(msgs) == 1 and msgs[0]["target"] == "nutritionist"


def test_extract_digit_agent_name():
    """含数字的 agent 名（对齐 _KEBAB_CASE_RE）可提取。"""
    msgs = extract_at_messages("@my-agent-2 继续处理")
    assert msgs[0]["target"] == "my-agent-2"


def test_extract_punctuation_adjacent():
    """标点紧跟（中文 LLM 高发格式）：@nutritionist，你好 / @nutritionist。你吃饭了吗。"""
    msgs = extract_at_messages("@nutritionist，你好，请继续")
    assert msgs and msgs[0]["target"] == "nutritionist"
    msgs2 = extract_at_messages("哈哈它叫我老板。@nutritionist。你吃饭了吗")
    assert msgs2 and msgs2[0]["target"] == "nutritionist"


def test_check_main_agent_mid_content_at(monkeypatch):
    """content 中间 @ 同步挂起子名 → 拦截（FORMAT_ERROR 语义）。"""
    from agent.generic.agent_loop import _check_main_agent_content_reply_to_suspended
    from agent.subagent_registry import SubagentRegistry, RunningSubagent

    inst = RunningSubagent(
        unique_name="nutritionist", agent_type="nutritionist",
        supplement_queue=object(), state="waiting_for_answer", is_sync=True,
    )
    monkeypatch.setattr(SubagentRegistry, "get", staticmethod(lambda name: inst if name == "nutritionist" else None))
    content = "哈哈它叫我老板了。\n\n@nutritionist 你好，先告诉你用户情况"
    assert _check_main_agent_content_reply_to_suspended(content, []) is True


def test_route_orphan_main_agent_forwarded(monkeypatch):
    """orphan（sender=主Agent）→ MainAgentRequestQueue push 推回主 Agent（不静默）。"""
    from agent import route_to_subagent
    from agent.subagent_registry import SubagentRegistry

    pushed = []
    monkeypatch.setattr(SubagentRegistry, "get", staticmethod(lambda name: None))
    # R4-A/B P1：生产代码函数内 `from agent.main_agent_request_queue import get_main_agent_request_queue`
    # ——函数内 import 从**源模块**取属性，正确 patch 目标是源模块（patch route_to_subagent 同名属性无效）
    import agent.main_agent_request_queue as q_mod
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue",
                        staticmethod(lambda: type("_Q", (), {"push": staticmethod(lambda c: pushed.append(c))})()))
    result = route_to_subagent.route_to_subagent("nutritionist", "主Agent", "你好", source="db_monitor")
    assert result["status"] == "error"
    assert pushed and "已不存在" in pushed[0]
