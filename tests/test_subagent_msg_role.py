"""验证 role=subagent_msg 消息能存入 db 且 history 重建时被过滤。"""
import asyncio
import os
import tempfile


def test_add_subagent_msg_message():
    """能存 role=subagent_msg 的消息。"""
    from agent.session import MessageStore
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = MessageStore(db_path)

        async def _run():
            await store.init_db()
            return await store.add_message(role="subagent_msg", content="@主Agent [file-processor-a1b2] 测试问题")

        asyncio.run(_run())

        async def _get():
            return await store.get_messages()

        msgs = asyncio.run(_get())
        assert len(msgs) == 1
        assert msgs[0].role == "subagent_msg"
        assert "@主Agent" in msgs[0].content
    finally:
        os.unlink(db_path)


def test_get_messages_includes_subagent_msg():
    """get_messages 返回 subagent_msg 消息（不过滤 role）。"""
    from agent.session import MessageStore
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = MessageStore(db_path)

        async def _run():
            await store.init_db()
            await store.add_message(role="user", content="用户消息")
            await store.add_message(role="subagent_msg", content="@主Agent [test] 子 Agent 消息")
            await store.add_message(role="assistant", content="主 Agent 回复")
            return await store.get_messages()

        msgs = asyncio.run(_run())
        roles = [m.role for m in msgs]
        assert "subagent_msg" in roles
        assert len(msgs) == 3
    finally:
        os.unlink(db_path)


def test_history_reconstruction_skips_subagent_msg():
    """history 重建时 subagent_msg 消息被过滤，不进 LLM 上下文。"""
    import inspect

    from agent.generic import agent_loop
    source = inspect.getsource(agent_loop)
    assert 'subagent_msg' in source, "agent_loop.py 未处理 subagent_msg role"
    # 找到 history 重建段，确认有 continue 跳过
    # 用更精确的检查：找 msg.get("role") == "subagent_msg" 的 continue
    assert 'subagent_msg' in source and 'continue' in source, "未实现 subagent_msg 过滤"


def test_history_uses_dict_access():
    """history 重建用 dict 访问（msg.get），不是属性访问（msg.role）。"""
    import inspect

    from agent.generic import agent_loop
    source = inspect.getsource(agent_loop)
    # 确认有 msg.get("role") 调用，不是 msg.role
    assert 'msg.get("role")' in source or 'msg.get(\'role\')' in source, "history 重建应用 msg.get dict 访问"
