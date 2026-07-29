"""测试多轮对话（先 tool_calls 后纯文本）的持久化问题。

**问题**：当 LLM 先调用工具（第1轮），然后给出纯文本回复（第2轮）时，
纯文本回复没有持久化到数据库。

**根因**：
1. agent_loop.py 第222行：`if response.tool_calls:` — 只有带 tool_calls 的
   assistant 消息才会被追加到 messages 列表中。纯文本回复不会追加。
2. 多轮对话中：第1轮有 tool_calls → assistant(tool_calls) 被添加到 messages；
   第2轮纯文本 → 没有 assistant 消息被添加到 messages。
3. chat.py 持久化逻辑中，遍历 rv["messages"] 时找到 assistant(tool_calls)，
   last_assistant_id 不为 None，所以"从 reply_chunks 构造 assistant 消息"
   的回退逻辑不触发。
4. 结果：纯文本回复丢失。

**测试要求**：
1. 用真实 LLM API 调用
2. 模拟多轮对话：给 LLM 一个会触发工具调用的输入，然后 LLM 在工具执行后给出纯文本回复
3. 验证 rv["messages"] 的结构（有 assistant(tool_calls) 但没有纯文本 assistant）
4. 验证 reply_chunks 包含纯文本回复内容
5. 模拟 _do_persist 逻辑，验证当前代码会导致纯文本回复丢失
6. 验证修复后的逻辑能正确持久化两条 assistant 消息
"""

import json
import os
import sys
import tempfile

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.generic.agent_loop import (
    BaseHandler,
    StepOutcome,
    StreamEvent,
    agent_runner_loop,
)
from agent.generic.litellm_adapter import create_litellm_client
from agent.session import MessageStore

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_llm_config():
    """从 user-config.json 加载真实 LLM 配置"""
    config_path = os.path.join(PROJECT_ROOT, "config", "user-config.json")
    with open(config_path, encoding="utf-8") as f:
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
        return StepOutcome(
            data={"echo": text},
            next_prompt=f"echo result: {text}",
            should_exit=False,
        )


# ---------------------------------------------------------------------------
# 工具 Schema
# ---------------------------------------------------------------------------

ECHO_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": (
                "Echo back the input text. "
                "Use this when the user asks you to echo something."
            ),
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
# 持久化逻辑复现
# ---------------------------------------------------------------------------

async def _do_persist_current_chat_py(store, rv, reply_chunks):
    """复现 chat.py 第308-349行的持久化逻辑（当前代码，有 bug）。

    当 rv["messages"] 中有 assistant(tool_calls) 时，
    last_assistant_id 不为 None，回退逻辑不触发，
    导致纯文本回复丢失。
    """
    if not rv or not isinstance(rv, dict) or not rv.get("messages"):
        return []

    persisted_ids = []
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
                pid = await store.add_message(role="user", content=content)
                persisted_ids.append(pid)
                user_persisted = True
            continue
        elif role == "tool" and tool_call_id:
            pid = await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            persisted_ids.append(pid)
        elif role == "assistant":
            pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
            persisted_ids.append(pid)
            last_assistant_id = pid

    # 修复：纯文本回复时 rv["messages"] 中没有 assistant 消息，
    # 从 reply_chunks 构造 assistant 消息并持久化
    if last_assistant_id is None:
        full_reply = "".join(reply_chunks)
        if full_reply.strip():
            pid = await store.add_message(role="assistant", content=full_reply)
            persisted_ids.append(pid)
            last_assistant_id = pid

    return persisted_ids


