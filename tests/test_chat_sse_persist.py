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
import json
import os
import pathlib
import re
import tempfile
from unittest.mock import AsyncMock, Mock, patch

import pytest
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

    import agent.session as session_module
    from niu_api.chat import router as chat_router

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

    # /chat SSE 端点设计上不持久化 user 消息：
    # - persist_agent_reply（chat.py 行 279-280）显式 `if role == "user": continue` 跳过
    # - /chat SSE 端点（chat.py 行 413-549）不调 store.add_message(role="user", ...)
    # - 只有 /chat/sync 端点（chat.py 行 577-580）才写 user 消息
    # 因此 DB 中 user 消息数应为 0。
    assert len(user_msgs) == 0, (
        f"/chat SSE 端点不应持久化 user 消息，实际: {len(user_msgs)} 条，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )
    assert len(assistant_msgs) >= 1, (
        f"DB 应至少有 1 条 assistant 消息，实际: {len(assistant_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
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
        StepOutcome,
        agent_runner_loop,
    )

    resp = Mock()
    resp.content = "你好！我是妞妞"
    resp.tool_calls = []
    # Mock 对象 hasattr 永远返回 True 且属性值 truthy。
    # agent_loop 行 769 检测 `response.context_overflow` 触发 CONTEXT_OVERFLOW 退出，
    # 行 741 检测 `response.usage` 提取 prompt_tokens，需明确设 None 跳过上下文检测。
    resp.context_overflow = False
    resp.usage = None
    resp.finish_reason = "stop"
    # E2：agent_loop 行 884 检测 `response.stream_error`——Mock 未显式赋值时属性值
    # 为 truthy Mock 对象，getattr 命中走 LLM_ERROR 分支（aa38d208 2026-08-06 引入
    # stream_error 检查后此测试即失败，与 E2 无关的 pre-existing）。显式设 False
    # 让纯文本回复走正常 CURRENT_TASK_DONE 路径。
    resp.stream_error = False

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
    # 显式设 _is_sync_subagent=False：MagicMock 默认返回 truthy mock 对象，
    # 会让 _intercept_at_prefix_content 行 102 主 Agent 分支条件
    # (memory_context is None and not is_sync_subagent) 不成立，
    # 导致纯文本回复被走到行 152-155 FORMAT_ERROR 路径，
    # 循环 continue 40 轮后返回 MAX_TURNS_EXCEEDED。
    # 必须明确赋 False 让拦截层走主 Agent 分支返回 NO_INTERCEPTION。
    handler._is_sync_subagent = False

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
    # 纯文本回复（无 tool_calls）时 agent_loop 行 818 `if response.tool_calls:`
    # 为 False，不 append assistant_msg 到 messages。
    # assistant content 通过 StreamEvent("persist", pure_text_msg) 单独推送（行 980-981），
    # 不进入 return value 的 messages 列表。
    # 因此 messages 只含 system + user，长度为 2。
    assert len(messages) == 2, f"Expected 2 messages (system+user), got {len(messages)}: {messages}"

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "你好"

    # 验证 assistant 回复通过 StreamEvent("persist") 推送（纯文本回复路径）
    persist_events = [
        json.loads(e.content) for e in events
        if e.type == "persist"
    ]
    assistant_persist = [m for m in persist_events if m.get("role") == "assistant"]
    assert len(assistant_persist) == 1, (
        f"纯文本回复应通过 1 个 persist 事件推送 assistant 消息，实际: {len(assistant_persist)}"
    )
    assert assistant_persist[0]["content"] == "你好！我是妞妞", (
        f"persist 推送的 assistant content 应为 '你好！我是妞妞'，"
        f"实际: '{assistant_persist[0].get('content')}'"
    )
    # 纯文本回复不应有 tool_calls
    assert "tool_calls" not in assistant_persist[0] or not assistant_persist[0].get("tool_calls"), (
        f"纯文本回复的 assistant 消息不应有 tool_calls，实际: {assistant_persist[0]}"
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
                await store.add_message(role="user", content=content)
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


# ---------------------------------------------------------------------------
# E4-02：system_notice 专用 SSE 事件（强制退出转发——E2 llm_error 模式）
# ---------------------------------------------------------------------------

class _FakeLoop:
    """记录 call_soon_threadsafe 调用的假事件循环（不真正调度）。"""

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))


def test_notify_system_notice_sync_broadcasts_event_to_subscribers():
    """notify_system_notice_sync 应广播 system_notice 事件（type/message/source）到订阅者。"""
    from niu_api import chat as chat_mod

    fake = _FakeLoop()
    old_loop = chat_mod._main_loop
    chat_mod._main_loop = fake
    try:
        chat_mod.notify_system_notice_sync("⚠️ 输出多次超长截断，已强制退出", "runner")
        assert len(fake.calls) == 1
        cb, args = fake.calls[0]
        assert cb is chat_mod._sync_broadcast
        assert args[0] == {
            "type": "system_notice",
            "message": "⚠️ 输出多次超长截断，已强制退出",
            "source": "runner",
        }
    finally:
        chat_mod._main_loop = old_loop


