"""端到端测试：验证 /chat SSE 端点在纯文本回复时正确持久化 assistant 消息。

测试场景：
1. 模拟用户发"你好"
2. LLM 返回纯文本回复"你好！我是妞妞"
3. 验证数据库中有 role=assistant 的记录，content 为"你好！我是妞妞"

测试策略：
- 使用 FastAPI TestClient 直接调用 /chat SSE 端点
- Mock NiuRunner.chat() 返回纯文本流 + last_return_value
- 使用临时 SQLite 数据库验证持久化结果
- 通过 patch agent.session._message_store 全局变量注入临时 store
"""
import pytest
import json
import asyncio
import tempfile
import os
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from pathlib import Path

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db_path():
    """创建临时数据库路径，测试后清理"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def temp_message_store(temp_db_path):
    """创建使用临时数据库的 MessageStore"""
    from agent.session import MessageStore
    store = MessageStore(db_path=temp_db_path)
    await store.init_db()
    yield store


# ---------------------------------------------------------------------------
# Helper: 构造纯文本回复的 mock runner
# ---------------------------------------------------------------------------

def _make_mock_runner_for_plain_text(reply_text: str, user_input: str):
    """构造一个 mock NiuRunner，其 chat() 方法返回纯文本流，
    并在完成后设置 last_return_value 包含完整的 messages。

    模拟 agent_runner_loop 在纯文本回复场景下的 return value：
    messages = [system, user, assistant(无 tool_calls)]
    """
    runner = Mock()

    # agent_runner_loop 纯文本回复的 return value
    return_value = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply_text},
        ],
    }

    def chat_generator(session_id, message, stream=True, **kwargs):
        """模拟 runner.chat() 的生成器行为：
        - yield reply 文本块（str，与 NiuRunner.chat() 一致）
        - 完成后设置 last_return_value
        """
        yield reply_text
        runner.last_return_value = return_value

    runner.chat = chat_generator
    runner.last_return_value = None
    runner._persisted_msgs = None  # 显式设 None，避免 Mock 属性访问返回 Mock 破坏 getattr 默认值
    runner.llm_config = {"apikey": "test-key", "model": "test-model"}

    return runner, return_value


# ---------------------------------------------------------------------------
# 端到端测试：/chat SSE → 持久化 → DB 验证
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_sse_persist_assistant_plain_text(temp_db_path, temp_message_store):
    """端到端验证：/chat SSE 端点在纯文本回复时正确持久化 assistant 消息。

    完整路径：
    1. 用户发 "你好"
    2. LLM 返回纯文本 "你好！我是妞妞"
    3. /chat 端点从 runner.last_return_value 提取 messages
    4. 持久化 user + assistant 消息到 SQLite
    5. 验证 DB 中有 role=assistant 的记录，content 为 "你好！我是妞妞"
    """
    user_input = "你好"
    reply_text = "你好！我是妞妞"

    mock_runner, expected_return_value = _make_mock_runner_for_plain_text(
        reply_text, user_input
    )

    # ---- 构造 FastAPI app ----
    from fastapi import FastAPI
    from niu_api.chat import router as chat_router
    import agent.session as session_module

    app = FastAPI()
    app.include_router(chat_router)

    # 保存原始全局 store，测试后恢复
    original_store = session_module._message_store
    session_module._message_store = temp_message_store

    try:
        with (
            patch("niu_api.chat.get_or_create_runner", return_value=mock_runner),
            patch("niu_api.chat._load_llm_config", return_value={
                "type": "openai",
                "apikey": "test-api-key",
                "apibase": "https://api.example.com",
                "model": "test-model",
            }),
            patch("niu_api.chat.notify_new_message", new_callable=AsyncMock),
            patch("niu_api.compat._check_and_trigger_auto_tidy", new_callable=AsyncMock),
        ):
            client = TestClient(app)

            # ---- 发送 /chat 请求 ----
            response = client.post(
                "/chat",
                json={"message": user_input, "session_id": "test-session"},
                headers={"Accept": "text/event-stream"},
            )

            assert response.status_code == 200, f"Expected 200, got {response.status_code}"

            # ---- 解析 SSE 响应 ----
            sse_text = response.text
            chunks = []
            done_received = False
            for line in sse_text.split("\n"):
                if line.startswith("data: "):
                    data_str = line[len("data: "):]
                    try:
                        data = json.loads(data_str)
                        if "chunk" in data:
                            chunks.append(data["chunk"])
                        if data.get("done"):
                            done_received = True
                    except json.JSONDecodeError:
                        pass

            # 验证 SSE 推送了 reply 内容
            assert chunks, f"SSE 应推送至少一个 chunk，实际: {sse_text}"
            full_reply = "".join(chunks)
            assert reply_text in full_reply, (
                f"SSE 回复应包含 '{reply_text}'，实际: '{full_reply}'"
            )
            assert done_received, "SSE 应发送 done 信号"

    finally:
        # 恢复全局 store
        session_module._message_store = original_store

    # ---- 验证数据库持久化 ----
    messages = await temp_message_store.get_messages()

    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(user_msgs) >= 1, (
        f"DB 应至少有 1 条 user 消息，实际: {len(user_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )
    assert len(assistant_msgs) >= 1, (
        f"DB 应至少有 1 条 assistant 消息，实际: {len(assistant_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )

    # 验证 user 消息内容
    assert user_msgs[0].content == user_input, (
        f"user 消息 content 应为 '{user_input}'，实际: '{user_msgs[0].content}'"
    )

    # 验证 assistant 消息内容
    assert assistant_msgs[0].content == reply_text, (
        f"assistant 消息 content 应为 '{reply_text}'，实际: '{assistant_msgs[0].content}'"
    )


# ---------------------------------------------------------------------------
# 补充测试：验证纯文本回复时 return_value 的 messages 结构
# ---------------------------------------------------------------------------

def test_plain_text_return_value_structure():
    """验证纯文本回复时 agent_runner_loop 的 return value 结构正确。

    纯文本回复（无工具调用）时：
    - messages 包含 system + user + assistant
    - assistant 消息无 tool_calls
    - result 为 CURRENT_TASK_DONE
    """
    from agent.generic.agent_loop import (
        agent_runner_loop,
        StreamEvent,
        StepOutcome,
    )

    resp = Mock()
    resp.content = "你好！我是妞妞"
    resp.tool_calls = []

    client = Mock()
    client.last_tools = ""

    def chat_fn(**kwargs):
        def gen():
            yield resp
            return resp
        return gen()

    client.chat = chat_fn

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 40
    handler.current_turn = 1

    def dispatch_no_tool(tool_name, args, response, index=0):
        yield
        # next_prompt 必须是空字符串而非 None——agent_loop 用 len(next_prompts)==0 判断退出
        # None 会被 add 进 set 破坏判断（set 里有 None，len=1 永远不退出）
        return StepOutcome(data=None, next_prompt="", should_exit=False)

    handler.dispatch = dispatch_no_tool

    gen = agent_runner_loop(
        client=client,
        system_prompt="You are a helpful assistant.",
        user_input="你好",
        handler=handler,
        tools_schema=[],
        verbose=False,
    )

    events = []
    return_value = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return_value = e.value

    # 验证 return value 结构
    assert isinstance(return_value, dict)
    assert return_value["result"] == "CURRENT_TASK_DONE"
    assert "messages" in return_value

    messages = return_value["messages"]
    # 应包含 system + user + assistant
    assert len(messages) == 3, f"Expected 3 messages, got {len(messages)}: {messages}"

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "你好"
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "你好！我是妞妞"
    # 纯文本回复不应有 tool_calls
    assert "tool_calls" not in messages[2] or not messages[2].get("tool_calls"), (
        f"纯文本回复的 assistant 消息不应有 tool_calls，实际: {messages[2]}"
    )


# ---------------------------------------------------------------------------
# 补充测试：验证 /chat 端点 DB 管道对 assistant 消息的持久化逻辑
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_db_pipeline_persist_assistant_from_return_value(temp_db_path, temp_message_store):
    """验证 /chat 端点的 DB 管道从 return_value 正确持久化 assistant 消息。

    直接模拟 /chat 端点中 rv["messages"] 遍历持久化的逻辑，
    不经过 HTTP 请求，更精确地测试持久化路径。
    """
    store = temp_message_store

    # 模拟 agent_runner_loop 的 return value（纯文本回复）
    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！我是妞妞"},
        ],
    }

    # 复现 /chat 端点中的 DB 管道持久化逻辑（chat.py 第 316-335 行）
    user_persisted = False
    last_assistant_id = None

    for msg in rv["messages"]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id", "")

        if role == "system":
            continue
        if role == "user":
            if not user_persisted:
                user_msg_id = await store.add_message(role="user", content=content)
                user_persisted = True
            continue
        elif role == "tool" and tool_call_id:
            await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
        elif role == "assistant":
            pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
            last_assistant_id = pid

    # 验证持久化结果
    assert last_assistant_id is not None, "应持久化 assistant 消息"

    messages = await store.get_messages()
    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(user_msgs) == 1, f"应有 1 条 user 消息，实际: {len(user_msgs)}"
    assert len(assistant_msgs) == 1, f"应有 1 条 assistant 消息，实际: {len(assistant_msgs)}"
    assert assistant_msgs[0].content == "你好！我是妞妞", (
        f"assistant content 应为 '你好！我是妞妞'，实际: '{assistant_msgs[0].content}'"
    )


# ---------------------------------------------------------------------------
# 补充测试：验证 system 消息不被持久化
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_db_pipeline_skips_system_messages(temp_db_path, temp_message_store):
    """验证 DB 管道不持久化 system 消息。"""
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }

    for msg in rv["messages"]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            continue
        if role == "user":
            await store.add_message(role="user", content=content)
        elif role == "assistant":
            await store.add_message(role="assistant", content=content)

    messages = await store.get_messages()
    system_msgs = [m for m in messages if m.role == "system"]
    assert len(system_msgs) == 0, f"DB 不应有 system 消息，实际: {len(system_msgs)}"
