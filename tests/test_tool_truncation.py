"""工具输出截断测试。"""
from agent.generic.agent_loop import (
    MAX_TOOL_RESULT_CHARS,
    _truncate_tool_content,
    _truncate_dict_result,
)


def test_truncate_tool_content_str_under_limit():
    """字符串短于上限原样返回。"""
    assert _truncate_tool_content("hello", "test_tool") == "hello"


def test_truncate_tool_content_str_over_limit():
    """字符串超上限被截断 + 加 [截断] 标记。"""
    big = "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_tool_content(big, "test_tool")
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result
    assert "test_tool" in result


def test_truncate_dict_result_small_dict():
    """小 dict 原样返回（序列化后不超限）。"""
    d = {"status": "ok", "data": [1, 2, 3]}
    result = _truncate_dict_result(d, "test_tool")
    assert result == d


def test_truncate_dict_result_large_dict():
    """大 dict 序列化后超限，返回截断提示 dict。"""
    big_data = "x" * (MAX_TOOL_RESULT_CHARS + 5000)
    d = {"status": "ok", "data": big_data}
    result = _truncate_dict_result(d, "lightrag_get_graph")
    # 返回 dict 含截断提示
    assert isinstance(result, dict)
    assert result.get("status") == "truncated"
    assert "[截断]" in result.get("message", "")
    assert "lightrag_get_graph" in result.get("message", "")
    # data 字段被截断到合理大小
    assert len(result.get("data", "")) <= MAX_TOOL_RESULT_CHARS


def test_truncate_dict_result_non_serializable():
    """不可序列化的对象降级为 str 截断。"""
    class Foo:
        def __str__(self):
            return "x" * (MAX_TOOL_RESULT_CHARS + 1000)
    result = _truncate_dict_result(Foo(), "test_tool")
    assert isinstance(result, str)
    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert "[截断]" in result
