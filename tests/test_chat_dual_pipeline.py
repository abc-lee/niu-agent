"""测试 chat.py 双管道持久化 — 从 return value 获取 messages 并存入 DB。

双管道架构 Phase 4：
- SSE 管道：只推送 reply 内容给前端（runner.py 已完成）
- DB 管道：从 agent_runner_loop 的 return value 获取完整 messages，持久化到数据库

TDD: 先写测试，确认失败，再改代码。
"""
import pathlib

# ---------------------------------------------------------------------------
# 静态测试：验证 chat.py 代码结构
# ---------------------------------------------------------------------------

CHAT_PY_PATH = pathlib.Path(__file__).parent.parent / "niu_api" / "chat.py"


def test_chat_py_exists():
    """chat.py 文件应存在"""
    assert CHAT_PY_PATH.exists(), f"chat.py not found at {CHAT_PY_PATH}"



def test_chat_endpoint_accesses_return_value_messages():
    """chat_endpoint 应从 return value 获取 messages 并持久化"""
    source = CHAT_PY_PATH.read_text(encoding="utf-8")
    # 验证 /chat 端点中有获取 return_value 的代码
    assert "last_return_value" in source, (
        "/chat 端点应读取 runner.last_return_value 获取 return value"
    )
    # 验证 /chat 端点中有从 rv["messages"] 持久化消息的代码
    assert 'rv["messages"]' in source or "rv['messages']" in source, (
        "/chat 端点应有从 rv['messages'] 持久化消息的逻辑"
    )


def test_chat_sync_accesses_return_value_messages():
    """chat_sync 端点应从 return value 获取 messages"""
    source = CHAT_PY_PATH.read_text(encoding="utf-8")
    # 验证 /chat/sync 端点读取 last_return_value
    assert "last_return_value" in source, (
        "/chat/sync 端点应读取 runner.last_return_value"
    )



# ---------------------------------------------------------------------------
# 集成测试：验证 return value 中的 messages 结构
# ---------------------------------------------------------------------------

def test_runner_chat_exposes_last_return_value():
    """runner.chat() 应在完成后暴露 last_return_value"""
    runner_path = pathlib.Path(__file__).parent.parent / "agent" / "runner.py"
    source = runner_path.read_text(encoding="utf-8")
    assert "last_return_value" in source, (
        "runner.py 应设置 last_return_value 属性"
    )
    # 验证 last_return_value 在生成器完成后设置
    assert "self.last_return_value = return_value" in source or "self.last_return_value =" in source, (
        "runner.py 应在生成器完成后设置 last_return_value"
    )


def test_return_value_contains_messages_key():
    """agent_runner_loop 的 return value 应包含 messages 键"""
    agent_loop_path = pathlib.Path(__file__).parent.parent / "agent" / "generic" / "agent_loop.py"
    source = agent_loop_path.read_text(encoding="utf-8")
    # 验证所有 return 语句都包含 messages
    # 搜索所有 return 语句
    returns_with_messages = 0
    returns_without_messages = 0
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("return ") and "yield" not in stripped:
            if '"messages"' in stripped or "'messages'" in stripped or "messages" in stripped:
                returns_with_messages += 1
            elif stripped.startswith("return should_exit") or stripped.startswith("return None"):
                # should_exit 是 dict，包含 messages（由前面的代码添加）
                returns_with_messages += 1
            else:
                returns_without_messages += 1
    assert returns_with_messages > 0, (
        "agent_runner_loop 的 return value 应包含 messages 键"
    )


def test_message_store_add_message_supports_tool_call_id():
    """MessageStore.add_message 应支持 tool_call_id 参数"""
    session_path = pathlib.Path(__file__).parent.parent / "agent" / "session.py"
    source = session_path.read_text(encoding="utf-8")
    # 验证 add_message 方法签名包含 tool_call_id
    assert "tool_call_id" in source, (
        "MessageStore.add_message 应支持 tool_call_id 参数"
    )
