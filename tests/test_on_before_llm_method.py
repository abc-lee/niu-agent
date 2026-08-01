"""NiuRunner._on_before_llm 方法单元测试。

验证：
1. _on_before_llm 调用 _inject_dynamic_resources + _assemble_system_message
2. _on_before_llm 修改 messages[0] 的 content（注入生效）
3. _on_turn_end 不再调 _inject_dynamic_resources（注入已移走）
4. 首轮（turn=1）合并 _first_turn_extra_injection（C4：拖入文件 resources 模式要求）
5. 第二轮（turn=2）不再合并（C4：实例属性已清空）
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.runner import NiuRunner


@pytest.fixture
def runner(monkeypatch):
    """构造一个最小化 NiuRunner 实例（C2 + M1 修复：补齐 _inject_dynamic_resources 访问的所有属性）

    故意跳过 __init__，已预填 _inject_dynamic_resources 当前实际访问的所有实例属性；
    若未来 _inject_dynamic_resources 新增实例属性访问，需同步更新此 fixture。
    """
    runner = NiuRunner.__new__(NiuRunner)
    # Decay pool (Ebbinghaus forgetting curve) — _inject_dynamic_resources 访问
    from agent.decay_pool import DecayPool
    runner._decay_pool = DecayPool()
    # _assemble_system_message 访问（C2 修复：缺 dynamic_system_prefix 必跑 AttributeError）
    runner.default_model = "test-model"
    runner.static_system_prompt = "STATIC SYSTEM PROMPT"
    runner.dynamic_system_prefix = ""  # C2 修复：_assemble_system_message L782 访问
    # _format_lightrag_entities_for_prompt 访问的两个黑名单（类属性，L1859-1860 定义）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    # 每轮重读 memory.json 后，patch 掉避免读真实 ~/.niu/memory.json（hermetic）
    monkeypatch.setattr("agent.runner._load_memory_for_prompt", lambda: "")
    return runner


def test_on_before_llm_calls_inject_and_assemble(runner):
    """_on_before_llm 调 _inject_dynamic_resources + _assemble_system_message"""
    runner._inject_dynamic_resources = MagicMock(return_value=("INJECTION TEXT", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=1)

    runner._extract_context_from_messages.assert_called_once_with(messages)
    runner._inject_dynamic_resources.assert_called_once_with("CONTEXT")
    runner._assemble_system_message.assert_called_once()
    # _assemble_system_message 的第 2 个参数应是 injection 文本
    args = runner._assemble_system_message.call_args
    assert args[0][2] == "INJECTION TEXT" or args.kwargs.get("injection") == "INJECTION TEXT"


def test_on_before_llm_modifies_messages_zero(runner):
    """_on_before_llm 修改 messages[0] 的 content（注入生效）

    走真实 _inject_dynamic_resources + _assemble_system_message 路径。
    C2 修复：fixture 已补 dynamic_system_prefix，_assemble_system_message 可正常调用。
    M1 修复：mock 掉 _format_running_subagents_section 避免真实 SubagentRegistry 副作用。
    """
    # 不 mock _inject_dynamic_resources，走真实路径
    runner._get_brain_injector = MagicMock(return_value=None)
    # M1 修复：mock 子 Agent 清单段，避免真实 SubagentRegistry.list_running() 副作用
    runner._format_running_subagents_section = MagicMock(return_value="")

    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter:
        mock_adapter.return_value.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
        runner._brain_adapter = mock_adapter.return_value

        messages = [{"role": "system", "content": "old content"}, {"role": "user", "content": "hello"}]
        runner._on_before_llm(messages, turn=1)

    # messages[0] 的 content 应被修改（_assemble_system_message 内部原地改）
    # Claude 路径改成 list，其他模型改字符串，都改变 content
    assert messages[0]["content"] != "old content", "messages[0] content 应被 _assemble_system_message 修改"


def test_on_turn_end_no_longer_calls_inject(runner):
    """_on_turn_end 不再调 _inject_dynamic_resources（注入已移到 _on_before_llm）"""
    runner._inject_dynamic_resources = MagicMock(return_value=("INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")

    # patch 脑区衰减
    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
        runner._on_turn_end(messages, tools_schema=[], turn=1)

    # _inject_dynamic_resources 不应被调用
    runner._inject_dynamic_resources.assert_not_called()
    # _assemble_system_message 也不应被调用
    runner._assemble_system_message.assert_not_called()


def test_on_before_llm_first_turn_merges_resources(runner):
    """C4 修复：_on_before_llm 首轮合并 _first_turn_extra_injection（resources 模式要求）

    拖入文件时 chat() 把 mode=reference/move 指令存入 self._first_turn_extra_injection，
    _on_before_llm 首轮（turn=1）把它合并进 injection，让首轮 LLM 能读到。
    """
    runner._inject_dynamic_resources = MagicMock(return_value=("DYNAMIC_INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")
    # 模拟 chat() 已存入 resources 文本
    runner._first_turn_extra_injection = "\n\n【文件操作模式要求】\n- 文件 x.pdf：必须使用引用模式（mode=reference）"

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=1)

    # _assemble_system_message 收到的 injection 应含 resources 文本
    args = runner._assemble_system_message.call_args
    injection_arg = args[0][2]
    assert "DYNAMIC_INJECTION" in injection_arg, "应含动态注入文本"
    assert "文件操作模式要求" in injection_arg, "应含 resources 模式要求文本"
    assert "mode=reference" in injection_arg, "应含具体 mode 指令"
    # 实例属性应被清空（防跨对话泄漏）
    assert runner._first_turn_extra_injection == "", "首轮合并后应清空"


def test_on_before_llm_second_turn_no_resources_merge(runner):
    """C4 修复：_on_before_llm 第二轮（turn=2）不再合并 resources（已清空）"""
    runner._inject_dynamic_resources = MagicMock(return_value=("DYNAMIC_INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")
    # 模拟首轮已清空（首轮合并后状态）
    runner._first_turn_extra_injection = ""

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=2)

    # 第二轮不合并 resources（实例属性已空）
    args = runner._assemble_system_message.call_args
    injection_arg = args[0][2]
    assert injection_arg == "DYNAMIC_INJECTION", "第二轮应只含动态注入，不含 resources"
