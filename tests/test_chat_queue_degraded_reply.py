"""ChatQueue runner.chat 异常时降级回复测试
用真实 persist_agent_reply + _FakeStore 验证降级回复真的写入 DB。
不 patch persist_agent_reply（避免假测试）。
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from niu_api.chat_queue import ChatQueue

_RATE_LIMIT_FRIENDLY = "模型服务限流（429），请稍后重试"  # 通道 1 RateLimitError 文案（产品文案锁定）


class _FakeStore:
    def __init__(self):
        self.messages = []
    async def add_message(self, role, content, **kwargs):
        msg_id = str(uuid.uuid4())
        self.messages.append({"id": msg_id, "role": role, "content": content, **kwargs})
        return msg_id


@pytest.mark.asyncio
async def test_runner_chat_exception_writes_degraded_reply_to_db():
    """runner.chat 抛异常时，降级回复 [系统繁忙，请重试] 真的写入 DB"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    # mock runner.chat 抛异常
    def _raise(*args, **kwargs):
        raise RuntimeError("LLM timeout")
    q._runner.chat = MagicMock(side_effect=_raise)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()
    notify_calls = []

    # 用真实 persist_agent_reply（不 patch），验证 rv=None 走 elif 分支写入 DB
    # patch get_message_store 返回 _FakeStore
    # patch notify_new_message 避免实际 SSE 推送（只验证 DB 写入）
    # patch notify_llm_error_sync 记录调用——非 LLM 异常（RuntimeError）不 notify
    # patch get_context_manager 返回 AsyncMock（避免 await MagicMock 抛 TypeError）
    with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
        with patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)):
            with patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: notify_calls.append(a)):
                with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                    mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                    result = await asyncio.wait_for(
                        q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                        timeout=5
                    )

    # 验证降级回复被写入 DB
    assert result == "[系统繁忙，请重试]", f"Expected degraded reply, got {result!r}"
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1, f"Expected 1 assistant message, got {len(assistant_msgs)}"
    assert assistant_msgs[0]["content"] == "[系统繁忙，请重试]"
    # E4-12：非 LLM 异常（RuntimeError 内部 bug）→ degraded_reason="internal" 落库（DB 可追溯）
    assert assistant_msgs[0]["degraded_reason"] == "internal"
    # E2：非 LLM 异常（RuntimeError 内部 bug）不 notify（不误标"模型调用失败"）
    assert notify_calls == []

    await q.stop()


@pytest.mark.asyncio
async def test_normal_path_unchanged():
    """正常路径不受影响——用真实 persist_agent_reply + _FakeStore 验证"""
    q = ChatQueue(runner=MagicMock())
    await q.start()

    # mock runner.chat 正常返回（生成器 yield 一条回复）
    def _ok(*args, **kwargs):
        yield "正常回复"
    q._runner.chat = MagicMock(side_effect=_ok)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None

    fake_store = _FakeStore()

    # 不 patch persist_agent_reply——用真实函数 + _FakeStore 验证 DB 写入
    with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
        with patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)):
            with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                result = await asyncio.wait_for(
                    q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                    timeout=5
                )

    assert result == "正常回复"
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "正常回复"

    await q.stop()


# ===== E2 Task 3：降级分离（中性落库 + 友好投递 + notify 解耦） =====


def _litellm_error_runner():
    """runner：chat 抛 litellm.RateLimitError（chat_error 保留对象后 type() 有效）"""
    q = ChatQueue(runner=MagicMock())

    def _raise(*args, **kwargs):
        raise litellm.RateLimitError(message="You exceeded your current quota", llm_provider="openai", model="gpt-4o")
    q._runner.chat = MagicMock(side_effect=_raise)
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None
    return q


async def _run_chat_queue(q, fake_store, persist_side_effect=None):
    """在 mock 环境中跑 enqueue_and_wait，返回 (result, notify_calls)。
    消费方命名空间 patch：get_message_store 为 chat_queue 模块级绑定（patch 源模块无效——会写真实 DB）；
    notify_llm_error_sync / persist_agent_reply / notify_new_message / get_context_manager 为函数内
    局部 import——patch 源模块（与 compat Task 2 测试同款）。
    """
    notify_calls = []
    patches = [
        patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)),
        patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)),
        patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: notify_calls.append(a)),
        patch("agent.context_manager.get_context_manager", new=AsyncMock()),
    ]
    if persist_side_effect is not None:
        patches.append(patch("niu_api.chat.persist_agent_reply", side_effect=persist_side_effect))
    with patches[0], patches[1], patches[2], patches[3]:
        if persist_side_effect is not None:
            with patches[4]:
                result = await asyncio.wait_for(
                    q.enqueue_and_wait(content="test", source="scheduler", session_id="default"), timeout=5
                )
        else:
            result = await asyncio.wait_for(
                q.enqueue_and_wait(content="test", source="scheduler", session_id="default"), timeout=5
            )
    return result, notify_calls


