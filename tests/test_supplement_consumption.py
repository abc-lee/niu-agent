"""验证子 Agent supplement 消费：普通补充次末插入，/stop 最末插入。"""
from agent.subagent_supplement import SubagentSupplementItem


def test_format_supplement_for_insert_normal():
    """普通补充格式化为次末插入文本。"""
    from agent.generic.agent_loop import format_subagent_supplement
    items = [SubagentSupplementItem("补充1", False, "主Agent")]
    text = format_subagent_supplement(items)
    assert "补充1" in text
    assert "终止" not in text


def test_format_supplement_for_insert_terminate():
    """/stop 格式化为最末插入文本（含终止指令）。"""
    from agent.generic.agent_loop import format_subagent_supplement
    items = [SubagentSupplementItem("/stop", True, "主Agent")]
    text = format_subagent_supplement(items, is_final_position=True)
    assert "终止" in text
    assert "总结" in text


def test_format_supplement_empty():
    """空列表返回空字符串。"""
    from agent.generic.agent_loop import format_subagent_supplement
    assert format_subagent_supplement([]) == ""


def test_format_supplement_mixed_only_normal():
    """混合 items 但 is_final_position=False 时只格式化普通补充（跳过 terminate）。"""
    from agent.generic.agent_loop import format_subagent_supplement
    items = [
        SubagentSupplementItem("普通", False, "主Agent"),
        SubagentSupplementItem("/stop", True, "主Agent"),
    ]
    text = format_subagent_supplement(items, is_final_position=False)
    assert "普通" in text
    assert "/stop" not in text  # terminate 项不在次末位处理


def test_terminate_force_exit_logic_exists():
    """agent_runner_loop 应含终止强制退出逻辑。"""
    import inspect
    from agent.generic import agent_loop
    source = inspect.getsource(agent_loop)
    assert "supplement_terminate" in source, "agent_loop.py 未实现 supplement_terminate"
    # 验证有强制退出逻辑
    assert "if supplement_terminate" in source, "未实现终止强制退出"
