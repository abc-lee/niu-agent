"""
端到端测试：消息持久化流程（真实 LLM API 调用）

测试场景：
1. 纯文本回复 — LLM 不调用工具，验证 StreamEvent("reply") 包含内容，
   且 rv["messages"] 包含 system + user 消息
2. 有 tool_calls 的回复 — LLM 调用工具后返回纯文本，验证 assistant 消息带 tool_calls
3. 持久化验证（纯文本）— 从 StreamEvent 提取 assistant content 写入 SQLite
3b. 持久化验证（tool_calls）— 从 rv["messages"] 持久化 tool 相关消息到 SQLite

注意：不 mock LLM，使用真实 API 调用。

关键行为说明：
agent_runner_loop 中，纯文本回复（无 tool_calls）不会将 assistant 消息
追加到 messages 列表中。assistant 内容通过 StreamEvent("reply", content) yield 出来。
这是 chat.py 中双管道持久化的设计基础：
- SSE 管道：从 StreamEvent 流式推送内容给前端
- DB 管道：从 rv["messages"] 持久化 tool 相关消息，
  纯文本 assistant 回复从 reply_chunks 拼接后单独 add_message
"""

import asyncio
import json
import os
import sys
import tempfile

import aiosqlite
import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.generic.agent_loop import (
    agent_runner_loop,
    BaseHandler,
    StepOutcome,
    StreamEvent,
    exhaust,
)
from agent.generic.litellm_adapter import create_litellm_client
from agent.session import MessageStore


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_llm_config():
    """从 user-config.json 加载真实 LLM 配置"""
    config_path = os.path.join(PROJECT_ROOT, "config", "user-config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    llm = data.get("llm", {})
    # 统一键名为小写
    config = {}
    for key, value in llm.items():
        config[key.lower()] = value
    config.setdefault("type", "openai")
    config.setdefault("apikey", "")
    config.setdefault("apibase", "")
    config.setdefault("model", "")
    return config


# ---------------------------------------------------------------------------
# 测试用 Handler
# ---------------------------------------------------------------------------

class EchoHandler(BaseHandler):
    """简单的 Handler，实现一个 echo 工具"""

    def __init__(self):
        super().__init__()
        self._done_hooks = []
        self.max_turns = 40
        self.current_turn = 1

    def do_echo(self, args, response):
        """echo 工具：原样返回用户输入的文本"""
        text = args.get("text", "")
        yield StreamEvent("system", f"[echo] {text}\n")
        return StepOutcome(data={"echo": text}, next_prompt=f"echo result: {text}", should_exit=False)


# ---------------------------------------------------------------------------
# 工具 Schema
# ---------------------------------------------------------------------------

ECHO_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo back the input text. Use this when the user asks you to echo something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to echo back",
                    }
                },
                "required": ["text"],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _run_agent_loop(client, system_prompt, user_input, handler, tools_schema=None, verbose=False):
    """运行 agent_runner_loop 并收集所有 StreamEvent + return value"""
    events = []
    return_value = None
    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema or [],
        verbose=verbose,
    )
    try:
        while True:
            event = next(gen)
            events.append(event)
    except StopIteration as e:
        return_value = e.value
    return events, return_value


def _extract_reply_content(events):
    """从 StreamEvent 列表中提取所有 reply 类型的内容（拼接）"""
    chunks = []
    for event in events:
        if isinstance(event, StreamEvent) and event.type == "reply":
            chunks.append(event.content)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# 场景1：纯文本回复
# ---------------------------------------------------------------------------