@pytest.mark.asyncio
async def test_llm_exception_friendly_push_neutral_db_and_notify():
    """LLM 类异常（litellm.RateLimitError 实例）→ 投递文本为友好文案 + DB 仍中性占位符 + notify 事件触发"""
    q = _litellm_error_runner()
    await q.start()
    fake_store = _FakeStore()

    result, notify_calls = await _run_chat_queue(q, fake_store)

    # 投递友好文案（通道 1 翻译）——与落库文本分离
    assert result == _RATE_LIMIT_FRIENDLY
    # DB 仍中性占位符（错误细节不落库）
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "[系统繁忙，请重试]"
    # E4-12：LLM 异常（RateLimitError——非超时类）→ degraded_reason="internal"（粗分类——仅 timeout 单独标记）
    assert assistant_msgs[0]["degraded_reason"] == "internal"
    # notify 事件触发（is_llm 时 notify_llm_error_sync 被调）
    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"  # type(chat_error).__name__ 有效（保留异常对象）
    assert emsg == _RATE_LIMIT_FRIENDLY
    assert src == "chat_queue"

    await q.stop()


@pytest.mark.asyncio
async def test_persist_failure_guard_keeps_friendly_push_and_notify():
    """persist 抛异常（DB 降级）→ 投递仍友好 + notify 仍触发（notify 与 persist 解耦）"""
    q = _litellm_error_runner()
    await q.start()
    fake_store = _FakeStore()

    result, notify_calls = await _run_chat_queue(q, fake_store, persist_side_effect=RuntimeError("db down"))

    # persist 失败投递仍友好（try/except 守卫后统一赋值）
    assert result == _RATE_LIMIT_FRIENDLY
    # persist 失败 → 无 assistant 落库（仅 user 消息）
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 0
    assert len(fake_store.messages) == 1  # 仅 user 消息
    # notify 与 persist 解耦——persist 成败均推（Electron 并行可见性一致）
    assert len(notify_calls) == 1
    assert notify_calls[0][2] == "chat_queue"

    await q.stop()


def _llm_error_rv_runner(error_msg="litellm.RateLimitError: quota exceeded", error_type="RateLimitError", yield_text=_RATE_LIMIT_FRIENDLY):
    """runner：chat 正常 yield（源头友好文案）+ last_return_value 为 LLM_ERROR dict"""
    q = ChatQueue(runner=MagicMock())

    def _ok(*args, **kwargs):
        yield yield_text  # 源头友好化后 full_reply 已是友好文案（agent_loop yield 双参）
    q._runner.chat = MagicMock(side_effect=_ok)
    rv = {"result": "LLM_ERROR", "error_msg": error_msg}
    if error_type is not None:
        rv["error_type"] = error_type
    q._runner.last_return_value = rv
    q._runner._persisted_msgs = None
    return q


@pytest.mark.asyncio
async def test_llm_error_rv_skip_persist_friendly_push_not_double_format():
    """LLM_ERROR return 路径：skip persist（DB 无 assistant 落库）+ notify + full_reply 源头友好文案（非双包）"""
    q = _llm_error_rv_runner()
    await q.start()
    fake_store = _FakeStore()

    result, notify_calls = await _run_chat_queue(q, fake_store)

    # full_reply 直通源头友好文案——不重复 format（通道 2 双包风险）
    assert result == _RATE_LIMIT_FRIENDLY
    # skip persist——错误文本不落库（用户拍板"不写 DB"，刷新 Chat 自然消失）
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 0, "LLM_ERROR 分支必须 skip persist——DB 无 assistant 落库"
    # notify 用 raw error_msg 单独 format
    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"  # error_type 优先 rv 透传显式类型
    assert emsg == _RATE_LIMIT_FRIENDLY
    assert src == "chat_queue"

    await q.stop()


@pytest.mark.asyncio
async def test_llm_error_rv_no_error_type_extraction():
    """LLM_ERROR dict 无 error_type 键 → error_type 从原文提取（error_type or extract_error_type 分支）"""
    q = _llm_error_rv_runner(error_msg="litellm.RateLimitError: quota exceeded", error_type=None)
    await q.start()
    fake_store = _FakeStore()

    result, notify_calls = await _run_chat_queue(q, fake_store)

    assert result == _RATE_LIMIT_FRIENDLY
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 0  # skip persist
    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"  # extract_error_type 从 "litellm.RateLimitError: ..." 提取
    assert emsg == _RATE_LIMIT_FRIENDLY
    assert src == "chat_queue"

    await q.stop()


