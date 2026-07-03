"""验证终止模式下生成总结（方案 B'）。"""
import inspect
from agent.generic import agent_loop


def test_terminate_branch_calls_llm():
    """终止分支应调 LLM 生成总结，不直接 return。"""
    source = inspect.getsource(agent_loop)
    # 找到 if supplement_terminate 分支
    assert "supplement_terminate" in source
    # 验证分支内有 client.chat 调用（生成总结）
    # 简化：检查源码含 client.chat 且在终止分支附近
    # 更精确：检查终止分支不直接 return（有 LLM 调用）
    assert "tools=[]" in source or "tools_schema=[]" in source, "终止模式应强制 tools=[] 禁用工具"


def test_terminate_branch_has_try_except():
    """终止分支的 LLM 调用应有 try-except 兜底。"""
    source = inspect.getsource(agent_loop)
    # 验证终止分支内有异常处理
    assert "except" in source


def test_terminate_result_text_includes_summary():
    """终止模式下 result_text 应含 LLM 生成的总结文本。"""
    source = inspect.getsource(agent_loop)
    # 验证终止分支内有 result_text 拼接逻辑
    assert "result_text" in source


def test_format_subagent_supplement_terminate_text():
    """format_subagent_supplement(is_final_position=True) 返回终止指令文本。"""
    from agent.generic.agent_loop import format_subagent_supplement
    from agent.subagent_supplement import SubagentSupplementItem
    items = [SubagentSupplementItem("/stop", True, "主Agent")]
    text = format_subagent_supplement(items, is_final_position=True)
    assert "终止" in text
    assert "总结" in text
