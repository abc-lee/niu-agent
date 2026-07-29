"""call_subagent_with_auto_answer helper 单元测试"""
from unittest import mock


def test_helper_returns_directly_for_non_at_niu_result():
    """第一次返回非 @niu-agent 文本 → 直接返回"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成结果"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"


def test_helper_auto_replies_to_at_niu_question():
    """第一次返回 @niu-agent 问题 → 自动回复 → 第二次返回 @end → 返回最终结果"""
    from agent import subagent

    call_count = [0]
    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "[file-processor-a1b2] 我该选哪个？"
        else:
            return "任务完成结果"

    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"
    assert call_count[0] == 2


def test_helper_does_not_misidentify_normal_result():
    """子 Agent 正常结果含 [已完成] 不被误判为 @niu-agent 问题"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="[已完成] 文件 X 处理完毕"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "[已完成] 文件 X 处理完毕"


def test_extract_unique_name_sync_path_plain_agent_name():
    """同步路径 [browser-operator] 问题 格式能被提取"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[browser-operator] 第一个问题", "browser-operator") == "browser-operator"
    assert _extract_unique_name("[file-processor] 我该选哪个？", "file-processor") == "file-processor"


def test_extract_unique_name_async_path_hex_suffix_still_works():
    """异步路径 [file-processor-a1b2] 问题 格式仍能被提取（保持向后兼容）"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[file-processor-a1b2] 第一个问题", "file-processor") == "file-processor-a1b2"
    assert _extract_unique_name("[browser-operator-708b] 问题", "browser-operator") == "browser-operator-708b"


def test_extract_unique_name_no_match_returns_none():
    """非 [子名] 格式返回 None"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("正常结果文本", "browser-operator") is None
    assert _extract_unique_name("[已完成] 任务结束", "browser-operator") is None
    assert _extract_unique_name("[browser-operator-x1yz] 问题", "browser-operator") is None  # x/y/z 非 hex


def test_call_subagent_with_auto_answer_sync_path_auto_replies(monkeypatch):
    """同步路径 [browser-operator] 问题 → call_subagent_with_auto_answer 自动回复"""
    from agent import subagent

    call_count = {"value": 0}
    call_args_log = []

    def fake_call_subagent(**kwargs):
        call_count["value"] += 1
        call_args_log.append(kwargs.copy())
        # 第一次返回 [browser-operator] 问题（同步路径格式）
        # 第二次返回正常结果
        if call_count["value"] == 1:
            return "[browser-operator] 第一个问题"
        return "子 Agent 完成"

    monkeypatch.setattr(subagent, "call_subagent", fake_call_subagent)

    result = subagent.call_subagent_with_auto_answer(
        agent_name="browser-operator",
        task="测试",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
    )

    # 应自动回复一次，然后第二次返回正常结果
    assert call_count["value"] == 2, f"应调用 2 次，实际：{call_count['value']}"
    assert result == "子 Agent 完成"
    # 第二次调用应传 answer + answer_unique_name
    second_call = call_args_log[1]
    assert second_call.get("answer") is not None
    assert second_call.get("answer_unique_name") == "browser-operator"
