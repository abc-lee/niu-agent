"""同步子 Agent 交互单元测试"""


def test_ask_main_agent_impl_sync_appends_assistant_and_returns_wrapped():
    """_ask_main_agent_impl_sync 调用后 messages append assistant content + 返回 [unique_name] question"""
    from agent import subagent

    messages = [{"role": "user", "content": "开始"}]
    fake_handler = object()  # 不需要 handler 属性

    wrapped = subagent._ask_main_agent_impl_sync(
        question="我应该选择哪个选项？",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent 我应该选择哪个选项？",
    )

    # 断言：messages append assistant content
    assert messages[-1] == {"role": "assistant", "content": "@niu-agent 我应该选择哪个选项？"}
    # 断言：返回 wrapped 文本
    assert wrapped == "[test-ab12] 我应该选择哪个选项？"
    # 断言：messages 末尾是 assistant（不是 user）
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_ask_main_agent_impl_sync_sanitizes_question():
    """_ask_main_agent_impl_sync 对 question 做 sanitization（限 2000 字符 + strip 行首 @）"""
    from agent import subagent

    messages = []
    fake_handler = object()

    # 超长 question 截断
    long_question = "x" * 3000
    wrapped = subagent._ask_main_agent_impl_sync(
        question=long_question,
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent ...",
    )
    assert len(wrapped) < 3000  # 已截断

    # question 行首 @ 被 strip
    wrapped2 = subagent._ask_main_agent_impl_sync(
        question="@嵌套@问题",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent @嵌套@问题",
    )
    assert wrapped2 == "[test-ab12] 嵌套@问题"  # 行首 @ 被 strip