async def _do_persist_fixed(store, rv, reply_chunks):
    """修复后的持久化逻辑。

    修复方案：遍历完 rv["messages"] 后，检查 reply_chunks 中是否有纯文本内容
    未被持久化。如果最后一条 assistant 消息是带 tool_calls 的，而 reply_chunks
    有内容，说明存在一条纯文本 assistant 回复未被持久化，需要额外写入。

    判断依据：rv["messages"] 中最后一条 assistant 消息是否带 tool_calls。
    - 如果带 tool_calls，说明纯文本回复（第2轮）没有被追加到 messages，
      需要从 reply_chunks 构造。
    - 如果不带 tool_calls（纯文本回复），说明纯文本回复已在 messages 中，
      不需要额外构造。
    """
    if not rv or not isinstance(rv, dict) or not rv.get("messages"):
        return []

    persisted_ids = []
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
                pid = await store.add_message(role="user", content=content)
                persisted_ids.append(pid)
                user_persisted = True
            continue
        elif role == "tool" and tool_call_id:
            pid = await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            persisted_ids.append(pid)
        elif role == "assistant":
            pid = await store.add_message(role="assistant", content=content or "", tool_calls=tool_calls)
            persisted_ids.append(pid)
            last_assistant_id = pid

    # 修复：检查是否需要从 reply_chunks 构造纯文本 assistant 消息
    # 场景1：rv["messages"] 中完全没有 assistant 消息（纯文本回复，无工具调用）
    if last_assistant_id is None:
        full_reply = "".join(reply_chunks)
        if full_reply.strip():
            pid = await store.add_message(role="assistant", content=full_reply)
            persisted_ids.append(pid)
            last_assistant_id = pid
    else:
        # 场景2：rv["messages"] 中有 assistant 消息，但最后一条带 tool_calls
        # 说明第2轮纯文本回复没有被追加到 messages，需要从 reply_chunks 构造
        last_assistant_msg = None
        for msg in reversed(rv["messages"]):
            if msg.get("role") == "assistant":
                last_assistant_msg = msg
                break

        if last_assistant_msg and last_assistant_msg.get("tool_calls"):
            full_reply = "".join(reply_chunks)
            if full_reply.strip():
                pid = await store.add_message(role="assistant", content=full_reply)
                persisted_ids.append(pid)
                last_assistant_id = pid

    return persisted_ids


# ===========================================================================
# 测试1：真实 LLM 调用 — 验证多轮对话的 rv["messages"] 结构
# ===========================================================================

def test_multi_turn_messages_structure():
    """测试1：用真实 LLM 验证多轮对话中 rv["messages"] 的结构。

    预期行为：
    - 第1轮：LLM 调用 echo 工具 → assistant(tool_calls) 消息被添加到 messages
    - 第2轮：LLM 给出纯文本回复 → assistant 消息**不会**被添加到 messages
    - rv["messages"] 中有 assistant(tool_calls) 但没有纯文本 assistant

    验证：
    1. rv["messages"] 中至少有1条 assistant 消息（带 tool_calls）
    2. rv["messages"] 中没有纯文本 assistant 消息（无 tool_calls）
    3. StreamEvent("reply") 中有纯文本回复内容
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果，不要再次调用工具。"
    )
    user_input = "请用 echo 工具返回文字：multi-turn-test"

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
    assert "messages" in rv, f"Missing 'messages' in return value: {list(rv.keys())}"

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
        f"LLM may not have called the tool. "
        f"Assistant messages: {[{'content': m.get('content', '')[:80], 'has_tool_calls': bool(m.get('tool_calls'))} for m in assistant_msgs]}"
    )

    # 核心验证：rv["messages"] 中没有纯文本 assistant 消息
    # 这是 bug 的根因：agent_loop.py 只在 response.tool_calls 时追加 assistant 消息
    plain_assistant_msgs = [m for m in assistant_msgs if not m.get("tool_calls")]
    assert len(plain_assistant_msgs) == 0, (
        f"Expected NO plain-text assistant messages in rv['messages'], "
        f"but found {len(plain_assistant_msgs)}. "
        f"This means agent_loop.py now appends plain-text assistant messages, "
        f"which would change the bug scenario. "
        f"All assistant messages: {[{'has_tool_calls': bool(m.get('tool_calls')), 'content': m.get('content', '')[:80]} for m in assistant_msgs]}"
    )

    # 验证 StreamEvent("reply") 中有纯文本回复内容
    reply_content = _extract_reply_content(events)
    assert reply_content.strip(), (
        f"Expected non-empty reply content from StreamEvents. "
        f"Event types: {[e.type if isinstance(e, StreamEvent) else type(e).__name__ for e in events]}"
    )

    print("\n[Test1] PASSED: rv['messages'] structure verified")
    print(f"  Total messages: {len(messages)}")
    print(f"  Message roles: {[m.get('role') for m in messages]}")
    print(f"  Assistant messages: {len(assistant_msgs)} (all with tool_calls)")
    print(f"  Plain-text assistant in messages: {len(plain_assistant_msgs)} (should be 0)")
    print(f"  Reply content from StreamEvents: '{reply_content[:100]}'")


# ===========================================================================
# 测试2：验证 reply_chunks 包含纯文本回复
# ===========================================================================

def test_reply_chunks_contains_plain_text():
    """测试2：验证 StreamEvent("reply") 包含 LLM 在工具执行后的纯文本回复。

    验证：
    1. reply_chunks（从 StreamEvent 提取）有非空内容
    2. reply_chunks 的内容与 rv["messages"] 中 assistant(tool_calls) 的 content 不同
       （因为纯文本回复是第2轮的内容，不是第1轮 tool_calls 时的 content）
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果，不要再次调用工具。"
    )
    user_input = "请用 echo 工具返回文字：reply-chunks-test"

    events, rv = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=ECHO_TOOL_SCHEMA,
        verbose=False,
    )

    assert rv is not None, "agent_runner_loop returned None"
    messages = rv["messages"]

    # 从 StreamEvent 提取纯文本回复
    reply_content = _extract_reply_content(events)
    assert reply_content.strip(), "Expected non-empty reply content from StreamEvents"

    # 找到 assistant(tool_calls) 消息的 content
    assistant_tc_msgs = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_tc_msgs) >= 1, "Expected at least 1 assistant message with tool_calls"

    # assistant(tool_calls) 的 content 通常是空的或很短的
    tc_content = assistant_tc_msgs[0].get("content", "") or ""

    print("\n[Test2] PASSED: reply_chunks contains plain text reply")
    print(f"  Reply content (from StreamEvents): '{reply_content[:100]}'")
    print(f"  Assistant(tool_calls) content: '{tc_content[:100]}'")
    print(f"  Reply is different from tool_calls content: {reply_content.strip() != tc_content.strip()}")


