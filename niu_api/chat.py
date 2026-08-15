"""
Chat API endpoints

使用 NiuRunner 作为后端
"""

import asyncio
import json
from typing import Optional

from agent.runner import NiuRunner, get_runner
from agent.session import get_message_store
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from niu_api.internal.subagent_event_bus import subscribe, unsubscribe, has_subagent
from niu_api.compat import _chat_lock

router = APIRouter(tags=["chat"])


# ============== SSE 事件总线（发布-订阅模式） ==============

# 每个 SSE 连接拥有自己的 Queue，notify_new_message 广播到所有订阅者
_event_subscribers: list[asyncio.Queue] = []
_main_loop: asyncio.AbstractEventLoop | None = None

# frontend_ready 事件：前端 SSE 订阅建立后调 /api/frontend-ready 通知后端
# scheduler 收到 signal_scheduler_ready 后等这个事件才扫描过期任务，
# 确保扫到的 reply 推 SSE 时前端已在订阅。
# 超时 60s 未收到强制继续（避免前端永远不起来卡死后端）。
import threading as _threading  # noqa: E402

frontend_ready_event = _threading.Event()


def set_frontend_ready():
    """前端 SSE 订阅建立后调用，通知后端可以开始扫描过期任务"""
    frontend_ready_event.set()
    logger.info("[FRONTEND_READY] Frontend SSE subscription established")


def set_main_event_loop(loop: asyncio.AbstractEventLoop):
    """在 uvicorn 启动时调用，保存主事件循环引用"""
    global _main_loop
    _main_loop = loop


async def notify_new_message(message_id: str, role: str, content: str, source: str = "electron") -> bool:
    """新消息写入数据库后调用，广播给所有 SSE 订阅者。

    source 白名单：electron（前端用户操作）、subagent（子 Agent 触发，阶段二新增）

    Returns:
        True 表示事件已入队广播（或被 role/source 过滤跳过视为成功）；
        False 表示无订阅者或所有订阅者队列满，调用方应保留消息重试。
    """
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
        return True
    if source not in ("electron", "subagent"):
        return True  # 非白名单通道不走SSE，前端零感知，视为成功
    if not _event_subscribers:
        return False  # 无订阅者，调用方（如 db_monitor）应保留消息重试
    event = {
        "type": "new_message",
        "id": message_id,
        "role": role,
        "content": content,
        "source": source,
    }
    delivered = 0
    for q in _event_subscribers[:]:  # 复制列表，避免迭代中修改
        try:
            q.put_nowait(event)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning("[SSE] Subscriber queue full, skipping event")
    return delivered > 0


def notify_new_message_sync(message_id: str, role: str, content: str, source: str = "electron") -> bool:
    """同步版本 — 从非 async 上下文（如 scheduler 线程）调用。

    source 白名单：electron（前端用户操作）、subagent（子 Agent 触发，阶段二新增）

    Returns:
        True 表示事件已成功注入主 loop（或被 role/source 过滤跳过视为成功）；
        False 表示主 loop 不可用 / 已关闭 / call_soon_threadsafe 失败，
        调用方（如 db_monitor._drain_main_agent_request_queue）应保留消息不 pop 重试。
    """
    # 双管道分离：tool 消息只走 DB 管道，不推送给前端
    if role == "tool":
        return True
    if source not in ("electron", "subagent"):
        return True
    if not _event_subscribers:
        return False
    event = {
        "type": "new_message",
        "id": message_id,
        "role": role,
        "content": content,
        "source": source,
    }
    loop = _main_loop
    if loop is None or loop.is_closed():
        return False
    # 用 call_soon_threadsafe 安全注入到 FastAPI 的事件循环
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
        return True
    except RuntimeError:
        # 循环已关闭——返回 False 让调用方保留消息重试
        return False


