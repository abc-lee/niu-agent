"""D17/D19 缓存友好排布组装测试（_assemble_system_message v2 语义）。

覆盖计划要点：
- 完整 messages 形态快照：[system 静态区][历史索引 user][窗口原文…][动态块 user][当前输入]
- 动态块载体=user 全 provider 一致；幂等移除旧块（str 与 list 双载体共存）
- 轮中 tool 尾部场景插入位
- 首轮 chat() 空骨架路径（system 不含动态文本）
- supplement 与动态块共存相对顺序
"""
from unittest.mock import patch

from agent.runner import NiuRunner


def _make_runner(model="ark-code-latest"):
    runner = NiuRunner.__new__(NiuRunner)
    runner.static_system_prompt = "STATIC_PROMPT"
    runner.dynamic_system_prefix = "\n\n### [虚拟磁盘工具]\n...disk desc..."
    runner.default_model = model
    return runner


def _dyn_marker(m):
    c = m.get("content")
    return isinstance(c, str) and c.startswith("[系统动态信息]")


def test_full_sequence_snapshot_non_claude():
    """完整形态：[system 静态区][索引 user][窗口…][动态块 user][当前输入]。"""
    runner = _make_runner()
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "[历史索引]"},
        {"role": "assistant", "content": "窗口回复A"},
        {"role": "user", "content": "窗口输入B"},
        {"role": "assistant", "content": "窗口回复B"},
        {"role": "user", "content": "当前用户输入"},
    ]
    dynamic = runner._assemble_system_message(messages, "MEM_SEC", "INJ_TEXT", model=runner.default_model)
    runner._refresh_dynamic_user_block(messages, dynamic)

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "user", "user"]
    # system = 静态指令+disk_desc+memory，无时间无注入
    assert messages[0]["content"] == "STATIC_PROMPT\n\n### [虚拟磁盘工具]\n...disk desc...\n\nMEM_SEC"
    assert messages[1]["content"] == "[历史索引]"
    # 动态块紧贴最后一条 user 输入之前，头+注入+时间最后
    block = messages[-2]
    assert block["role"] == "user"
    assert block["content"].startswith("[系统动态信息]\nINJ_TEXT")
    assert block["content"].index("INJ_TEXT") < block["content"].index("Current Time:")
    assert block["content"].endswith("Current Time: ") is False  # 时间行有实际值
    assert messages[-1]["content"] == "当前用户输入"


def test_idempotent_removal_with_list_content_carriers():
    """str 与 list 双载体共存：list content 消息不干扰幂等移除与插入。"""
    runner = _make_runner()
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": [{"type": "text", "text": "Claude 富文本输入"}]},
    ]
    d1 = runner._build_dynamic_block("第一轮")
    runner._refresh_dynamic_user_block(messages, d1)
    assert sum(1 for m in messages if _dyn_marker(m)) == 1

    d2 = runner._build_dynamic_block("第二轮")
    runner._refresh_dynamic_user_block(messages, d2)
    blocks = [m for m in messages if _dyn_marker(m)]
    assert len(blocks) == 1, "list 载体存在时旧动态块仍被精确移除"
    assert "第二轮" in blocks[0]["content"]
    assert messages[-1]["content"] == [{"type": "text", "text": "Claude 富文本输入"}]


def test_on_before_llm_end_to_end_shape():
    """_on_before_llm 全链路：静态区就位 + 动态块插入 + 幂等，多轮不叠加。"""
    from agent import runner as runner_mod

    runner = _make_runner()
    runner._first_turn_extra_injection = ""
    calls = []


    def fake_assemble(self, messages, memory_section, injection, model):
        calls.append(injection)
        return self._build_dynamic_block(injection)

    with patch.object(runner_mod.NiuRunner, "_assemble_system_message", fake_assemble), \
         patch.object(runner_mod, "_load_memory_for_prompt", lambda: ""), \
         patch.object(runner_mod.NiuRunner, "_extract_context_from_messages", lambda self, m: ""), \
         patch.object(runner_mod.NiuRunner, "_inject_dynamic_resources", lambda self, ctx: ("SKILL_INJ", None)):

        messages: list[dict] = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "第一问"},
        ]
        runner._on_before_llm(messages, turn=1)
        assert sum(1 for m in messages if _dyn_marker(m)) == 1
        assert messages[-2]["content"].startswith("[系统动态信息]\nSKILL_INJ")

        # 第二轮（模拟轮中追加 assistant/tool 后再刷新）
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]})
        messages.append({"role": "tool", "tool_call_id": "t1", "content": "结果"})
        runner._on_before_llm(messages, turn=2)
        assert sum(1 for m in messages if _dyn_marker(m)) == 1, "多轮刷新不得叠加"
        # 轮中 tool 尾部：动态块仍锚定最后一个 user 输入之前
        assert messages[1]["content"].startswith("[系统动态信息]")
        assert messages[2]["content"] == "第一问"
        assert messages[-1]["role"] == "tool"


def test_first_turn_skeleton_via_build_path():
    """chat() 空骨架路径：injection/memory 为空 → system 只含静态指令+disk_desc。"""
    runner = _make_runner()
    system_message = {"role": "system", "content": ""}
    returned = runner._assemble_system_message([system_message], "", "", model=runner.default_model)

    assert system_message["content"] == "STATIC_PROMPT\n\n### [虚拟磁盘工具]\n...disk desc..."
    assert "Current Time" not in system_message["content"], "首条 system 不含动态文本（D17）"
    # 返回的动态块文本由 _on_before_llm turn=1 注入，骨架路径忽略
    assert returned.startswith("[系统动态信息]")
