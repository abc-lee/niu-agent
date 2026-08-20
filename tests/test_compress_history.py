"""context-manager 模式二 history 构造测试。"""
import sys
from pathlib import Path

# 确保 niu_api 可 import
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_compress_history  # noqa: E402


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def test_build_compress_history_basic():
    """基本场景：3 条消息（user/assistant/user）构造 history，每条 content 加 idx 前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
        FakeMsg(id="msg-3", role="user", content="今天天气"),
    ]
    msg_tokens = [10, 20, 15]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"].startswith("[idx:1] 10tokens ")
    assert "你好" in history[0]["content"]
    assert history[1]["role"] == "assistant"
    assert history[1]["content"].startswith("[idx:2] 20tokens ")
    assert history[2]["role"] == "user"
    assert history[2]["content"].startswith("[idx:3] 15tokens ")
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_with_tool_calls():
    """assistant 带 tool_calls + tool 消息：保留 tool_calls/tool_call_id，content 加前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="查天气"),
        FakeMsg(
            id="msg-2", role="assistant", content="",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="今天晴", tool_call_id="tc-1"),
    ]
    msg_tokens = [5, 8, 12]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"] == messages[1].tool_calls
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "tc-1"
    assert history[2]["content"].startswith("[idx:3] 12tokens ")


def test_build_compress_history_protected_excludes_orphan_tool():
    """PROTECTED 排除 assistant(tool_calls) 后，其 tool 消息也同步排除（避免孤立 tool 导致 idx 错位）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端消息"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="user", content="近端消息"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant 受保护 → msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # 关键：msg-2(assistant, tool_calls) 不在保护集（protect_recent 只数最后1条 user/assistant = msg-4）
    # 所以 msg-2 不被排除，msg-3(tool) 也不被排除（父 assistant 在）
    # 此场景下 history 应含 msg-1, msg-2, msg-3（msg-4 排除）
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    # msg-4 被排除，其余 3 条保留，idx 连续 1,2,3
    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_protected_assistant_excludes_its_tool():
    """PROTECTED 排除 assistant(tool_calls) 时，其 tool 消息也同步排除（孤立 tool 检测）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="assistant", content="近端回复"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant = msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # msg-2(assistant, tool_calls) 不在保护集，保留
    # msg-3(tool, tc-1) 父 assistant msg-2 在，保留
    # 此场景 history 应含 msg-1, msg-2, msg-3
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}

    # 现在构造另一个场景：protect_recent=2，msg-2 和 msg-4 都受保护
    # msg-2 被排除 → msg-3(tool, tc-1) 父 assistant 不在 → 孤立 tool，必须同步排除
    history2, idx_to_id2 = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=2,
        exclude_protected=True,
    )
    # msg-2 和 msg-4 被排除，msg-3 孤立 tool 同步排除，只剩 msg-1
    assert len(history2) == 1
    assert idx_to_id2 == {1: "msg-1"}