async def push_ingest_result(file_path: str, error: str = ""):
    """将入库异常写入 message.db 并推送 SSE 通知。

    仅在入库异常时调用（管道失败或质量异常）。正常入库不调用。
    设计为 async 函数，在 LightRAG 事件循环中调用。

    Args:
        file_path: 入库文件路径
        error: 错误信息
    """
    import os
    file_name = os.path.basename(file_path) if file_path else "未知文件"
    content = f"文件入库异常：{file_name}" + (f"（{error}）" if error else "")

    try:
        from agent.session import MessageStore
        store = MessageStore()
        msg_id = await store.add_message(role="system", content=content)
        if msg_id:
            notify_new_message_sync(msg_id, "system", content)
    except Exception:
        pass


def notify_tool_status_sync(tool_name: str, status: str, summary: str = ""):
    """从同步线程推送工具调用状态到 SSE 事件总线

    Args:
        tool_name: 工具名称（短名，如 detect_faces）
        status: "start" 或 "end"
        summary: 可选的简短描述
    """
    event = {
        "type": "tool_status",
        "tool_name": tool_name,
        "status": status,
        "summary": summary,
    }
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass


def notify_compact_status_sync(status: str, mode: str = "", usage: float | None = None,
                               reset_tokens: bool = False) -> None:
    """广播压缩状态事件到 /api/events/stream。

    跨线程安全：可在 executor 工作线程或后台 asyncio task 中调用。
    status: "started" | "done"
    mode: "force" | "sleep" | "auto"（可选，用于日志和前端提示）
    usage: done 时压缩后重算的 context_usage（0-1）；None 表示未计算（前端 fallback loadStats）
    reset_tokens: done 时是否清空主 runner 的 _last_prompt_tokens。
        仅"实际发生压缩"的路径传 True（sleep/force/clear 删除消息后）；
        skip/abort/error 等未压缩路径传 False——旧真实 token 数仍有效，保留使下次判定准确。
    """
    if status == "done" and reset_tokens:
        try:
            runner = get_runner()  # 无创建副作用（不存在返回 None）；勿用 get_or_create_runner
            if runner is not None and getattr(runner, "handler", None) is not None:
                runner.handler._last_prompt_tokens = 0
        except Exception:
            pass
    if _main_loop is None:
        return
    event = {"type": "compact_status", "status": status, "mode": mode, "usage": usage}
    try:
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        # loop 已关闭，忽略
        pass


def notify_brain_region_sync(source: str = 'auto', changed_labels: list[str] | None = None) -> None:
    """广播脑区状态变更事件到 /api/events/stream。

    跨线程安全：可在 agent_loop 线程、brain_tools 工具处理线程、
    HTTP 请求线程中调用。

    Args:
        source: "auto"（自动激活/衰减/强化）或 "manual"（手动修改）
        changed_labels: 变更的区域 label 列表（可选，auto 衰减路径可省略表示全量刷新）
    """
    if _main_loop is None:
        return
    event = {
        "type": "brain_region_updated",
        "source": source,
        "changed_labels": changed_labels or [],
    }
    try:
        _main_loop.call_soon_threadsafe(_sync_broadcast, event)
    except RuntimeError:
        pass

def _sync_broadcast(event: dict):
    """在 FastAPI 事件循环中执行广播"""
    for q in _event_subscribers[:]:  # 复制列表，避免迭代中修改
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 队列满说明客户端处理慢，跳过但不移除
            logger.warning("[SSE] Subscriber queue full, skipping event")


def _msg_fingerprint(msg: dict) -> str | None:
    """生成消息指纹，用于去重判断

    指纹规则：
    - assistant(tool_calls): role + tool_calls[0].id（唯一标识一次工具调用）
    - assistant(纯文本): role + content[:50]（短文本前缀，避免长文本哈希）
    - tool: role + tool_call_id（唯一标识一次工具结果）
    - 其他: None（不参与去重）
    """
    role = msg.get("role", "")
    if role == "assistant":
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            first_id = tool_calls[0].get("id", "") if tool_calls else ""
            return f"assistant:tc:{first_id}"
        else:
            content = (msg.get("content", "") or "")[:50]
            return f"assistant:text:{content}"
    elif role == "tool":
        tool_call_id = msg.get("tool_call_id", "")
        return f"tool:{tool_call_id}"
    return None


