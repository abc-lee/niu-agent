"""测试：/chat/sync 端点 LLM_ERROR 分支 skip persist（E2 Task 4）。

场景：
1. runner.chat() 完成后 last_return_value 为 {"result": "LLM_ERROR", ...}
   （对齐 agent_loop LLM_ERROR return：agent_loop.py L892/L905）
2. 验证：错误文本不落库（DB 无 assistant 消息）——E2-03/04 真实 bug：
   LLM_ERROR 错误文本此前经 persist_agent_reply 兜底以 assistant 角色落库；
   用户拍板"不写 DB"，刷新 Chat 从 DB 加载历史时错误自然消失
3. 验证：返回 message_id=None（返回处无条件读取，不初始化则 NameError 500）

测试策略：
- FastAPI TestClient 直接调用 /chat/sync（与 test_chat_sse_persist.py 同模式）
- Mock NiuRunner.chat() 返回 LLM_ERROR 文本流 + 完成后设置 last_return_value = LLM_ERROR dict
- patch 消费方命名空间：niu_api.chat 模块属性（_load_llm_config/get_or_create_runner/notify_new_message）
  + agent.context_manager 源模块（chat_sync 函数内局部导入）+ agent.session 全局 store
- 使用临时 SQLite 数据库验证零错误落库（不触碰真实 ~/.niu/messages.db）
"""
import os
import tempfile
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


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


def _make_mock_runner_llm_error(reply_text: str, error_msg: str, error_type: str):
    """构造 mock NiuRunner：chat() 返回 LLM_ERROR 文本流，完成后 last_return_value 为 LLM_ERROR dict。

    LLM_ERROR dict 对齐 agent_loop return（agent_loop.py L892/L905）：
    {"result": "LLM_ERROR", "error_msg": error_msg, "error_type": getattr(response, "error_type_name", None)}
    """
    runner = Mock()

    llm_error_rv = {
        "result": "LLM_ERROR",
        "error_msg": error_msg,
        "error_type": error_type,
    }

    def chat_generator(session_id, message, stream=True, **kwargs):
        """模拟 runner.chat() 生成器：
        - yield 源头友好文案（E2 Task 2 后 agent_loop 对 LLM_ERROR 分支 yield format 后的文案）
        - 完成后设置 last_return_value（LLM_ERROR dict）
        """
        yield reply_text
        runner.last_return_value = llm_error_rv

    runner.chat = chat_generator
    runner.last_return_value = None
    runner._persisted_msgs = None  # 显式设 None，避免 Mock 属性访问返回 Mock 破坏 getattr 默认值
    runner._extracted_at_msgs = None
    runner.llm_config = {"apikey": "test-key", "model": "test-model"}

    return runner, llm_error_rv


@pytest.mark.asyncio
async def test_chat_sync_llm_error_skip_persist(temp_db_path, temp_message_store):
    """E2 Task 4：/chat/sync LLM_ERROR 分支 skip persist + message_id=None。

    - mock runner 返回 LLM_ERROR rv（agent_loop LLM_ERROR 语义：模型调用失败/用户 Stop）
    - 错误文本不得以 assistant 角色落库（E2-03/04 修复：LLM_ERROR 错误文本经 persist_agent_reply 兜底落库）
    - 返回 message_id=None（返回处无条件读取——不初始化则 NameError 500）
    - reply 正常返回源头友好文案（LLM_ERROR 分支不做二次 format/不改变回复）
    """
    user_input = "你好"
    reply_text = "模型服务限流，请稍后重试"  # 源头友好文案（agent_loop 已 format——全链路双包防护）
    error_msg = "litellm.RateLimitError: 429 rate limit exceeded"
    error_type = "RateLimitError"

    mock_runner, _ = _make_mock_runner_llm_error(reply_text, error_msg, error_type)

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
            # chat_sync 函数内局部导入 agent.context_manager.get_context_manager → patch 源模块
            patch("agent.context_manager.get_context_manager", new_callable=AsyncMock) as mock_get_cm,
        ):
            mock_cm = Mock()
            mock_cm.get_context_for_chat = AsyncMock(return_value=[])
            mock_get_cm.return_value = mock_cm

            client = TestClient(app)

            # ---- 发送 /chat/sync 请求 ----
            response = client.post(
                "/chat/sync",
                json={"message": user_input, "session_id": "test-session"},
            )

            assert response.status_code == 200, (
                f"Expected 200, got {response.status_code}: {response.text}"
            )

            data = response.json()
            assert data["message_id"] is None, (
                f"LLM_ERROR 分支 message_id 应为 None（skip persist），实际: {data['message_id']}"
            )
            assert data["reply"] == reply_text, (
                f"reply 应为源头友好文案 '{reply_text}'，实际: '{data['reply']}'"
            )
    finally:
        # 恢复全局 store
        session_module._message_store = original_store

    # ---- 验证数据库：LLM_ERROR 错误文本零落库 ----
    messages = await temp_message_store.get_messages()

    user_msgs = [m for m in messages if m.role == "user"]
    assistant_msgs = [m for m in messages if m.role == "assistant"]

    # /chat/sync 按设计持久化 user 消息（chat.py store.add_message(role="user", ...)）
    assert len(user_msgs) == 1, (
        f"应持久化 1 条 user 消息，实际: {len(user_msgs)}，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )
    # E2-03/04 核心断言：LLM_ERROR 错误文本不得以 assistant 角色落库
    assert len(assistant_msgs) == 0, (
        f"LLM_ERROR 分支不应持久化 assistant 消息（错误文本落库 bug），实际: {len(assistant_msgs)} 条，"
        f"所有消息: {[(m.role, m.content) for m in messages]}"
    )

    # 兜底断言：任何角色消息都不得包含错误文本
    all_contents = " ".join(m.content or "" for m in messages)
    assert error_msg not in all_contents, (
        f"错误文本不应出现在 DB 中，实际消息: {[(m.role, m.content) for m in messages]}"
    )