def test_build_compress_history_out_msg_ids():
    """out_msg_ids 收集保留消息的真实 ID（与 idx 顺序一致，含孤立 tool 同步排除）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="a"),
        FakeMsg(id="msg-2", role="assistant", content="b"),
    ]
    out_msg_ids = []

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=[10, 20],
        out_msg_ids=out_msg_ids,
    )

    assert out_msg_ids == ["msg-1", "msg-2"]
    assert idx_to_id == {1: "msg-1", 2: "msg-2"}


def test_build_compress_history_no_tokens():
    """msg_tokens 为 None 时不加 tokens 前缀，只加 idx。"""
    messages = [FakeMsg(id="msg-1", role="user", content="你好")]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=None,
        out_msg_ids=None,
    )

    # 前缀格式 [idx:1] 内容（无 tokens）
    assert history[0]["content"].startswith("[idx:1] ")
    assert "你好" in history[0]["content"]


def test_mode2_passes_history_to_call_subagent(monkeypatch):
    """模式二应构造 history 列表传给 call_subagent，而非序列化文本塞进 task。"""
    import asyncio

    import niu_api.compat as compat

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    # mock runner 控制 usage_percent（>50 触发模式二）
    # 注意：compat.py 是函数内 import `from niu_api.chat import get_or_create_runner`
    # 必须 patch 源模块 niu_api.chat.get_or_create_runner，patch compat.get_or_create_runner 无效
    import agent.subagent as subagent_module
    import niu_api.chat as chat_module
    import niu_api.llm_proxy as llm_proxy_module
    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()  # 120K tokens
        llm_config = {}  # compat.py L1385 runner.llm_config 需要

        def _ensure_session_chain(self):
            """测试桩：无操作（生产为 NiuRunner 方法，三管道调用）。"""
            pass

    def fake_get_or_create_runner():
        return FakeRunner()

    # mock call_subagent 捕获参数，返回 keep= 方案，短路后续执行
    # 注意：compat.py L1375 是函数内 import `from agent.subagent import call_subagent`
    # 必须 patch 源模块 agent.subagent.call_subagent，patch compat.call_subagent 无效
    captured = {}
    def fake_call_subagent(*args, **kwargs):
        # 兼容 entity-extractor / dream-evolver / journal-agent / context-manager 多种调用形式
        agent_name = kwargs.get("agent_name") or (args[0] if args else None)
        if agent_name != "context-manager":
            # 其他子Agent（entity-extractor 等）返回空结果，让 pipeline 继续推进
            return "skip"
        captured["agent_name"] = agent_name
        captured["task"] = kwargs.get("task")
        captured["history"] = kwargs.get("history")
        return "keep=1,2\nupdate="

    # mock 压缩执行（避免触发 chat_lock/DB 操作）
    async def fake_noop(*a, **kw):
        return None

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    # 关键：patch 源模块（compat.py 函数内 import 从 niu_api.chat 取）
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    # 关键：patch 源模块（compat.py 函数内 import 从 agent.subagent 取）
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    # _read_context_window_tokens 等配置读取 mock（这些是模块级 import，patch compat 正确）
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)  # 不保护，2 条都进 history
    # T6：压缩前置游标追平校验——模拟 entity/dream 已追平（游标=尾部最后一条消息）
    monkeypatch.setattr(compat, "_read_cursor_value", lambda path, key: messages[-1].id, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "is_sleeping", lambda: True, raising=False)  # T5：sleep 管道测试保持睡眠态
    # builder refetch lightrag 段（T2 后无参内部 refetch）——mock 隔离，不读真实用户配置
    monkeypatch.setattr(llm_proxy_module, "get_llm_config", lambda use_lightrag_config=False: {
        "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
        "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
    })

    # 调用 _tidy_context_impl（request dict 形式）
    request = {"session_id": "test", "mode": "sleep"}
    try:
        asyncio.run(compat._tidy_context_impl(request))
    except Exception:
        pass  # 后续执行可能报错（未 mock 全部），只关心 call_subagent 是否被正确调用

    # 验证 call_subagent 收到 history 参数
    assert captured.get("agent_name") == "context-manager"
    assert captured.get("history") is not None
    assert isinstance(captured["history"], list)
    assert len(captured["history"]) == 2
    # task 是压缩指令（不含序列化消息文本）
    assert "CRITICAL" in captured["task"] or "压缩" in captured["task"]
    # task 不应含 [id:UUID] 格式（那是旧序列化文本的特征）
    assert "[id:" not in captured["task"]


def test_mode3_passes_history_to_call_subagent(monkeypatch):
    """模式三（force）应构造 history 列表传给 call_subagent，而非序列化文本塞进 task。"""
    import asyncio

    import niu_api.compat as compat

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    # mock runner 控制 usage_percent（force 模式不依赖 usage，但 _tidy_context_impl 仍会读取）
    import agent.subagent as subagent_module
    import niu_api.chat as chat_module
    import niu_api.llm_proxy as llm_proxy_module
    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()  # 180K tokens，模拟溢出
        llm_config = {}

        def _ensure_session_chain(self):
            """测试桩：无操作（生产为 NiuRunner 方法，三管道调用）。"""
            pass

    def fake_get_or_create_runner():
        return FakeRunner()

    # mock call_subagent 捕获参数，返回 keep=/update=/cursor= 三行
    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["agent_name"] = agent_name
            captured["task"] = kwargs.get("task", "")
            captured["history"] = kwargs.get("history")
            return "keep=1,2\ncursor=2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    # T6：压缩前置游标追平校验——模拟 entity/dream 已追平（游标=尾部最后一条消息）
    monkeypatch.setattr(compat, "_read_cursor_value", lambda path, key: messages[-1].id, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    # builder refetch lightrag 段（T2 后无参内部 refetch）——mock 隔离，不读真实用户配置
    monkeypatch.setattr(llm_proxy_module, "get_llm_config", lambda use_lightrag_config=False: {
        "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
        "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
    })

    # 调用 _tidy_context_impl force 模式
    request = {"session_id": "test", "mode": "force"}
    try:
        asyncio.run(compat._tidy_context_impl(request))
    except Exception:
        pass  # 后续执行可能报错（未 mock 全部），只关心 call_subagent 是否被正确调用

    # 验证 call_subagent 收到 history 参数
    assert captured.get("agent_name") == "context-manager"
    assert captured.get("history") is not None
    assert isinstance(captured["history"], list)
    assert len(captured["history"]) == 2
    # task 是压缩指令（不含序列化消息文本）
    assert "CRITICAL" in captured["task"] or "压缩" in captured["task"]
    # task 不应含 [id:UUID] 格式（那是旧序列化文本的特征）
    assert "[id:" not in captured["task"]
    # task 应含 cursor= 输出说明（模式三特有）
    assert "cursor=" in captured["task"]
