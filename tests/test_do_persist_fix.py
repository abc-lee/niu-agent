"""测试 _do_persist 修复：纯文本回复时从 rv["data"] 构造 assistant 消息。

已验证的事实：
- 纯文本回复时，rv["messages"] 中没有 role=assistant 的消息
- 但 rv["data"] 可能包含回复内容（字符串）
- 有 tool_calls 时，rv["messages"] 中有 assistant 消息

修复方案：
- 在 _do_persist 中，当 rv["messages"] 中没有 role=assistant 的消息时，
  从 rv["data"] 或 reply_chunks 提取内容，构造一条 assistant 消息写入数据库。

测试场景：
A: rv["messages"] 中有 assistant 消息（有 tool_calls）→ 正常持久化
B: rv["messages"] 中没有 assistant 消息，但 rv["data"] 有内容（纯文本回复）→ 从 rv["data"] 构造 assistant 消息并持久化
C: rv["messages"] 中没有 assistant 消息，rv["data"] 为 None → 不持久化 assistant 消息
"""
import pytest
import json
import tempfile
import os

from agent.session import MessageStore


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
    store = MessageStore(db_path=temp_db_path)
    await store.init_db()
    yield store


# ---------------------------------------------------------------------------
# _do_persist 逻辑的独立复现（与 chat.py /chat SSE 端点一致）
# ---------------------------------------------------------------------------

async def _do_persist(store, rv, reply_chunks=None):
    """复现 /chat SSE 端点中 rv["messages"] 遍历持久化的逻辑。

    这是 chat.py 第 308-349 行的简化版本，用于独立测试。

    Args:
        store: MessageStore 实例
        rv: runner.last_return_value（dict）
        reply_chunks: SSE 管道收集的 reply 文本块列表
    """
    if not rv or not isinstance(rv, dict) or not rv.get("messages"):
        return

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
                await store.add_message(role="user", content=content)
                user_persisted = True
            continue
        elif role == "tool" and tool_call_id:
            await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
        elif role == "assistant":
            pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
            last_assistant_id = pid

    # 修复：当 rv["messages"] 中没有 assistant 消息时，
    # 从 rv["data"] 或 reply_chunks 构造 assistant 消息
    if last_assistant_id is None:
        # 优先从 rv["data"] 提取内容
        assistant_content = None
        data = rv.get("data")
        if data is not None:
            if isinstance(data, str):
                assistant_content = data
            elif isinstance(data, dict):
                assistant_content = json.dumps(data, ensure_ascii=False)
            elif isinstance(data, list):
                assistant_content = json.dumps(data, ensure_ascii=False)
            else:
                assistant_content = str(data)
        # 回退：从 reply_chunks 提取
        elif reply_chunks:
            full_reply = "".join(reply_chunks)
            if full_reply.strip():
                assistant_content = full_reply

        if assistant_content and assistant_content.strip():
            pid = await store.add_message(role="assistant", content=assistant_content)
            last_assistant_id = pid


# ---------------------------------------------------------------------------
# 场景 A：rv["messages"] 中有 assistant 消息（有 tool_calls）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_a_assistant_with_tool_calls(temp_db_path, temp_message_store):
    """场景A：rv["messages"] 中有 assistant 消息（有 tool_calls）→ 正常持久化

    模拟有工具调用时的 return value：
    - messages 包含 system + user + assistant(tool_calls) + tool(result)
    - assistant 消息应正常持久化，不需要从 rv["data"] 构造
    """
    store = temp_message_store

    rv = {
        "result": "EXITED",
        "data": "工具执行结果",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "帮我查一下今天的天气"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "北京"}'
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "北京今天晴，25°C"
            },
        ],
    }

    await _do_persist(store, rv, reply_chunks=["北京今天晴，25°C"])

    messages = await store.get_messages()
    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    tool_msgs = [m for m in messages if m.role == "tool"]

    # 验证 user 消息
    assert len(user_msgs) == 1, f"应有 1 条 user 消息，实际: {len(user_msgs)}"
    assert user_msgs[0].content == "帮我查一下今天的天气"

    # 验证 assistant 消息（带 tool_calls）
    assert len(assistant_msgs) == 1, f"应有 1 条 assistant 消息，实际: {len(assistant_msgs)}"
    assert assistant_msgs[0].tool_calls is not None, "assistant 消息应有 tool_calls"

    # 验证 tool 消息
    assert len(tool_msgs) == 1, f"应有 1 条 tool 消息，实际: {len(tool_msgs)}"
    assert tool_msgs[0].content == "北京今天晴，25°C"