class _FakeLockNeverAcquire:
    """模拟 _chat_lock 获取失败——acquire() 返回 False（非阻塞语义），
    触发 `if not acquired: raise TimeoutError` 分支（与 wait_for 600s 超时
    汇入同一 except TimeoutError → chat_error="timeout" 字符串路径）。"""

    async def acquire(self):
        return False

    def release(self):
        pass


@pytest.mark.asyncio
async def test_chat_lock_timeout_neutral_reply_no_notify():
    """_chat_lock 超时路径（chat_error="timeout" 字符串）→ 中性占位符投递 + notify 为空。

    chat_error 是字符串而非异常对象——`isinstance(chat_error, BaseException)` 守卫
    判 False → is_llm=False → 不 format 友好文案、不 notify（is_llm 分支锁定）。
    full_reply 回退中性占位符 [系统繁忙，请重试]（与 RuntimeError 等非 LLM 异常同路径）。
    """
    q = ChatQueue(runner=MagicMock())
    q._runner.last_return_value = None
    q._runner._persisted_msgs = None
    await q.start()
    fake_store = _FakeStore()
    notify_calls = []

    # patch _chat_lock（_process_single 函数内 `from niu_api.compat import _chat_lock`
    # 模块级绑定——patch 源模块 niu_api.compat 生效）
    with patch("niu_api.compat._chat_lock", new=_FakeLockNeverAcquire()):
        with patch("niu_api.chat_queue.get_message_store", new=AsyncMock(return_value=fake_store)):
            with patch("niu_api.chat.notify_new_message", new=AsyncMock(return_value=True)):
                with patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: notify_calls.append(a)):
                    with patch("agent.context_manager.get_context_manager", new=AsyncMock()) as mock_cm:
                        mock_cm.return_value.get_context_for_chat = AsyncMock(return_value=[])

                        result = await asyncio.wait_for(
                            q.enqueue_and_wait(content="test", source="scheduler", session_id="default"),
                            timeout=5
                        )

    # 字符串 "timeout" 非 BaseException → is_llm=False → 投递中性占位符（非 LLM 友好文案）
    assert result == "[系统繁忙，请重试]", f"Expected neutral placeholder, got {result!r}"
    # 中性占位符落库（错误细节不进 DB）
    assistant_msgs = [m for m in fake_store.messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "[系统繁忙，请重试]"
    # E4-12：锁超时路径（chat_error="timeout" 字符串）→ degraded_reason="timeout" 落库（瞬态可区分）
    assert assistant_msgs[0]["degraded_reason"] == "timeout"
    # 非 LLM（isinstance BaseException 守卫 False）→ notify_llm_error_sync 不被调用
    assert notify_calls == []
    # 判别锁超时分支：sync_chat 只在锁获取成功后执行——锁超时路径 runner.chat 永不调用。
    # （若 `if not acquired: raise TimeoutError` 被删/锁后移，unstubbed MagicMock runner.chat
    # 抛 TypeError → generic except → 同降级契约 → 无此断言测试静默通过丢失覆盖）
    q._runner.chat.assert_not_called()

    await q.stop()


# ===== E4 T5：degraded_reason 列迁移 + 旧行 NULL 读取端容错（E4-12） =====


@pytest.mark.asyncio
async def test_degraded_reason_migration_and_old_row_null_tolerance(tmp_path):
    """E4-12：degraded_reason 列扩展（tool_call_id 同款 PRAGMA table_info + ALTER TABLE ADD COLUMN 迁移模式）
    + 旧行 NULL 读取端 .get 默认容错（不迁移旧库即显式 SELECT 列查询失败——R4 B P3 实证）。"""
    import sqlite3

    from agent.session import MessageStore

    db_path = str(tmp_path / "messages.db")
    store = MessageStore(db_path=db_path)
    await store.init_db()

    # 模拟旧行：迁移后列存在但值为 NULL（INSERT 未指定 degraded_reason——历史行形态）
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
        ("old-row-1", "assistant", "[系统繁忙，请重试]", "2026-08-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    # 新行：带 degraded_reason 类别写入（走 add_message 显式列清单）
    await store.add_message(role="assistant", content="[系统繁忙，请重试]", degraded_reason="timeout")

    messages = await store.get_messages()
    by_id = {m.id: m for m in messages}

    # 旧行：degraded_reason 为 NULL → 读取端容错（falsy、不崩溃）；dict 形态 .get 默认亦容错
    old = by_id["old-row-1"]
    assert not old.degraded_reason, f"旧行 degraded_reason 应为 NULL 容错（falsy），实际 {old.degraded_reason!r}"
    assert old.to_dict().get("degraded_reason") in (None, ""), "dict 形态 .get 默认 NULL 容错"

    # 新行：类别写读回一致
    new = [m for m in messages if m.id != "old-row-1"]
    assert len(new) == 1, f"应只有 1 条新行，实际 {len(new)}"
    assert new[0].degraded_reason == "timeout"
    assert new[0].content == "[系统繁忙，请重试]"