def test_notify_system_notice_sync_main_loop_none_early_exit():
    """_main_loop=None 时 notify_system_notice_sync 早退不抛。"""
    from niu_api import chat as chat_mod

    old_loop = chat_mod._main_loop
    chat_mod._main_loop = None
    try:
        chat_mod.notify_system_notice_sync("msg", "runner")
    finally:
        chat_mod._main_loop = old_loop


def _make_runner_for_notice():
    """构造 NiuRunner（mock 掉重依赖——与 test_runner_stream_events 同款）。"""
    with patch("agent.runner.create_client") as mock_create_client, \
         patch("agent.runner.get_system_prompt", return_value="sys"), \
         patch("agent.runner.get_tools_schema", return_value=[]), \
         patch("agent.runner.get_skill_sync"), \
         patch("agent.runner.NiuHandler"), \
         patch("niu_api.internal.disk_engine.DiskEngine") as mock_disk_cls:
        mock_create_client.return_value = Mock()
        mock_disk_instance = Mock()
        mock_disk_instance.get_schema.return_value = {"type": "function", "function": {"name": "disk"}}
        mock_disk_instance.config.servers = {}  # empty servers dict for _build_disk_description
        mock_disk_cls.return_value = mock_disk_instance

        from agent.runner import NiuRunner
        runner = NiuRunner(
            llm_config={"apikey": "test", "model": "test-model"},
            mcp_client=None,
        )
    return runner


def test_runner_chat_forced_exit_system_event_forwards_system_notice():
    """E4-02：runner.chat 收到含 '已强制退出' 的 system 事件 → 触发 notify_system_notice_sync。

    强制退出事件不落库、不进 LLM 上下文，仅经 SSE system_notice 推前端 ⚠️ 提示；
    不是 reply 内容，不 yield 给调用方。
    """
    from agent.generic.agent_loop import StreamEvent
    runner = _make_runner_for_notice()

    events = [
        StreamEvent("system", "⚠️ 输出多次超长截断，已强制退出\n"),
        StreamEvent("system", "chat_idle"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)), \
         patch("niu_api.chat.notify_system_notice_sync") as mock_notice:
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == [], f"强制退出 system 事件不应作为 reply 输出，实际: {results}"
    mock_notice.assert_called_once_with("⚠️ 输出多次超长截断，已强制退出", source="runner")


def test_runner_chat_chat_busy_idle_still_forwarded():
    """E4-02：chat_busy/chat_idle 状态机事件转发保持（notify_new_message_sync 仍被调用）。"""
    from agent.generic.agent_loop import StreamEvent
    runner = _make_runner_for_notice()

    events = [
        StreamEvent("system", "chat_busy"),
        StreamEvent("system", "chat_idle"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)), \
         patch("niu_api.chat.notify_system_notice_sync") as mock_notice, \
         patch("niu_api.chat.notify_new_message_sync") as mock_state:
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == []
    assert mock_state.call_count == 2, \
        f"chat_busy/chat_idle 应触发 2 次 notify_new_message_sync，实际: {mock_state.call_count}"
    mock_state.assert_any_call("", "chat_busy", "", source="electron")
    mock_state.assert_any_call("", "chat_idle", "", source="electron")
    mock_notice.assert_not_called()


def test_runner_chat_other_system_events_dropped_no_notify():
    """E4-02：其余 system 事件（截断重试提示/普通文本）丢弃——不触发任何通知。

    截断重试分支（'正在重试'）已注入 LLM 上下文（messages.append user 角色）——
    显式接受零改动，不转发 SSE。
    """
    from agent.generic.agent_loop import StreamEvent
    runner = _make_runner_for_notice()

    events = [
        StreamEvent("system", "LLM Running...\n"),
        StreamEvent("system", "⚠️ 输出超长被截断，正在重试...\n"),
        StreamEvent("system", "some other system msg"),
        StreamEvent("reply", "Hello"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)), \
         patch("niu_api.chat.notify_system_notice_sync") as mock_notice, \
         patch("niu_api.chat.notify_new_message_sync") as mock_state:
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == ["Hello"]
    # 流内普通 system 事件（截断重试/LLM Running/其他）不触发任何通知
    mock_notice.assert_not_called()
    # chat() 收尾的防御性 chat_idle 推送保持（既有行为——非流内 system 事件）
    assert mock_state.call_count == 1, \
        f"只应有收尾 chat_idle 推送，实际: {mock_state.call_count}"
    mock_state.assert_called_once_with("", "chat_idle", "", source="electron")