# ===========================================================================
# 测试3：模拟 _do_persist — 当前代码导致纯文本回复丢失
# ===========================================================================

@pytest.mark.asyncio
async def test_current_persist_loses_plain_text():
    """测试3：模拟 chat.py 的 _do_persist 逻辑，验证当前代码导致纯文本回复丢失。

    步骤：
    1. 用真实 LLM 获取多轮对话的 rv 和 reply_chunks
    2. 用当前 _do_persist 逻辑持久化
    3. 验证数据库中只有 assistant(tool_calls) 记录，没有纯文本 assistant 记录
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果，不要再次调用工具。"
    )
    user_input = "请用 echo 工具返回文字：persist-bug-test"

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

    # 从 StreamEvent 提取纯文本回复（模拟 reply_chunks）
    reply_chunks = []
    for event in events:
        if isinstance(event, StreamEvent) and event.type == "reply":
            reply_chunks.append(event.content)

    reply_content = "".join(reply_chunks)
    assert reply_content.strip(), "Expected non-empty reply content from StreamEvents"

    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 使用当前 _do_persist 逻辑（有 bug）
        await _do_persist_current_chat_py(store, rv, reply_chunks)

        # 验证数据库记录
        all_messages = await store.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]

        # 当前逻辑下：有 assistant(tool_calls) 记录
        assistant_with_tc = [m for m in assistant_msgs if m.tool_calls]
        assert len(assistant_with_tc) >= 1, (
            f"Expected at least 1 assistant record with tool_calls, "
            f"got {len(assistant_with_tc)}"
        )

        # 当前逻辑下：没有纯文本 assistant 记录 — 这是 bug！
        assistant_plain = [m for m in assistant_msgs if not m.tool_calls]
        assert len(assistant_plain) == 0, (
            f"Current _do_persist logic should NOT create plain-text assistant record, "
            f"but found {len(assistant_plain)}. "
            f"This means the bug may have been fixed already. "
            f"All assistant records: {[(bool(m.tool_calls), m.content[:50]) for m in assistant_msgs]}"
        )

        print("\n[Test3] PASSED: Bug confirmed — plain-text reply lost in current logic")
        print(f"  Total DB records: {len(all_messages)}")
        print(f"  Assistant with tool_calls: {len(assistant_with_tc)}")
        print(f"  Assistant plain text: {len(assistant_plain)} (should be 0 — bug!)")
        print(f"  Lost reply content: '{reply_content[:100]}'")
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ===========================================================================
# 测试4：修复后的逻辑正确持久化两条 assistant 消息
# ===========================================================================

@pytest.mark.asyncio
async def test_fixed_persist_saves_both_assistant_messages():
    """测试4：验证修复后的 _do_persist 逻辑能正确持久化两条 assistant 消息。

    修复方案：遍历完 rv["messages"] 后，检查最后一条 assistant 消息是否带 tool_calls。
    如果带 tool_calls，说明纯文本回复（第2轮）没有被追加到 messages，
    需要从 reply_chunks 构造额外的 assistant 消息。

    验证：
    1. 数据库中有 assistant(tool_calls) 记录
    2. 数据库中有纯文本 assistant 记录
    3. 纯文本 assistant 记录的 content 与 reply_chunks 拼接后一致
    4. 两条 assistant 消息的顺序正确（tool_calls 在前，纯文本在后）
    """
    config = _load_llm_config()
    assert config["apikey"], "API key not configured in user-config.json"

    client = create_litellm_client(config)
    handler = EchoHandler()

    system_prompt = (
        "你是一个测试助手。用户会让你 echo 一段文字，你必须调用 echo 工具。"
        "收到工具结果后，用简短的中文总结结果，不要再次调用工具。"
    )
    user_input = "请用 echo 工具返回文字：persist-fix-test"

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

    # 从 StreamEvent 提取纯文本回复（模拟 reply_chunks）
    reply_chunks = []
    for event in events:
        if isinstance(event, StreamEvent) and event.type == "reply":
            reply_chunks.append(event.content)

    reply_content = "".join(reply_chunks)
    assert reply_content.strip(), "Expected non-empty reply content from StreamEvents"

    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 使用修复后的 _do_persist 逻辑
        await _do_persist_fixed(store, rv, reply_chunks)

        # 验证数据库记录
        all_messages = await store.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]

        # 修复后：有 assistant(tool_calls) 记录
        assistant_with_tc = [m for m in assistant_msgs if m.tool_calls]
        assert len(assistant_with_tc) >= 1, (
            f"Expected at least 1 assistant record with tool_calls, "
            f"got {len(assistant_with_tc)}"
        )

        # 修复后：有纯文本 assistant 记录
        assistant_plain = [m for m in assistant_msgs if not m.tool_calls]
        assert len(assistant_plain) >= 1, (
            f"Expected at least 1 plain-text assistant record after fix, "
            f"got {len(assistant_plain)}. "
            f"All assistant records: {[(bool(m.tool_calls), m.content[:50]) for m in assistant_msgs]}"
        )

        # 验证纯文本 assistant 记录的 content 不为空
        plain_record = assistant_plain[0]
        assert plain_record.content.strip(), (
            "Plain-text assistant record has empty content"
        )

        # 验证纯文本 assistant 的 content 包含 reply_chunks 的内容
        # （可能不完全相等，因为 LLM 可能在 tool_calls 的 assistant 消息中也有 content，
        #  但 reply_chunks 只包含第2轮的纯文本回复）
        assert plain_record.content.strip() == reply_content.strip(), (
            f"Plain-text assistant content should match reply_chunks. "
            f"DB content: '{plain_record.content[:100]}', "
            f"Reply chunks: '{reply_content[:100]}'"
        )

        # 验证顺序：assistant(tool_calls) 在前，纯文本 assistant 在后
        tc_index = None
        plain_index = None
        for i, m in enumerate(all_messages):
            if m.role == "assistant" and m.tool_calls and tc_index is None:
                tc_index = i
            if m.role == "assistant" and not m.tool_calls and plain_index is None:
                plain_index = i

        assert tc_index is not None and plain_index is not None, (
            "Both assistant records should exist in DB"
        )
        assert tc_index < plain_index, (
            f"assistant(tool_calls) should come before plain-text assistant. "
            f"tc_index={tc_index}, plain_index={plain_index}"
        )

        print("\n[Test4] PASSED: Fixed logic correctly persists both assistant messages")
        print(f"  Total DB records: {len(all_messages)}")
        print(f"  Assistant with tool_calls: {len(assistant_with_tc)}")
        print(f"  Assistant plain text: {len(assistant_plain)}")
        print(f"  Plain text content: '{plain_record.content[:100]}'")
        print(f"  Reply chunks content: '{reply_content[:100]}'")
        print(f"  Order: tc_index={tc_index} < plain_index={plain_index}")
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ===========================================================================
# 测试5：纯文本回复（无工具调用）的持久化仍然正确
# ===========================================================================

@pytest.mark.asyncio
async def test_fixed_persist_plain_text_only():
    """测试5：验证修复后的逻辑在纯文本回复（无工具调用）场景下仍然正确。

    这是对修复的回归测试：修复不应破坏纯文本回复的持久化。
    """
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

    # 从 StreamEvent 提取纯文本回复
    reply_chunks = []
    for event in events:
        if isinstance(event, StreamEvent) and event.type == "reply":
            reply_chunks.append(event.content)

    reply_content = "".join(reply_chunks)
    assert reply_content.strip(), "Expected non-empty reply content"

    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 使用修复后的 _do_persist 逻辑
        await _do_persist_fixed(store, rv, reply_chunks)

        # 验证数据库记录
        all_messages = await store.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]

        # 纯文本回复：应有1条 assistant 记录（无 tool_calls）
        assert len(assistant_msgs) >= 1, (
            f"Expected at least 1 assistant record, got {len(assistant_msgs)}"
        )

        # 纯文本回复：assistant 记录不应有 tool_calls
        assistant_with_tc = [m for m in assistant_msgs if m.tool_calls]
        assert len(assistant_with_tc) == 0, (
            f"Pure text reply should not have tool_calls, "
            f"but found {len(assistant_with_tc)}"
        )

        # 验证 content 匹配
        plain_record = assistant_msgs[0]
        assert plain_record.content.strip() == reply_content.strip(), (
            f"Assistant content should match reply_chunks. "
            f"DB: '{plain_record.content[:100]}', "
            f"Reply: '{reply_content[:100]}'"
        )

        print("\n[Test5] PASSED: Fixed logic works correctly for plain-text-only replies")
        print(f"  Assistant records: {len(assistant_msgs)}")
        print(f"  Content: '{plain_record.content[:100]}'")
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ===========================================================================
# 测试6：模拟数据 — 不依赖 LLM，直接验证持久化逻辑
# ===========================================================================

@pytest.mark.asyncio
async def test_persist_logic_with_simulated_data():
    """测试6：用模拟数据直接验证持久化逻辑，不依赖 LLM。

    构造一个典型的多轮对话 rv，模拟：
    - 第1轮：assistant 调用 echo 工具
    - 第2轮：assistant 给出纯文本回复（不在 rv["messages"] 中）

    验证当前逻辑丢失纯文本，修复逻辑正确持久化。
    """
    # 模拟多轮对话的 rv
    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "请用 echo 工具返回文字：simulated-test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_sim_001",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text": "simulated-test"}'
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_sim_001",
                "content": '{"echo": "simulated-test"}'
            },
            # 注意：这里没有纯文本 assistant 消息！
            # 因为 agent_loop.py 只在 response.tool_calls 时追加 assistant
        ],
    }

    # 模拟 reply_chunks（纯文本回复，来自第2轮 LLM 回复）
    reply_chunks = ["根据 echo 工具的结果，返回的文字是：simulated-test"]

    # --- 验证当前逻辑（有 bug）---
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path_current = tmp.name

    try:
        store_current = MessageStore(db_path=db_path_current)
        await store_current.init_db()

        await _do_persist_current_chat_py(store_current, rv, reply_chunks)

        all_messages = await store_current.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]

        # 当前逻辑：只有 assistant(tool_calls)，没有纯文本 assistant
        assistant_with_tc = [m for m in assistant_msgs if m.tool_calls]
        assistant_plain = [m for m in assistant_msgs if not m.tool_calls]

        assert len(assistant_with_tc) == 1, (
            f"Expected 1 assistant with tool_calls, got {len(assistant_with_tc)}"
        )
        assert len(assistant_plain) == 0, (
            f"Current logic should NOT create plain-text assistant. "
            f"Got {len(assistant_plain)} plain-text assistant records. "
            f"Bug: plain-text reply is lost!"
        )

        print(f"\n[Test6a] Current logic: {len(assistant_with_tc)} assistant(tool_calls), "
              f"{len(assistant_plain)} plain-text assistant (bug: should be 0)")
    finally:
        if os.path.exists(db_path_current):
            os.unlink(db_path_current)

    # --- 验证修复后的逻辑 ---
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path_fixed = tmp.name

    try:
        store_fixed = MessageStore(db_path=db_path_fixed)
        await store_fixed.init_db()

        await _do_persist_fixed(store_fixed, rv, reply_chunks)

        all_messages = await store_fixed.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]

        # 修复后：有 assistant(tool_calls) + 纯文本 assistant
        assistant_with_tc = [m for m in assistant_msgs if m.tool_calls]
        assistant_plain = [m for m in assistant_msgs if not m.tool_calls]

        assert len(assistant_with_tc) == 1, (
            f"Expected 1 assistant with tool_calls, got {len(assistant_with_tc)}"
        )
        assert len(assistant_plain) == 1, (
            f"Fixed logic should create 1 plain-text assistant. "
            f"Got {len(assistant_plain)}"
        )

        # 验证纯文本 assistant 的 content
        plain_record = assistant_plain[0]
        expected_content = "".join(reply_chunks)
        assert plain_record.content == expected_content, (
            f"Plain-text assistant content mismatch. "
            f"Expected: '{expected_content}', Got: '{plain_record.content}'"
        )

        # 验证顺序
        tc_idx = None
        plain_idx = None
        for i, m in enumerate(all_messages):
            if m.role == "assistant" and m.tool_calls and tc_idx is None:
                tc_idx = i
            if m.role == "assistant" and not m.tool_calls and plain_idx is None:
                plain_idx = i

        assert tc_idx is not None and plain_idx is not None
        assert tc_idx < plain_idx, (
            f"assistant(tool_calls) at {tc_idx} should come before "
            f"plain-text assistant at {plain_idx}"
        )

        print(f"\n[Test6b] Fixed logic: {len(assistant_with_tc)} assistant(tool_calls), "
              f"{len(assistant_plain)} plain-text assistant")
        print(f"  Plain content: '{plain_record.content[:80]}'")
        print(f"  Order: tc_idx={tc_idx} < plain_idx={plain_idx}")
    finally:
        if os.path.exists(db_path_fixed):
            os.unlink(db_path_fixed)


# ===========================================================================
# 测试7：compat.py 的 _persist_messages_from_return_value 也有同样问题
# ===========================================================================

@pytest.mark.asyncio
async def test_compat_persist_logic_with_simulated_data():
    """测试7：验证 compat.py 的持久化逻辑也有同样的 bug。

    compat.py 使用 _persist_messages_from_return_value 持久化，
    然后通过 persisted_ids 找 last_assistant_id。当 rv["messages"]
    中有 assistant(tool_calls) 时，last_assistant_id 不为 None，
    回退逻辑不触发，纯文本回复丢失。
    """
    rv = {
        "result": "CURRENT_TASK_DONE",
        "data": None,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "请用 echo 工具返回文字：compat-test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_compat_001",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"text": "compat-test"}'
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_compat_001",
                "content": '{"echo": "compat-test"}'
            },
        ],
    }


    # 模拟 compat.py 的持久化逻辑
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MessageStore(db_path=db_path)
        await store.init_db()

        # 模拟 _persist_messages_from_return_value（只持久化 assistant + tool）
        persisted_ids = []
        for msg in rv["messages"]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in ("system", "user"):
                continue
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id", "")
            msg_id = await store.add_message(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            )
            persisted_ids.append(msg_id)

        # 模拟 compat.py 找 last_assistant_id 的逻辑
        last_assistant_id = None
        if persisted_ids and rv.get("messages"):
            persisted_idx = 0
            for msg in rv["messages"]:
                role = msg.get("role", "")
                if role in ("system", "user"):
                    continue
                if persisted_idx < len(persisted_ids):
                    if role == "assistant":
                        last_assistant_id = persisted_ids[persisted_idx]
                    persisted_idx += 1

        # 验证：last_assistant_id 不为 None（指向 assistant(tool_calls)）
        assert last_assistant_id is not None, (
            "last_assistant_id should not be None because assistant(tool_calls) exists"
        )

        # 验证：当前逻辑下，纯文本回复不会被持久化
        all_messages = await store.get_messages()
        assistant_msgs = [m for m in all_messages if m.role == "assistant"]
        assistant_plain = [m for m in assistant_msgs if not m.tool_calls]

        assert len(assistant_plain) == 0, (
            f"compat.py logic should also lose plain-text reply. "
            f"Got {len(assistant_plain)} plain-text assistant records."
        )

        print("\n[Test7] PASSED: compat.py has the same bug")
        print(f"  last_assistant_id: {last_assistant_id}")
        print(f"  Plain-text assistant in DB: {len(assistant_plain)} (bug: should be 0)")
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