# ---------------------------------------------------------------------------
# 场景 B：rv["messages"] 中没有 assistant 消息，rv["data"] 有内容
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_b_plain_text_from_rv_data(temp_db_path, temp_message_store):
    """场景B：rv["messages"] 中没有 assistant 消息，但 rv["data"] 有内容（纯文本回复）

    模拟纯文本回复时的 return value：
    - messages 只有 system + user（没有 assistant）
    - rv["data"] 包含回复内容字符串
    - 应从 rv["data"] 构造 assistant 消息并持久化
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": "你好！我是妞妞，很高兴认识你！",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=["你好！我是妞妞，很高兴认识你！"])

    messages = await store.get_messages()
    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    # 验证 user 消息
    assert len(user_msgs) == 1, f"应有 1 条 user 消息，实际: {len(user_msgs)}"
    assert user_msgs[0].content == "你好"

    # 验证 assistant 消息从 rv["data"] 构造
    assert len(assistant_msgs) == 1, (
        f"应有 1 条 assistant 消息（从 rv['data'] 构造），实际: {len(assistant_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )
    assert assistant_msgs[0].content == "你好！我是妞妞，很高兴认识你！", (
        f"assistant content 应为 '你好！我是妞妞，很高兴认识你！'，"
        f"实际: '{assistant_msgs[0].content}'"
    )


@pytest.mark.asyncio
async def test_scenario_b_plain_text_from_reply_chunks(temp_db_path, temp_message_store):
    """场景B变体：rv["data"] 为 None，但 reply_chunks 有内容

    当 rv["data"] 为 None 时，应从 reply_chunks 构造 assistant 消息。
    这对应 agent_runner_loop 纯文本回复时 data=None 的情况。
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=["你好！", "我是妞妞。"])

    messages = await store.get_messages()
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(assistant_msgs) == 1, (
        f"应有 1 条 assistant 消息（从 reply_chunks 构造），实际: {len(assistant_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )
    assert assistant_msgs[0].content == "你好！我是妞妞。", (
        f"assistant content 应为 '你好！我是妞妞。'，"
        f"实际: '{assistant_msgs[0].content}'"
    )


@pytest.mark.asyncio
async def test_scenario_b_rv_data_dict(temp_db_path, temp_message_store):
    """场景B变体：rv["data"] 是 dict 类型

    当 rv["data"] 是字典时，应序列化为 JSON 字符串作为 assistant 内容。
    """
    store = temp_message_store

    data_dict = {"answer": "42", "source": "deep_thought"}
    rv = {
        "result": "EXITED",
        "data": data_dict,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "生命的意义是什么？"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=[])

    messages = await store.get_messages()
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(assistant_msgs) == 1, (
        f"应有 1 条 assistant 消息（从 rv['data'] dict 构造），实际: {len(assistant_msgs)}"
    )
    # dict 应被序列化为 JSON
    parsed = json.loads(assistant_msgs[0].content)
    assert parsed == data_dict, f"assistant content 应为 JSON 序列化的 dict，实际: {assistant_msgs[0].content}"


# ---------------------------------------------------------------------------
# 场景 C：rv["messages"] 中没有 assistant 消息，rv["data"] 为 None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario_c_no_assistant_no_data(temp_db_path, temp_message_store):
    """场景C：rv["messages"] 中没有 assistant 消息，rv["data"] 为 None → 不持久化 assistant 消息

    当既没有 assistant 消息，也没有 data 或 reply_chunks 时，
    不应构造空的 assistant 消息。
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=[])

    messages = await store.get_messages()
    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    # 验证 user 消息正常持久化
    assert len(user_msgs) == 1, f"应有 1 条 user 消息，实际: {len(user_msgs)}"

    # 验证没有 assistant 消息
    assert len(assistant_msgs) == 0, (
        f"不应有 assistant 消息（data=None, reply_chunks 为空），"
        f"实际: {len(assistant_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )


@pytest.mark.asyncio
async def test_scenario_c_empty_string_data(temp_db_path, temp_message_store):
    """场景C变体：rv["data"] 为空字符串 → 不持久化 assistant 消息

    空字符串 strip() 后为空，不应构造 assistant 消息。
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": "",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=[])

    messages = await store.get_messages()
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(assistant_msgs) == 0, (
        f"不应有 assistant 消息（data 为空字符串），实际: {len(assistant_msgs)}"
    )


# ---------------------------------------------------------------------------
# 边界测试：rv["data"] 优先于 reply_chunks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rv_data_takes_priority_over_reply_chunks(temp_db_path, temp_message_store):
    """rv["data"] 优先于 reply_chunks

    当两者都有内容时，应优先使用 rv["data"] 的内容。
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": "来自 rv[data] 的内容",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    await _do_persist(store, rv, reply_chunks=["来自 reply_chunks 的内容"])

    messages = await store.get_messages()
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].content == "来自 rv[data] 的内容", (
        f"应优先使用 rv['data'] 的内容，实际: '{assistant_msgs[0].content}'"
    )


# ---------------------------------------------------------------------------
# 对比测试：当前 _do_persist 逻辑（无修复）在场景 B 下失败
# ---------------------------------------------------------------------------

async def _do_persist_current(store, rv, reply_chunks=None):
    """当前 chat.py 中的 _do_persist 逻辑（无修复），用于对比测试。

    这是 chat.py 第 308-349 行的简化版本，不包含从 rv["data"] 构造 assistant 消息的逻辑。
    """
    if not rv or not isinstance(rv, dict) or not rv.get("messages"):
        return

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
                await store.add_message(role="user", content=content)
                user_persisted = True
            continue
        elif role == "tool" and tool_call_id:
            await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
        elif role == "assistant":
            pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
            last_assistant_id = pid

    # 当前代码：没有从 rv["data"] 构造 assistant 消息的逻辑
    # 如果 rv["messages"] 中没有 assistant 消息，last_assistant_id 为 None
    # assistant 消息就不会被持久化


@pytest.mark.asyncio
async def test_current_logic_fails_scenario_b(temp_db_path, temp_message_store):
    """对比测试：当前逻辑在场景B（纯文本回复，rv["messages"] 无 assistant）下失败。

    这个测试验证了 bug 的存在：当前 _do_persist 逻辑不会从 rv["data"] 构造 assistant 消息，
    导致纯文本回复时数据库中没有 assistant 记录。
    """
    store = temp_message_store

    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": "你好！我是妞妞",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "你好"},
        ],
    }

    # 使用当前逻辑（无修复）
    await _do_persist_current(store, rv, reply_chunks=["你好！我是妞妞"])

    messages = await store.get_messages()
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    # 当前逻辑下，assistant 消息不会被持久化 → 这是 bug
    assert len(assistant_msgs) == 0, (
        f"当前逻辑下 assistant 消息不会被持久化（bug），"
        f"实际: {len(assistant_msgs)}"
    )