def test_scenario1_plain_text_reply():
    """场景1：LLM 返回纯文本回复（没有 tool_calls）

    agent_runner_loop 的行为：
    - 纯文本回复不会将 assistant 消息追加到 rv["messages"]
    - assistant 内容通过 StreamEvent("reply", content) yield 出来
    - rv["messages"] 只包含 system + user 消息

    验证：
    1. StreamEvent("reply") 中有非空内容
    2. rv["messages"] 包含 system + user 消息
    3. rv["result"] == "CURRENT_TASK_DONE"
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = "你是一个测试助手。请用简短的中文回复用户。不要调用任何工具。"
    user_input = "你好"

    events, rv = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=ECHO_TOOL_SCHEMA,
        verbose=False,
    )

    # 验证 return value 结构
    assert rv is not None, "agent_runner_loop returned None"
    assert isinstance(rv, dict), f"Expected dict, got {type(rv)}"
    assert "messages" in rv, f"Missing 'messages' in return value: {list(rv.keys())}"
    assert "result" in rv, f"Missing 'result' in return value: {list(rv.keys())}"
    assert rv["result"] == "CURRENT_TASK_DONE", f"Expected CURRENT_TASK_DONE, got {rv['result']}"

    messages = rv["messages"]

    # 验证 messages 包含 system + user
    assert len(messages) >= 2, f"Expected at least 2 messages (system+user), got {len(messages)}"
    assert messages[0]["role"] == "system", f"First message should be system, got {messages[0]['role']}"
    assert messages[1]["role"] == "user", f"Second message should be user, got {messages[1]['role']}"

    # 验证 StreamEvent("reply") 中有非空内容
    reply_content = _extract_reply_content(events)
    assert reply_content.strip(), (
        f"Expected non-empty reply content from StreamEvents. "
        f"Event types: {[e.type if isinstance(e, StreamEvent) else type(e).__name__ for e in events]}"
    )

    # 验证纯文本回复时 messages 中没有 assistant 消息
    # （这是 agent_runner_loop 的设计：纯文本不追加 assistant 到 messages）
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 0, (
        f"Pure text reply should NOT add assistant to messages, "
        f"but found {len(assistant_msgs)} assistant message(s). "
        f"This is a behavior change in agent_runner_loop."
    )

    print(f"\n[Scenario1] PASSED: reply_content='{reply_content[:100]}', "
          f"messages={[m['role'] for m in messages]}")


# ---------------------------------------------------------------------------
# 场景2：有 tool_calls 的回复
# ---------------------------------------------------------------------------

def test_scenario2_tool_calls_reply():
    """场景2：LLM 调用工具后返回纯文本回复

    agent_runner_loop 的行为：
    - 带 tool_calls 的 assistant 消息会被追加到 messages
    - tool 结果消息也会被追加到 messages
    - 最终纯文本回复通过 StreamEvent("reply") yield

    验证：
    1. rv["messages"] 中有 assistant 消息（带 tool_calls）
    2. rv["messages"] 中有 tool 消息
    3. tool_calls 中的工具名是 echo
    4. StreamEvent("reply") 中有最终纯文本内容
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果。"
    )
    user_input = "请用 echo 工具返回文字：hello world"

    events, rv = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=ECHO_TOOL_SCHEMA,
        verbose=False,
    )

    assert rv is not None, "agent_runner_loop returned None"
    assert isinstance(rv, dict), f"Expected dict, got {type(rv)}"
    assert "messages" in rv, f"Missing 'messages' in return value"

    messages = rv["messages"]

    # 验证有 assistant 消息（带 tool_calls）
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1, (
        f"Expected at least 1 assistant message, got {len(assistant_msgs)}. "
        f"All message roles: {[m.get('role') for m in messages]}"
    )

    # 验证至少有一条带 tool_calls 的 assistant 消息
    tool_call_msgs = [m for m in assistant_msgs if m.get("tool_calls")]
    assert len(tool_call_msgs) >= 1, (
        f"Expected at least 1 assistant message with tool_calls, got {len(tool_call_msgs)}. "
        f"LLM may not have called the tool. Assistant messages: "
        f"{[{'content': m.get('content', '')[:80], 'has_tool_calls': bool(m.get('tool_calls'))} for m in assistant_msgs]}"
    )

    # 验证 tool_calls 中的工具名是 echo
    first_tc_msg = tool_call_msgs[0]
    tc_names = [tc["function"]["name"] for tc in first_tc_msg["tool_calls"]]
    assert "echo" in tc_names, f"Expected 'echo' in tool_calls, got: {tc_names}"

    # 验证有 tool 消息
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) >= 1, f"Expected at least 1 tool message, got {len(tool_msgs)}"

    # 验证 StreamEvent("reply") 中有最终的纯文本内容
    reply_content = _extract_reply_content(events)
    assert reply_content.strip(), (
        f"Expected non-empty reply content after tool execution. "
        f"Event types: {[e.type if isinstance(e, StreamEvent) else type(e).__name__ for e in events]}"
    )

    print(f"\n[Scenario2] PASSED: {len(assistant_msgs)} assistant message(s), "
          f"{len(tool_call_msgs)} with tool_calls, "
          f"{len(tool_msgs)} tool message(s)")
    print(f"  reply_content preview: '{reply_content[:100]}'")
    for i, msg in enumerate(assistant_msgs):
        has_tc = bool(msg.get("tool_calls"))
        content_preview = msg.get("content", "")[:80]
        print(f"  assistant[{i}]: tool_calls={has_tc}, content='{content_preview}'")