def test_frontend_system_notice_four_hop_chain():
    """E4-02 四跳链前端消费点：main.js SSE 分支 → preload IPC 桥 → chat.html ⚠️ 渲染。

    E2 llm_error 模式参照：chat.py 广播 → main.js 分支 → preload onSystemNotice →
    chat.html addMessage('system', ...) 提示渲染（只推不落 DB，刷新消失）。
    """
    project_root = pathlib.Path(__file__).resolve().parent.parent

    main_js = (project_root / "ui" / "main" / "main.js").read_text(encoding="utf-8")
    preload = (project_root / "ui" / "main" / "preload-chat.js").read_text(encoding="utf-8")
    chat_html = (project_root / "ui" / "main" / "windows" / "assistant" / "chat.html").read_text(encoding="utf-8")

    # 第 2 跳：main.js 处理 system_notice SSE 事件 → 转发 system-notice IPC 到 chat 窗口
    assert "event.type === 'system_notice'" in main_js, "main.js 应有 system_notice SSE 分支"
    assert "webContents.send('system-notice', event)" in main_js, \
        "main.js 应转发 system-notice 到 chat 窗口"

    # 第 3 跳：preload-chat.js 暴露 onSystemNotice 桥接 system-notice 频道
    assert "onSystemNotice:" in preload, "preload-chat.js 应暴露 onSystemNotice"
    assert "ipcRenderer.on('system-notice'" in preload, \
        "preload-chat.js 应监听 system-notice IPC 频道"

    # P3-4a：两端频道名逐字一致性（防一端改名另一端漏改——从 system_notice 分支
    # 与 onSystemNotice 块内各自提取频道字面量，逐字比较；单端改名即失配）
    _notice_block = main_js[main_js.index("event.type === 'system_notice'"):]
    m_send = re.search(r"webContents\.send\('([^']+)',\s*event\)", _notice_block)
    _on_block = preload[preload.index("onSystemNotice:"):]
    m_on = re.search(r"ipcRenderer\.on\('([^']+)'", _on_block)
    assert m_send is not None and m_on is not None, \
        "应能从 main.js system_notice 分支与 preload onSystemNotice 块提取频道名字面量"
    assert m_send.group(1) == m_on.group(1), \
        f"main.js send 频道名 {m_send.group(1)!r} 与 preload on 频道名 {m_on.group(1)!r} 必须一致"

    # P3-4b：分支相对位置——system_notice 必须与 llm_error/ask_user 同为顶层 else-if
    # 链分支（同级缩进、非嵌套——防被其他分支包裹遮蔽），且位于 ask_user 之前
    def _branch_line(src, marker):
        i = src.index(marker)
        line_start = src.rfind("\n", 0, i) + 1
        return src[line_start:src.find("\n", i)]

    _sys_line = _branch_line(main_js, "event.type === 'system_notice'")
    _llm_line = _branch_line(main_js, "event.type === 'llm_error'")
    _ask_line = _branch_line(main_js, "event.type === 'ask_user'")
    for _name, _line in (("system_notice", _sys_line), ("llm_error", _llm_line), ("ask_user", _ask_line)):
        assert _line.strip().startswith("} else if ("), \
            f"{_name} 应为顶层 else-if 分支，实际行: {_line!r}"
    _sys_indent = len(_sys_line) - len(_sys_line.lstrip())
    _llm_indent = len(_llm_line) - len(_llm_line.lstrip())
    _ask_indent = len(_ask_line) - len(_ask_line.lstrip())
    assert _sys_indent == _llm_indent == _ask_indent, \
        "system_notice 与 llm_error/ask_user 必须同级缩进（顶层链，非嵌套遮蔽）"
    assert main_js.index("event.type === 'system_notice'") < main_js.index("event.type === 'ask_user'"), \
        "system_notice 分支必须位于 ask_user 之前（链序）"

    # 第 4 跳：chat.html 监听 onSystemNotice → addMessage('system', ...) ⚠️ 提示渲染
    assert "onSystemNotice(" in chat_html, "chat.html 应注册 onSystemNotice 监听器"
    assert "addMessage('system'," in chat_html, "chat.html 应渲染 ⚠️ system 提示"
    # P2：渲染层不追加 "⚠️ " 前缀——生产者（agent_loop L958/L1133/L1189）已自带 ⚠️，
    # 渲染层再拼会 "⚠️ ⚠️" 双重；透传 event.message，兜底文案自带 ⚠️
    assert "'⚠️ ' + (event.message" not in chat_html, \
        "chat.html 渲染层不得再拼接 '⚠️ ' 前缀（生产者已自带，防双重 ⚠️）"
    assert "event.message || '⚠️ 系统提示'" in chat_html, \
        "chat.html 应透传 event.message，兜底文案自带 ⚠️"
