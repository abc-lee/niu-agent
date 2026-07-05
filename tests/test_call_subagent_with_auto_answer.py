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