# ---------------------------------------------------------------------------
# 场景3：持久化验证（纯文本回复）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario3_persist_to_sqlite():
    """场景3：模拟 _do_persist 逻辑，纯文本回复写入 SQLite 数据库

    模拟 chat.py 的双管道持久化逻辑：
    - 纯文本回复的 assistant 内容从 StreamEvent("reply") 提取
    - user 消息从 rv["messages"] 提取
    - assistant 纯文本回复单独 add_message（不在 rv["messages"] 中）

    验证：
    1. 数据库中有 role=assistant 的记录
    2. assistant content 不为空
    3. 数据库中有 role=user 的记录
    """
    # 先用真实 LLM 获取消息
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = "你是一个测试助手。请用简短的中文回复用户。不要调用任何工具。"
    user_input = "你好，请回复一句话"

    events, rv = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=ECHO_TOOL_SCHEMA,
        verbose=False,
    )

    assert rv is not None, "agent_runner_loop returned None"
    assert "messages" in rv, "Missing 'messages' in return value"
    messages = rv["messages"]

    # 从 StreamEvent 提取 assistant 纯文本回复
    reply_content = _extract_reply_content(events)
    assert reply_content.strip(), "Expected non-empty reply content from StreamEvents"

    # 使用临时数据库进行持久化测试
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 模拟 chat.py 的双管道持久化逻辑
        # 1. 从 rv["messages"] 持久化 user 消息（只持久化第一条 user）
        user_persisted = False
        for msg in messages:
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
            if role == "tool" and tool_call_id:
                await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            elif role == "assistant":
                await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)

        # 2. 纯文本回复的 assistant 内容从 StreamEvent 提取后单独持久化
        # （模拟 chat.py 中 reply_chunks 拼接后 add_message 的逻辑）
        if reply_content.strip():
            await store.add_message(role="assistant", content=reply_content)

        # 验证数据库中有 assistant 记录
        all_messages = await store.get_messages()
        assistant_records = [m for m in all_messages if m.role == "assistant"]
        assert len(assistant_records) >= 1, (
            f"Expected at least 1 assistant record in DB, got {len(assistant_records)}. "
            f"All records: {[m.role for m in all_messages]}"
        )

        # 验证 assistant 记录的 content 不为空
        for i, record in enumerate(assistant_records):
            assert record.content.strip(), (
                f"Assistant record #{i} has empty content in DB. Record: {record}"
            )

        # 验证 user 记录也存在
        user_records = [m for m in all_messages if m.role == "user"]
        assert len(user_records) >= 1, f"Expected at least 1 user record in DB, got {len(user_records)}"

        print(f"\n[Scenario3] PASSED: {len(all_messages)} records in DB, "
              f"{len(assistant_records)} assistant, {len(user_records)} user")
        for i, record in enumerate(assistant_records):
            print(f"  assistant[{i}]: content='{record.content[:80]}'")
    finally:
        # 清理临时数据库
        if os.path.exists(db_path):
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# 场景3b：有 tool_calls 的消息持久化验证
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scenario3b_persist_tool_calls_to_sqlite():
    """场景3b：有 tool_calls 的消息持久化到 SQLite

    模拟 chat.py 的双管道持久化逻辑：
    - 带 tool_calls 的 assistant 消息从 rv["messages"] 持久化
    - tool 消息从 rv["messages"] 持久化
    - 最终纯文本回复从 StreamEvent 提取后持久化

    验证：
    1. 数据库中有带 tool_calls 的 assistant 记录
    2. 数据库中有 tool 记录
    3. tool 记录的 tool_call_id 关联正确
    4. 数据库中有纯文本 assistant 记录（最终回复）
    """
    # 先用真实 LLM 获取消息
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果。"
    )
    user_input = "请用 echo 工具返回文字：persistence test"

    events, rv = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=ECHO_TOOL_SCHEMA,
        verbose=False,
    )

    assert rv is not None, "agent_runner_loop returned None"
    assert "messages" in rv, "Missing 'messages' in return value"
    messages = rv["messages"]

    # 从 StreamEvent 提取最终纯文本回复
    reply_content = _extract_reply_content(events)

    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 模拟 chat.py 的双管道持久化逻辑
        user_persisted = False
        for msg in messages:
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
            if role == "tool" and tool_call_id:
                await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            elif role == "assistant":
                await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)

        # 最终纯文本回复从 StreamEvent 提取后持久化
        if reply_content.strip():
            await store.add_message(role="assistant", content=reply_content)

        # 验证数据库记录
        all_messages = await store.get_messages()

        # 验证有带 tool_calls 的 assistant 记录
        assistant_with_tc = [m for m in all_messages if m.role == "assistant" and m.tool_calls]
        assert len(assistant_with_tc) >= 1, (
            f"Expected at least 1 assistant record with tool_calls in DB, "
            f"got {len(assistant_with_tc)}. "
            f"All assistant records: {[(m.role, bool(m.tool_calls)) for m in all_messages if m.role == 'assistant']}"
        )

        # 验证 tool_calls 的内容
        first_tc_record = assistant_with_tc[0]
        assert isinstance(first_tc_record.tool_calls, list), f"tool_calls should be list, got {type(first_tc_record.tool_calls)}"
        assert len(first_tc_record.tool_calls) >= 1, "tool_calls list is empty"
        first_tc = first_tc_record.tool_calls[0]
        assert "function" in first_tc, f"tool_call missing 'function' key: {first_tc}"
        assert first_tc["function"]["name"] == "echo", f"Expected tool name 'echo', got {first_tc['function']['name']}"

        # 验证有 tool 记录
        tool_records = [m for m in all_messages if m.role == "tool"]
        assert len(tool_records) >= 1, f"Expected at least 1 tool record in DB, got {len(tool_records)}"

        # 验证 tool 记录有 tool_call_id
        for i, record in enumerate(tool_records):
            assert record.tool_call_id, f"Tool record #{i} has empty tool_call_id"

        # 验证 tool_call_id 关联：assistant 的 tool_calls[].id 应与 tool 的 tool_call_id 匹配
        assistant_tc_ids = set()
        for m in all_messages:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    assistant_tc_ids.add(tc.get("id", ""))
        tool_tc_ids = {m.tool_call_id for m in tool_records}
        # 至少有一个 tool_call_id 匹配
        overlap = assistant_tc_ids & tool_tc_ids
        assert len(overlap) >= 1, (
            f"No matching tool_call_id between assistant and tool records. "
            f"Assistant TC IDs: {assistant_tc_ids}, Tool TC IDs: {tool_tc_ids}"
        )

        # 验证有纯文本 assistant 记录（最终回复）
        assistant_plain = [m for m in all_messages if m.role == "assistant" and not m.tool_calls]
        assert len(assistant_plain) >= 1, (
            f"Expected at least 1 plain assistant record (final reply), got {len(assistant_plain)}. "
            f"All assistant records: {[(bool(m.tool_calls), m.content[:50] if m.content else '') for m in all_messages if m.role == 'assistant']}"
        )
        # 纯文本 assistant 记录的 content 不为空
        for i, record in enumerate(assistant_plain):
            assert record.content.strip(), f"Plain assistant record #{i} has empty content"

        print(f"\n[Scenario3b] PASSED: {len(all_messages)} records in DB")
        print(f"  assistant with tool_calls: {len(assistant_with_tc)}")
        print(f"  assistant plain text: {len(assistant_plain)}")
        print(f"  tool records: {len(tool_records)}")
        print(f"  tool_call_id overlap: {overlap}")
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