async def persist_agent_reply(
    store, rv, history_len: int, full_reply: str, source: str = "electron",
    persisted_msgs: list[dict] | None = None,
    extracted_at_msgs: list | None = None
) -> tuple[str | None, str]:
    """持久化 Agent 回复消息（从 rv["messages"] 双管道），通知前端。

    从 runner.last_return_value 的 messages 中持久化新增的 tool/assistant 消息，
    处理纯文本回复回退，并推送 SSE 通知。

    Args:
        store: SessionStore 实例
        rv: runner.last_return_value（dict or None）
        history_len: 历史 messages 长度（用于切片 rv["messages"]）
        full_reply: Agent 完整回复文本（reply_chunks 拼接）

    Returns:
        (message_id, full_reply) — message_id 为最后一条 assistant 消息的 ID，
        full_reply 为原始回复文本（不过滤，保持原样）
    """
    # 通道一：解析 full_reply 里的 @ 消息，strip 后存纯净回复为 assistant，
    # @ 消息以 role=subagent_msg 存 db（db_monitor 会路由到子 Agent queue）
    # 修正版方案 2：V4 主路径（rv 有 messages 且 persisted_msgs 非空）时 _persist_one_msg
    # 已逐条轮中提取/剥离 @ 段——此处不再提取（full_reply 整轮拼接会跨消息懒匹配，
    # 000006 超长 subagent_msg 根因）；仅 rv=None（停止/兜底）或未逐条持久化时兜底提取
    # （test_persist_agent_reply_dedup 契约：停止窗口 reply→persist 未落库场景仍需提取）。
    from agent.at_message_parser import extract_at_messages, format_for_db, strip_at_messages

    if not (rv and isinstance(rv, dict) and rv.get("messages") and persisted_msgs):
        at_msgs = extract_at_messages(full_reply)
        if at_msgs:
            full_reply = strip_at_messages(full_reply)
            for msg in at_msgs:
                db_content = format_for_db(msg)
                if extracted_at_msgs and db_content in extracted_at_msgs:
                    continue  # 已由 _persist_one_msg 轮中提取（停止落在 persist 已消费窗口）——避免重复入库
                await store.add_message(
                    role="subagent_msg",
                    content=db_content
                )
        elif "@" in full_reply:
            # 用户拍板：@ 消息任何失败不得静默——打日志留痕
            # 文案避免误导：未提取也可能是"为保留标记引用"（@end/@niu-agent 等行文转述）
            logger.warning(f"[persist] full_reply 含 @ 但未提取到合法 @子Agent 消息（格式问题？或为保留标记引用）: {full_reply[:200]}")
    elif "@" in full_reply:
        # V4 主路径（P2-1）：subagent_msg 已由 _persist_one_msg 轮中提取写入——跳过重复提取，
        # 但仍需 strip full_reply（IM 终发 route_out / ChatResponse.reply 推给用户不带 @ 段；
        # subagent_msg 已轮中写，strip 不丢任何东西）
        full_reply = strip_at_messages(full_reply)

    message_id = None

    # DEBUG: Log rv structure for diagnosing tool_calls persistence
    if rv and isinstance(rv, dict) and rv.get("messages"):
        _debug_msgs = rv["messages"]
        logger.info(f"[persist DEBUG] rv has {len(_debug_msgs)} messages, history_len={history_len}, slice_start={history_len+1}")
        for i, m in enumerate(_debug_msgs[history_len+1:], start=history_len+1):
            _tc = m.get("tool_calls")
            _tci = m.get("tool_call_id", "")
            _role = m.get("role", "")
            _content_preview = (m.get("content", "") or "")[:50]
            logger.info(f"[persist DEBUG]   [{i}] role={_role} has_tool_calls={bool(_tc)} tool_call_id={_tci[:20]} content={_content_preview!r}")

    if rv and isinstance(rv, dict) and rv.get("messages"):
        # V4: 构建"已持久化消息"指纹集合，用于兜底去重
        _persisted_fingerprints = set()
        if persisted_msgs:
            for pm in persisted_msgs:
                fp = _msg_fingerprint(pm)
                if fp:
                    _persisted_fingerprints.add(fp)

        last_assistant_id = None
        last_assistant_content = ""
        for msg in rv["messages"][history_len + 1:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id", "")

            if role == "system":
                continue
            if role == "user":
                continue

            # V4: 兜底去重——如果此消息已被逐条推送写入 DB，跳过
            fp = _msg_fingerprint(msg)
            if fp and fp in _persisted_fingerprints:
                continue

            if role == "tool" and tool_call_id:
                await store.add_message(role="tool", content=content or "", tool_call_id=tool_call_id)
            elif role == "assistant":
                # rv 路径下 assistant content 可能仍含 @ 消息原文，
                # strip 避免与 subagent_msg 重复入库（@ 消息已在上文以
                # role=subagent_msg 单独存）。strip 后为空则跳过。
                content = strip_at_messages(content or "")
                if not content.strip():
                    continue
                pid = await store.add_message(role="assistant", content=content, tool_calls=tool_calls)
                last_assistant_id = pid
                last_assistant_content = content

        # 纯文本回复不在 rv["messages"] 中，仅在逐条推送未执行时从 full_reply 持久化
        if not persisted_msgs and full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
            pid = await store.add_message(role="assistant", content=full_reply)
            last_assistant_id = pid

        # V4: SSE通知 + message_id返回
        # 逐条推送已执行时，从persisted_msgs获取最后一条assistant消息的ID
        if persisted_msgs:
            for pm in reversed(persisted_msgs):
                if pm.get("role") == "assistant" and pm.get("_persisted_id"):
                    message_id = pm["_persisted_id"]
                    break
        elif last_assistant_id:
            # 兜底路径：逐条推送未执行，从rv["messages"]遍历获取
            message_id = last_assistant_id
            await notify_new_message(message_id, "assistant", full_reply, source=source)
    elif full_reply.strip():
        # 回退：无 return_value 时，从 full_reply 持久化 assistant 消息
        # V4 去重（R1-P2 前缀判断 + R2-P2 @ 对齐 + R5-P3-A tool_use 对齐）：
        # persisted_msgs 中已写入的 assistant 内容拼接后若以 full_reply 为前缀
        # （含内容相等），说明文本已入库——跳过兜底写；非前缀（停止落在
        # reply→persist 窗口）兜底写避免丢内容。
        import re  # chat.py 顶层未导入 re，函数体内 import（与 L261 at_message_parser 风格一致）
        _persisted_concat = "".join(
            (pm.get("content") or "") for pm in (persisted_msgs or []) if pm.get("role") == "assistant"
        )
        if at_msgs:
            _persisted_concat = strip_at_messages(_persisted_concat)
        # <tool_use> 对齐（R5-P3-A）：主 Agent 非 verbose 分支 reply 已剥 <tool_use> 标签
        # （agent_loop L767-768 re.sub），V4 persist 存原始含标签内容——比较前对拼接内容
        # 做同款剥除（模式/flags 与 L767-768 逐字一致），否则含标签内容前缀失配仍重复
        _persisted_concat = re.sub(r"<tool_use>.*?</tool_use>", "", _persisted_concat, flags=re.DOTALL)
        # 双侧 strip（R4-P3-b）：V4 内容可能带前导空白，仅 strip 一侧会前缀失配仍重复
        if not _persisted_concat.strip().startswith(full_reply.strip()):
            message_id = await store.add_message(role="assistant", content=full_reply)
            await notify_new_message(message_id, "assistant", full_reply, source=source)

    return message_id, full_reply


class ChatRequest(BaseModel):
    """Chat request model"""

    session_id: str | None = None
    message: str
    system_prompt: str | None = ""
    resources: list = []


class ChatResponse(BaseModel):
    """Chat response model"""

    reply: str
    session_id: str | None = None
    message_id: str | None = None


def _load_llm_config():
    """直接从文件读取 LLM 配置，不走缓存，保留所有原始字段"""
    import json
    from pathlib import Path

    from niu_api.config import CONFIG_PATH

    config_path = Path(CONFIG_PATH)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # 直接返回原始配置，统一键名为小写（兼容不同格式）
        config = {}

        # 处理所有可能的键名格式（apiKey/apikey/api_key）
        for key, value in llm.items():
            # 统一转换为小写
            config[key.lower()] = value

        # 确保必要字段有默认值
        config.setdefault("type", "openai")
        config.setdefault("apikey", "")
        config.setdefault("apibase", "")
        config.setdefault("model", "")
        config.setdefault("litellm_kwargs", {})

        return config
    except Exception:
        # 异常兜底与三处标准缺省一致（config-manager/main.js/settings）：无 provider 字段，
        # 主聊天模型 reasoning_effort 缺省为空串（思维深度由模型自己决定，R11/R15）
        return {"type": "openai", "apikey": "", "apibase": "", "model": "", "reasoning_effort": "", "litellm_kwargs": {}}


def init_runner(tool_registry):
    """
    初始化 Runner（从 API 启动时调用）

    Args:
        tool_registry: ToolRegistry 实例
    """
    llm_config = _load_llm_config()

    # 不再需要 mcp_client，handler 直接使用 ToolRegistry
    runner = get_runner(llm_config=llm_config, mcp_client=None)

    # 设置 MCP 工具 Schema（从 ToolRegistry 获取）
    mcp_tools_schema = tool_registry.get_schemas()
    if mcp_tools_schema and runner is not None:
        runner.set_mcp_tools_schema(mcp_tools_schema)


def get_or_create_runner() -> Optional["NiuRunner"]:
    """Get or create NiuRunner，配置变更后自动重新初始化"""
    from agent import runner as runner_module

    existing = get_runner()
    current = _load_llm_config()

    if existing is not None and current["apikey"] and current["model"]:
        # Runner 已存在，检查配置是否变更
        runner_llm = getattr(existing, "llm_config", {})
        if runner_llm.get("apikey") != current["apikey"] or runner_llm.get("model") != current["model"] or runner_llm.get("reasoning_effort") != current.get("reasoning_effort") or runner_llm.get("litellm_kwargs") != current.get("litellm_kwargs"):
            # 配置已变更，重新初始化
            with runner_module._runner_lock:
                runner_module._runner = None

    if get_runner() is None:
        from agent.tool_registry import get_registry
        init_runner(get_registry())

    return get_runner()


@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """
    [DEPRECATED] Main chat endpoint - 使用 NiuRunner 流式响应
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    runner = get_or_create_runner()

    # Get or create session
    session_id = request.session_id or "default"

    # Stream response
    async def generate():
        # 排队等待锁：压缩管道可能执行数分钟，必须等够
        try:
            await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
        except TimeoutError:
            logger.warning("[/chat] _chat_lock 600s timeout, request rejected")
            yield f"data: {json.dumps({'error': 'Another request is in progress, please wait'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
            return

        # Electron 用户消息 → 去 IM 标志（规则 2）：channel_id 清空 + force 清空
        # 必须在 _chat_lock 内（对齐 /chat/sync）：排队等锁期间 scheduler 可能重臂 force，锁内清除才生效
        runner.set_im_channel("")
        runner.set_im_force(False)

        try:
            reply_chunks = []
            stream_error = None

            # Run streaming in executor thread, communicate chunks via queue
            import queue as _queue

            chunk_queue: _queue.Queue[str | None] = _queue.Queue()

            def sync_stream():
                nonlocal stream_error
                try:
                    for chunk in runner.chat(session_id, request.message, stream=True):
                        if chunk:
                            chunk_queue.put(chunk)
                except Exception as e:
                    stream_error = str(e)
                finally:
                    chunk_queue.put(None)  # sentinel

            loop = asyncio.get_running_loop()
            stream_future = loop.run_in_executor(None, sync_stream)

            # Drain queue in async context while executor produces chunks
            while not stream_future.done() or not chunk_queue.empty():
                try:
                    chunk = chunk_queue.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                if chunk is None:
                    break
                reply_chunks.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            # Ensure executor finished
            try:
                await stream_future
            except Exception as e:
                stream_error = stream_error or str(e)

            # Send error if streaming failed
            if stream_error:
                yield f"data: {json.dumps({'error': stream_error})}\n\n"

            # 方案 A：stream_error 时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
            # LLM_ERROR：agent_loop 返回 LLM_ERROR 时流式错误已通过 SSE 推给前端，
            # partial content 不应持久化到 DB（避免错误文本被当作正常 assistant 消息）
            full_reply = "".join(reply_chunks)
            store = await get_message_store()
            rv = getattr(runner, "last_return_value", None)
            message_id = None
            if stream_error:
                logger.warning(f"[Chat SSE] Skipped persist due to stream_error: {stream_error}")
            elif rv and isinstance(rv, dict) and rv.get("result") == "LLM_ERROR":
                logger.warning(f"[Chat SSE] Skipped persist due to LLM_ERROR: {rv.get('error_msg', '')}")
            else:
                # 正常路径：持久化 Agent 回复（使用 persist_agent_reply 双管道）
                history_len = 0  # /chat 端点不加载历史，rv 包含完整 messages
                persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
                extracted_at_msgs = getattr(runner, "_extracted_at_msgs", None)  # 修正版方案：轮中提取的 subagent_msg（去重用）
                message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs, extracted_at_msgs=extracted_at_msgs)

            # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞）
            if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
                overflow_data = rv.get("data", {})
                logger.warning(
                    f"[Chat SSE] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                    f"triggering force compression (blocking)"
                )
                from niu_api.compat import _tidy_context_impl, _tidy_lock
                # 使用带超时的acquire避免AB-BA死锁：
                # SSE /chat 持有 _chat_lock → 等待 _tidy_lock，
                # 模式2压缩持有 _tidy_lock → 等待 _chat_lock
                _tidy_acquired = False
                try:
                    await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
                    _tidy_acquired = True
                except TimeoutError:
                    logger.warning("[Chat SSE] Tidy lock busy, skipping force compression")
                if _tidy_acquired:
                    try:
                        tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                    finally:
                        _tidy_lock.release()
                    logger.info(f"[Chat SSE] Force compression result: {tidy_result.get('status')}")
            # Send final message
            yield f"data: {json.dumps({'done': True, 'session_id': session_id, 'message_id': message_id})}\n\n"
        finally:
            # 确保执行器线程完成后再释放锁，防止 runner.chat() 并发
            # 注意：finally 块中的 await 可能被 CancelledError（BaseException）中断，
            # 必须用嵌套 try/finally 保证 _chat_lock.release() 始终执行
            try:
                sf = locals().get("stream_future")
                if sf and not sf.done():
                    try:
                        await asyncio.wait_for(sf, timeout=30.0)
                    except (TimeoutError, asyncio.CancelledError, Exception):
                        logger.warning("[/chat] Executor thread did not finish after client disconnect")
            finally:
                _chat_lock.release()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/sync")
async def chat_sync(request: ChatRequest) -> ChatResponse:
    """
    Synchronous chat endpoint - waits for complete response.
    Persists both user and assistant messages to the database.
    """
    llm_cfg = _load_llm_config()

    if not llm_cfg["apikey"]:
        raise HTTPException(
            status_code=400, detail="LLM not configured. Please set up API key first."
        )

    # 排队等待锁：压缩管道可能执行数分钟，必须等够
    # 锁获取后立即进入 try/finally，确保 CancelledError 不会导致锁泄漏
    try:
        await asyncio.wait_for(_chat_lock.acquire(), timeout=600.0)
    except TimeoutError:
        logger.warning("[/chat/sync] _chat_lock 600s timeout, request rejected")
        raise HTTPException(
            status_code=503, detail="Another request is in progress, please try again later."
        ) from None

    try:
        # Persist user message to database
        store = await get_message_store()
        user_msg_id = await store.add_message(role="user", content=request.message)
        # /chat/sync 当前只被 Electron 前端调用（scheduler 已走 ChatQueue 入队）
        await notify_new_message(user_msg_id, "user", request.message, source="electron")

        # Load conversation history via ContextManager (same as /api/chat/session)
        from agent.context_manager import get_context_manager

        context_manager = await get_context_manager(store)
        history_for_runner = await context_manager.get_context_for_chat(exclude_last=True)

        runner = get_or_create_runner()
        runner.set_im_channel("")
        runner.set_im_force(False)  # Electron 用户消息转假（规则 2 + 粘性清除）——与 compat/chat_queue electron 分支对齐
        session_id = request.session_id or "default"

        # Run chat (non-streaming)
        def sync_chat():
            full_reply = ""
            for chunk in runner.chat(session_id, request.message, stream=True, history=history_for_runner):
                full_reply += chunk
            return full_reply

        chat_error = None
        try:
            loop = asyncio.get_running_loop()
            full_reply = await loop.run_in_executor(None, sync_chat)
        except Exception as e:
            import traceback
            logger.error(f"Chat sync error: {e}\n{traceback.format_exc()}")
            chat_error = str(e)
            full_reply = f"Error: {str(e)}"

        # 方案 A：异常时不进 DB（避免错误文本被下一轮 _inject_dynamic_resources 当 query 反复查 lightrag）
        if chat_error is None:
            # 持久化 Agent 回复（使用 persist_agent_reply 双管道）
            rv = getattr(runner, "last_return_value", None)
            history_len = len(history_for_runner) if history_for_runner else 0
            persisted_msgs = getattr(runner, "_persisted_msgs", None)  # V4: 已逐条持久化的消息
            extracted_at_msgs = getattr(runner, "_extracted_at_msgs", None)  # 修正版方案：轮中提取的 subagent_msg（去重用）
            message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=persisted_msgs, extracted_at_msgs=extracted_at_msgs)
        else:
            rv = getattr(runner, "last_return_value", None)
            message_id = None
            logger.warning(f"[Chat Sync] Skipped persist due to chat error: {chat_error}")

        # 检测主 Agent 上下文溢出 → 同步触发 force 压缩（阻塞，压缩完再继续）
        if rv and isinstance(rv, dict) and rv.get("result") == "CONTEXT_OVERFLOW":
            overflow_data = rv.get("data", {})
            logger.warning(
                f"[Chat] Main agent CONTEXT_OVERFLOW at {overflow_data.get('tokens_used', 0)} tokens, "
                f"triggering force compression (blocking)"
            )
            from niu_api.compat import _tidy_context_impl, _tidy_lock
            # 使用带超时的acquire避免AB-BA死锁（同SSE /chat路径）
            _tidy_acquired = False
            try:
                await asyncio.wait_for(_tidy_lock.acquire(), timeout=10.0)
                _tidy_acquired = True
            except TimeoutError:
                logger.warning("[Chat] Tidy lock busy, skipping force compression")
            if _tidy_acquired:
                try:
                    tidy_result = await _tidy_context_impl(request={"session_id": session_id, "mode": "force"}, chat_lock_already_held=True)
                finally:
                    _tidy_lock.release()
                logger.info(f"[Chat] Force compression result: {tidy_result.get('status')}")
            # 压缩完成后不再触发额外异步整理（force 已包含完整3步，压缩已在 agent_loop 轮内同步完成）
        return ChatResponse(session_id=session_id, reply=full_reply, message_id=message_id)
    finally:
        _chat_lock.release()


@router.get("/api/events/stream")
async def events_stream():
    """
    SSE 事件流 — 实时推送新消息给前端

    前端（main.js）订阅此端点，收到 new_message 事件后
    通过 Electron IPC 推送给 chat.html 渲染。
    """
    async def generate():
        # 每个连接拥有自己的队列
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.append(q)
        # 在订阅者注册之后通知 frontend_ready——保证 scheduler 扫描过期任务时
        # _event_subscribers 非空，reply 推 SSE 不会丢
        # （前端 main.js 仍会调 POST /api/frontend-ready 作为重连兜底，
        #   但首次连接的可靠通知在这里）
        set_frontend_ready()
        logger.info(f"[SSE] Client connected (total: {len(_event_subscribers)})")
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    # 心跳，保持连接
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            logger.info("[SSE] Client disconnected")
            raise
        finally:
            try:
                _event_subscribers.remove(q)
            except ValueError:
                pass  # already removed (double-disconnect edge case)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/subagents/{unique_name}/stream")
async def subagent_event_stream(unique_name: str):
    """子 Agent 独立 SSE 端点。"""
    if not has_subagent(unique_name):
        raise HTTPException(status_code=404, detail=f"Subagent {unique_name} not found")

    async def generate():
        q = await subscribe(unique_name)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            logger.info(f"[SubagentSSE] {unique_name} client disconnected")
            raise
        finally:
            await unsubscribe(unique_name, q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/frontend-ready")
async def frontend_ready():
    """前端 SSE 订阅建立后调用，通知后端 frontend_ready_event.set()。

    注意：首次连接的可靠通知在 events_stream 的 generate() 内
    _event_subscribers.append(q) 之后调用 set_frontend_ready()。
    本端点作为重连场景的兜底（重连时 event 已 set，wait 立即返回）。
    """
    set_frontend_ready()
    return {"ok": True}


@router.get("/api/chat/status")
async def chat_status():
    """返回当前 Agent 是否忙碌。用于前端窗口恢复时同步停止按钮状态。"""
    return {"busy": _chat_lock.locked()}


@router.post("/api/stop_all")
async def stop_all_subagents():
    """停止所有在跑的子 Agent（双击停止按钮触发）。

    停主 Agent 由前端单独发 /stop 处理（现有机制）。
    """
    from agent.runner import request_stop_all_subagents
    request_stop_all_subagents()
    return {"status": "ok"}


@router.get("/api/subagents/running")
async def list_running_subagents():
    """返回当前在跑的子 Agent 列表（供前端双击停止 UX 提示）。"""
    from agent.subagent_registry import SubagentRegistry
    running = SubagentRegistry.list_running()
    return {
        "count": len(running),
        "subagents": [
            {
                "unique_name": inst.unique_name,
                "agent_type": inst.agent_type,
                "is_sync": inst.is_sync,
                "state": getattr(inst, 'state', 'running'),
                "started_at": getattr(inst, 'started_at', None),
            }
            for inst in running
        ],
    }


class SubagentMessage(BaseModel):
    content: str = Field(..., min_length=1)


@router.post("/api/subagents/{unique_name}/message")
async def send_subagent_message(unique_name: str, msg: SubagentMessage):
    """用户向子 Agent 发送消息（补充信息或回答 @user 提问）。"""
    from agent.route_to_subagent import route_to_subagent

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    result = route_to_subagent(unique_name, sender='user', content=content, source='post_api')
    if result['status'] == 'not_found':
        raise HTTPException(status_code=404, detail=result['message'])
    return {"status": result['status'], "message": result['message']}


class AskAnswerRequest(BaseModel):
    answer: str = Field(..., min_length=1)

    @field_validator("answer")
    @classmethod
    def _strip_answer(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("answer must not be empty")
        return v


@router.post("/api/chat/ask-answer")
async def ask_answer(request: AskAnswerRequest):
    """主 Agent ask_user 等待期间，用户回答注入（不触发新对话轮）。"""
    from agent.ask_user import get_user_ask_registry
    registry = get_user_ask_registry()
    if not registry.is_waiting("main-agent"):
        return {"ok": False, "error": "no pending ask"}
    ok = registry.set_answer("main-agent", request.answer)
    return {"ok": ok}

@router.post("/chat/session")
async def get_or_create_session(request: ChatRequest) -> dict:
    """Get or create a chat session"""
    session_id = request.session_id or "default"
    store = await get_message_store()
    count = await store.count_messages()
    return {"session_id": session_id, "message_count": count}


@router.delete("/chat/session/{session_id}")
async def clear_session(session_id: str):
    """Clear a chat session"""
    store = await get_message_store()
    await store.clear_messages()
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler._last_prompt_tokens = 0
    # 游标复位（与 clear_chat 一致，消除"清消息但游标残留"的不一致）
    from niu_api.compat import _reset_all_cursors
    await _reset_all_cursors()
    return {"status": "ok", "session_id": session_id}
