"""
LightRAG Instance Manager

Manages the LightRAG instance lifecycle: initialization, configuration,
and access. LightRAG runs in-process, sharing the same Python runtime
as the ai-bot API server.

Architecture:
- LLM calls: direct LiteLLMSession.chat() (→ LiteLLM → user-config.json)
- Embedding calls: direct Python callable (→ niu_api.internal.embedding)
- Reranker: direct Python callable (→ niu_api.internal.reranker)
- Storage: NanoVectorDB (LightRAG default) in ~/.niu/lightrag_storage/

Usage:
    from niu_api.internal.lightrag_manager import get_lightrag, ensure_lightrag

    # Get instance (lazy-init on first call)
    rag = get_lightrag()

    # Or force initialization
    rag = await ensure_lightrag()
"""

import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Union
from asyncio import AbstractEventLoop

from loguru import logger

from niu_api.internal.region_manager import FLOOR_WEIGHT

# ============== Config ==============

# STORAGE_DIR 支持环境变量覆盖（让 e2e 测试能用临时目录避免污染 ~/.niu/lightrag_storage）
# 默认值 = ~/.niu/lightrag_storage（与原行为一致）
# 注意：STORAGE_DIR 在 import 时确定值，测试需在 import 前设置环境变量或 monkeypatch.setattr 覆盖
# 空字符串 env 视为未设置，避免 Path("") = cwd 污染项目根目录
_raw_storage_dir = os.environ.get("NIU_STORAGE_DIR", "").strip()
STORAGE_DIR = Path(_raw_storage_dir) if _raw_storage_dir else Path.home() / ".niu" / "lightrag_storage"

# ============== LightRAG LLM Function Builder ==============

# Cache a shared LiteLLMSession instance keyed by config tuple.
# Avoids connection init overhead for high-frequency entity extraction calls.
_cached_session: Optional[Any] = None
_cached_config_key: Optional[tuple] = None
_session_lock = threading.Lock()


def _get_litellm_session(config: dict) -> Any:
    """Get or create a cached LiteLLMSession for LightRAG LLM calls.

    Config changes (model/api_base/api_key/api_type/reasoning_effort) trigger session rebuild.
    Thread-safe via double-check locking.
    """
    global _cached_session, _cached_config_key
    from agent.generic.litellm_adapter import LiteLLMSession

    config_key = (config.get("model"), config.get("apibase"), config.get("apikey"), config.get("type"), config.get("reasoning_effort"), config.get("provider"), config.get("temperature", 0.2), tuple(sorted(config.get("litellm_kwargs", {}).items())))

    if _cached_session is not None and _cached_config_key == config_key:
        return _cached_session

    with _session_lock:
        if _cached_session is not None and _cached_config_key == config_key:
            return _cached_session

        llm_config = {
            "api_type": config.get("type", "openai"),  # type -> api_type mapping
            "apikey": config["apikey"],
            "apibase": config["apibase"],
            "model": config["model"],
            "reasoning_effort": config.get("reasoning_effort"),
            "provider": config.get("provider", ""),
            "litellm_kwargs": config.get("litellm_kwargs", {}),
            "temperature": config.get("temperature", 0.2),
        }

        _cached_session = LiteLLMSession(cfg=llm_config)
        _cached_config_key = config_key
        logger.info("Created LiteLLMSession for LightRAG: model=%s, api_type=%s, provider=%s", config.get("model"), config.get("type"), config.get("provider"))
        return _cached_session


def _build_keyword_extraction_response_format() -> dict:
    """构造 keyword_extraction 用的 json_schema strict response_format。

    抽出来复用，避免 _resolve_response_format 内两处重复构造（漂移风险）。
    """
    from lightrag.types import GPTKeywordExtractionFormat
    schema = GPTKeywordExtractionFormat.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "keyword_extraction",
            "strict": True,
            "schema": schema,
        },
    }


def _resolve_response_format(config: dict) -> Optional[dict]:
    """根据 litellm_kwargs.response_format_mode 决定构造哪种 response_format。

    返回值：
    - {"type": "json_schema", "json_schema": {...strict...}}: 最强档，schema 严格匹配
    - {"type": "json_object"}: 中等档，仅约束合法 JSON
    - None: 最弱档，prompt-only + json_repair 客户端容错

    本函数无副作用，不修改 config。response_format_mode 字段在 _llm_model_func
    内通过 _strip_response_format_mode 单独剔除，避免透传给 LiteLLM provider
    （response_format_mode 是项目自定义字段，不是 OpenAI 标准也不是 LiteLLM
    认识的字段）。

    配置优先级：
    1. response_format_mode 字段（探测结果，权威）
    2. allowed_openai_params 含 "response_format"（旧版本兼容，默认 json_schema 档）
    3. 都没有 → None（保守降级，未探测过）

    Why: OpenAI response_format.type 有 3 档（json_schema/json_object/无），
    不同厂商支持档位不同。探测端点按 json_schema → json_object → prompt_only
    递进测试，结果写入 response_format_mode。本函数运行时读出来决定构造哪种。

    真实环境验证（2026-07-19）：
    - 豆包 Coding Plan：网关 400 拒绝 response_format → 探测后 mode=prompt_only
    - GLM：网关接受但模型输出漂移 → 探测后 mode=prompt_only
    - OpenAI：真正支持 → 探测后 mode=json_schema
    """
    litellm_kwargs = config.get("litellm_kwargs") or {}
    mode = litellm_kwargs.get("response_format_mode")
    if mode == "json_schema":
        return _build_keyword_extraction_response_format()
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "prompt_only":
        return None
    # 旧版本兼容：无 response_format_mode 但有 allowed_openai_params
    allowed = litellm_kwargs.get("allowed_openai_params") or []
    if "response_format" in allowed:
        return _build_keyword_extraction_response_format()
    return None


def _strip_response_format_mode(config: dict) -> dict:
    """剔除 config["litellm_kwargs"]["response_format_mode"] 字段，返回新 dict。

    Why: response_format_mode 是项目自定义字段，不是 OpenAI 标准也不是
    LiteLLM 认识的字段。如果留在 litellm_kwargs：
    1. 会被 LiteLLMSession.chat 通过 request_params.update(self.litellm_kwargs)
       (litellm_adapter.py:377-378) 透传给 litellm.completion，可能触发 provider
       400 拒绝未知参数
    2. 会进入 _get_litellm_session 的 config_key 计算（lightrag_manager.py:66
       tuple(sorted(config.get("litellm_kwargs", {}).items()))），影响缓存键

    本函数返回新 dict 不修改原 config（无副作用），调用方用返回值传给
    _get_litellm_session。原 config 仍含 response_format_mode，下次 _resolve_response_format
    调用仍能读到，但不再透传给 provider 也不参与 config_key。

    Why 不直接 pop：v4 曾用 pop 副作用修改 config，导致 keyword_extraction=True
    与 False 两种调用模式的 config_key 不一致，破坏 _get_litellm_session 缓存。
    本函数返回新 dict 避免此问题。
    """
    litellm_kwargs = config.get("litellm_kwargs") or {}
    if "response_format_mode" not in litellm_kwargs:
        return config  # 无需复制
    new_litellm_kwargs = {k: v for k, v in litellm_kwargs.items() if k != "response_format_mode"}
    return {**config, "litellm_kwargs": new_litellm_kwargs}


def _should_auto_probe_after_upgrade(user_config: dict) -> bool:
    """判断是否需要在启动时自动触发 response_format 探测。

    返回 True 的条件：lightrag_llm.litellm_kwargs 和 llm.litellm_kwargs
    都无 response_format_mode 键（表示用户从旧版本升级，未探测过）。

    Why: 旧版本用户配置无 response_format_mode 字段。如果不自动探测，
    GLM 等需要探测的配置会永远走 prompt_only（_resolve_response_format
    返回 None），与"GLM 支持其他返回格式"事实矛盾。

    同时检查 llm.litellm_kwargs 是因为 lightrag_llm.model 为空时
    get_llm_config 走 fallback 用 llm 段，response_format_mode 可能
    写在 llm 段（场景二/三：LightRAG 用主 Agent 同一模型）。
    """
    lightrag_llm = user_config.get("lightrag_llm") or {}
    lightrag_kwargs = lightrag_llm.get("litellm_kwargs") or {}
    llm = user_config.get("llm") or {}
    llm_kwargs = llm.get("litellm_kwargs") or {}
    return "response_format_mode" not in lightrag_kwargs and "response_format_mode" not in llm_kwargs


def _trigger_background_probe_if_needed() -> None:
    """启动后后台探测 response_format 档位（如检测到旧版本配置）。

    在独立 daemon 线程跑，不阻塞启动流程。探测结果写入
    lightrag_llm.litellm_kwargs.response_format_mode + allowed_openai_params。

    时序说明：本函数在 LightRAG eager init 之后调用，但此时 niu_api lifespan
    可能还没 yield（FastAPI 在 yield 前不处理 HTTP 请求）。daemon 线程内先
    sleep 10s 等服务起来，然后最多重试 3 次（每次间隔 10s）。

    已开始执行的 keyword_extraction 调用会继续用旧 session（基于旧 config_key），
    下次调用 _get_litellm_session 看到 config_key 变化会自动重建 session，
    读到新配置——无时序问题。

    M2 atomic write：先写临时文件再 os.replace，避免主进程在写入过程中读到
    部分 JSON 触发 JSONDecodeError。
    """
    import json
    import threading
    from pathlib import Path
    from niu_api.llm_proxy import get_llm_config

    def _probe_in_background():
        try:
            user_config_path = Path.home() / ".niu" / "user-config.json"
            if not user_config_path.exists():
                # 兼容项目内 config/user-config.json
                user_config_path = Path(__file__).parent.parent.parent / "config" / "user-config.json"
            if not user_config_path.exists():
                return
            with open(user_config_path, encoding="utf-8") as f:
                user_config = json.load(f)
            if not _should_auto_probe_after_upgrade(user_config):
                return  # 已探测过

            # 后台触发探测（用当前 lightrag_llm 配置）
            import httpx
            config = get_llm_config(use_lightrag_config=True)
            # 标准化字段名（get_llm_config 返回小写）
            probe_payload = {
                "apikey": config.get("apikey", ""),
                "apibase": config.get("apibase", ""),
                "model": config.get("model", ""),
                "type": config.get("type", "openai"),
                "provider": config.get("provider", ""),
                "litellm_kwargs": config.get("litellm_kwargs", {}),
            }
            # 重试机制：本函数在 LightRAG eager init 后立即调用，此时 lifespan
            # 可能还没 yield（FastAPI 在 yield 前不处理 HTTP 请求）。daemon 线程
            # 先 sleep 10s 等服务起来，然后最多重试 3 次（每次间隔 10s）。
            data = None
            import time
            time.sleep(10)  # 等 lifespan yield + 服务起来
            for _ in range(3):
                try:
                    # 三次采样 + 限流/超时重试最坏耗时 ~250s/档（限流主导早返 ~160s），两档 ~500s。
                    # 正常场景 3 次采样 + 无重试约 90s/档。设 300s 覆盖正常+限流场景，病态连续
                    # 超时场景（~335s/档）后台会先放弃，属可接受取舍。
                    with httpx.Client(timeout=300) as client:
                        resp = client.post(
                            "http://127.0.0.1:9876/api/probe-response-format",
                            json=probe_payload,
                        )
                        data = resp.json()
                    if data.get("result") == "supported":
                        break
                except Exception:
                    pass
                time.sleep(10)
            if not data or data.get("result") != "supported":
                return  # 探测失败不写配置

            mode = data.get("mode")
            if mode not in ("json_schema", "json_object", "prompt_only"):
                return

            # 关键：prompt_only 降级时区分"真不支持"与"基础设施临时故障"
            # - reason 含 gateway_blocked：网关 200 但响应非合法 JSON（如 GLM），
            #   是真不支持，写入 prompt_only
            # - reason 仅含 tier_failed（无 gateway_blocked）：3 档都因异常降级
            #   （4xx/5xx/超时/认证/限流/网络），很可能是 API Key 临时失效或
            #   网络波动，不写入避免覆盖用户原有配置导致永久降级
            reason = data.get("reason", "")
            # 新逻辑下 infra_error 走 probe_failed 早返（不写配置），rate_limited/timeout
            # 走 probe_failed 早返（不写配置），prompt_only 必然是真不支持（gateway_blocked
            # 或 model_rejected）。守卫保留但改为接受两种确定性不支持信号。
            if mode == "prompt_only" and not ("gateway_blocked" in reason or "model_rejected" in reason):
                logger.warning(
                    f"探测结果 prompt_only 但 reason 不含确定性不支持信号，跳过写入: {reason[:100]}"
                )
                return

            # 写入前重读文件确认 response_format_mode 仍未被其他进程写入
            # （避免与前端 saveConfig 并发竞争覆盖用户配置）
            with open(user_config_path, encoding="utf-8") as f:
                fresh_config = json.load(f)
            fresh_lightrag = fresh_config.get("lightrag_llm") or {}
            fresh_kwargs = fresh_lightrag.get("litellm_kwargs") or {}
            if "response_format_mode" in fresh_kwargs:
                logger.info(
                    "Background probe: response_format_mode already written "
                    "by another process, skipping write"
                )
                return

            # 写入配置（atomic write：先写临时文件再 os.replace，避免主进程
            # 在写入过程中读到部分 JSON 触发 JSONDecodeError）
            allowed = ["response_format"] if mode in ("json_schema", "json_object") else []
            lightrag_llm = user_config.setdefault("lightrag_llm", {})
            litellm_kwargs = lightrag_llm.setdefault("litellm_kwargs", {})
            litellm_kwargs["response_format_mode"] = mode
            litellm_kwargs["allowed_openai_params"] = allowed
            import os
            tmp_path = f"{user_config_path}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(user_config, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, user_config_path)
            except Exception:
                # 写入或替换失败时清理临时文件，避免残留
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise
            logger.info("Background probe completed: response_format_mode=%s", mode)
        except Exception as e:
            logger.warning("Background probe failed: %s", e)

    threading.Thread(target=_probe_in_background, daemon=True, name="response-format-probe").start()


def _build_llm_model_func():
    """Build the async LLM function for LightRAG.

    Returns an async function that LightRAG calls for all LLM operations.
    Calls LiteLLMSession.chat() directly via asyncio.to_thread, avoiding
    OpenAI SDK compatibility issues and HTTP proxy overhead.

    Brain region injection is done here (not in proxy layer) for entity
    extraction requests.
    """
    from niu_api.llm_proxy import get_llm_config
    from niu_api.internal.brain_region_prompt import (
        build_static_brain_region_prompt,
        build_dynamic_brain_region_prompt,
        BRAIN_REGION_MARKER,
    )

    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> Union[str, AsyncGenerator[str, Any]]:
        # 1. Pop LightRAG internal params (concurrency control, not for LLM)
        kwargs.pop("hashing_kv", None)
        kwargs.pop("_priority", None)
        kwargs.pop("_timeout", None)
        kwargs.pop("_queue_timeout", None)

        # 2. Brain region injection for entity extraction requests
        if system_prompt and BRAIN_REGION_MARKER in system_prompt:
            if "大脑区域架构" not in system_prompt:  # idempotent guard
                static_part = build_static_brain_region_prompt()
                dynamic_part = build_dynamic_brain_region_prompt()
                system_prompt = system_prompt + f"\n\n{static_part}\n\n{dynamic_part}"

        # 3. Handle keyword_extraction: 根据探测结果构造对应 response_format
        # 探测由设置窗口"测试连接并保存"触发，按 json_schema → json_object → prompt_only
        # 递进测试，结果写入 lightrag_llm.litellm_kwargs.response_format_mode。
        # 真实环境验证（2026-07-19）：
        # - 豆包 Coding Plan：网关 400 拒绝，探测后 mode=prompt_only
        # - GLM：网关接受但模型输出漂移，探测后 mode=prompt_only
        # BadRequestError fallback 保留兜底（偶发 400 时仍走 prompt-only 重试）。
        config = get_llm_config(use_lightrag_config=True)
        response_format = None
        kw_prompt_suffix = ""
        if keyword_extraction:
            response_format = _resolve_response_format(config)
            kw_prompt_suffix = '\n\nReturn your response as a JSON object with "high_level_keywords" and "low_level_keywords" arrays.'

        # 4. Build messages list
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            for msg in history_messages:
                content = msg.get("content") or ""  # litellm safety: None -> ""
                messages.append({"role": msg.get("role", "user"), "content": content})
        messages.append({"role": "user", "content": prompt})

        # 5. Handle enable_cot and stream from kwargs
        enable_cot = kwargs.pop("enable_cot", False)
        stream = kwargs.pop("stream", False)

        # 7. Call LiteLLMSession via asyncio.to_thread
        def _consume_generator(gen):
            """Consume a LiteLLMSession.chat() generator, return (chunks, mock_response)."""
            chunks = []
            mock_response = None
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_response = e.value
            return chunks, mock_response

        def sync_call():
            from litellm import BadRequestError
            session = _get_litellm_session(_strip_response_format_mode(config))

            # Try with response_format first (works for models like OpenAI that support it)
            gen = session.chat(messages=messages, response_format=response_format)
            try:
                chunks, mock_response = _consume_generator(gen)
            except BadRequestError:
                if not keyword_extraction:
                    raise
                # Model doesn't support response_format — retry with prompt-only approach
                logger.info("response_format not supported by model, retrying with prompt-only JSON instruction")
                fallback_messages = list(messages)
                fallback_messages[-1]["content"] = prompt + kw_prompt_suffix
                gen = session.chat(messages=fallback_messages, response_format=None)
                chunks, mock_response = _consume_generator(gen)

            full_content = "".join(chunks)

            # Handle enable_cot (thinking chain)
            if enable_cot and mock_response and mock_response.thinking:
                if full_content:
                    # Content exists — ignore thinking, just return content
                    pass
                else:
                    # No content but thinking exists — wrap in think tags
                    full_content = f"<think>{mock_response.thinking}</think>\n"

            return full_content

        result = await asyncio.to_thread(sync_call)

        # 8. Stream handling
        if stream:
            # Pseudo-streaming: split complete result into chunks as AsyncIterator
            chunk_size = 20
            async def _async_gen():
                for i in range(0, max(len(result), 1), chunk_size):
                    yield result[i:i + chunk_size]
            return _async_gen()

        return result

    return _llm_model_func


def _get_lightrag_config() -> Dict[str, Any]:
    """Read LightRAG config from preferences.json."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            return prefs.get("lightrag", {})
    except Exception as e:
        logger.debug(f"Failed to read lightrag config: {e}")
    return {}


def _get_embedding_dim_for_lightrag() -> int:
    """Get embedding dimension for LightRAG from config."""
    from niu_api.internal.embedding import get_embedding_dim
    return get_embedding_dim()


# ============== Async/Sync Bridge ==============

# LightRAG is async. We run it in a dedicated daemon thread with its own
# event loop, bridging sync callers (handler) to async LightRAG.

_loop: Optional[AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()

# Read-write lock for the NetworkX knowledge graph.
#
# IMPORTANT: call_async operations (ainsert_custom_kg, adelete_by_entity,
# amerge_entities, ainsert) must NOT be called inside this lock.
# call_async submits to LightRAG's asyncio loop and blocks for up to 600s;
# holding the lock during that time would freeze all reads.
#
# Lock usage:
# - Write lock: only for direct NetworkX mutations (e.g. _decay_structural_edges
#   which calls kg.remove_edge / edge_data["weight"] directly).
#   call_async-based writes do NOT need this lock — they run serialized in the
#   asyncio loop and update the NetworkX graph internally.
# - Read lock: acquired by get_graph_snapshot, list_entities (entity_type path),
#   and any other direct NetworkX graph traversal. Readers should copy() the
#   graph under the lock, then iterate the snapshot lock-free.
#
# CAVEAT: graph_read_lock only synchronizes with direct NetworkX mutations
# (graph_write_lock holders like _decay_structural_edges). It does NOT
# synchronize with call_async-based writes, which run in the asyncio loop
# without acquiring this lock. This means snapshot = g.copy() under
# graph_read_lock may still encounter concurrent modification from call_async.
# This is a deliberate trade-off: holding the lock during call_async would
# freeze reads for up to 600s. In practice, call_async writes are serialized
# in the asyncio loop and brief; the risk of partial snapshot is low but not
# zero. If a RuntimeError occurs, the endpoint returns an empty result and
# the frontend retries on the next poll cycle.
#
_graph_rwlock = threading.RLock()


def graph_read_lock():
    """Context manager for read access to the NetworkX graph.

    Usage:
        with graph_read_lock():
            snapshot = nx_graph.copy()
    """
    return _graph_rwlock


def graph_write_lock():
    """Context manager for direct NetworkX graph mutations (NOT call_async).

    Only use for operations that directly modify the NetworkX graph object
    (e.g. kg.remove_edge, edge_data["weight"] = ...).
    Do NOT wrap call_async() calls — they block too long and freeze reads.

    Usage:
        with graph_write_lock():
            kg.remove_edge(src, tgt)
    """
    return _graph_rwlock


def get_brain_regions() -> list[str]:
    """Get list of brain region names from the knowledge graph.

    Directly reads from the NetworkX in-memory graph without calling
    LightRAG API, avoiding potential event loop deadlocks.

    This is a pure synchronous read — safe to call from anywhere,
    including LLM proxy callbacks.

    Returns:
        List of brain region names (e.g., ["聊天历史脑区", "文档库脑区"]),
        or empty list if LightRAG is unavailable or graph is empty.
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return []

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return []

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return []

        # Take a snapshot under read lock to prevent RuntimeError from
        # concurrent graph modification by background sync threads.
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Filter nodes whose entity_type is BrainRegion
        brain_regions = [
            name for name, data in snapshot.nodes(data=True)
            if data.get("entity_type", "").lower() == "brainregion"
        ]

        return brain_regions

    except Exception as e:
        logger.debug("get_brain_regions failed: %s", e)
        return []


def get_region_members(region_name: str) -> list[str]:
    """Get member entity names for a specific brain region.

    Directly reads from the NetworkX in-memory graph, finding entities
    connected to the region via "包含" edges.

    This is a pure synchronous read — safe to call from anywhere,
    including LLM proxy callbacks.

    Args:
        region_name: Brain region entity name (e.g., "文档库脑区")

    Returns:
        List of member entity names, or empty list if region not found.
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return []

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return []

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return []

        # Take a snapshot under read lock
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Find members via "包含" edges (region -> member)
        # Note: LightRAG stores edge type in 'keywords' field, not 'type'
        # Note: LightRAG graph keys are all lowercase, so compare in lowercase
        region_name_lower = region_name.lower() if isinstance(region_name, str) else region_name
        members = []
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "包含":
                if src == region_name_lower:
                    members.append(tgt)
                elif tgt == region_name_lower:
                    members.append(src)

        return members

    except Exception as e:
        logger.debug("get_region_members failed: %s", e)
        return []


def get_all_region_members() -> dict[str, list[str]]:
    """Get all brain regions and their member entity names.

    Directly reads from the NetworkX in-memory graph without calling
    LightRAG API, avoiding potential event loop deadlocks.

    Returns:
        Dict mapping region name to list of member entity names,
        e.g., {"文档库脑区": ["Python", "NumPy"], "聊天历史脑区": ["用户"]}
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return {}

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return {}

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return {}

        # Take a snapshot under read lock
        with graph_read_lock():
            snapshot = nx_graph.copy()

        # Build mapping: region -> members
        # Note: LightRAG stores edge type in 'keywords' field, not 'type'
        region_members: dict[str, list[str]] = {}
        for src, tgt, data in snapshot.edges(data=True):
            edge_type = data.get("keywords") or data.get("type", "")
            if edge_type.lower() == "包含":
                # 无向图中 src/tgt 顺序不确定，需判断哪端是脑区
                if src.endswith("脑区"):
                    region, member = src, tgt
                elif tgt.endswith("脑区"):
                    region, member = tgt, src
                else:
                    continue
                if region not in region_members:
                    region_members[region] = []
                region_members[region].append(member)

        return region_members

    except Exception as e:
        logger.debug("get_all_region_members failed: %s", e)
        return {}


def find_entities_with_single_floor_edge(floor_weight: float = FLOOR_WEIGHT) -> set[str]:
    """找出"只剩 1 条 _region:contains 归属边、且该边已到保底值"的实体集合。

    用途：脑区社区重算输入范围扩展。这些实体被保底规则锁在原脑区无法迁移，
    必须被纳入社区重算，让新脑区分配一条归属边后，下轮衰减自然解除保底。

    判定规则（扩展判定，比 _decay_brain_region_edges 的 total_degree<=1 更严格）：
      - 只统计 _region:contains 归属边数量（keywords="包含"），不数知识边
      - 跳过 _session: 前缀边（keywords 字段以 "_session:" 开头）
      - 跳过脑区节点本身（name 以"脑区"结尾，与 get_all_region_members 一致）
      - 归属边数量 == 1 且 weight <= floor_weight → 命中

    与 _decay_brain_region_edges 的区别：
      - decay 用 total_degree<=1（全部边数含知识边）触发保底，会漏掉
        "1 条归属边 + 多条知识边"的实体（这些实体被保底锁住但 total_degree>1）
      - 本函数专门捕捉这类被保底锁住但仍有知识边的实体，让它们参与社区重算

    注意：知识边（实体↔实体，keywords 非 "包含" 且非 "_session:"）不参与计数。

    Args:
        floor_weight: 保底权重阈值（默认 region_manager.FLOOR_WEIGHT=0.1）

    Returns:
        实体名称集合（小写，与 detect_communities 中 assigned_entities 一致）
    """
    try:
        rag = get_lightrag()
        if rag is None:
            return set()

        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return set()

        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None or nx_graph.number_of_nodes() == 0:
            return set()

        with graph_read_lock():
            snapshot = nx_graph.copy()

        result: set[str] = set()
        # 脑区判断方式：与 get_all_region_members L429-434 保持完全一致
        # 只用 name.endswith("脑区") 判断——系统所有脑区命名都是 "{label}脑区" 格式
        # （region_manager.py L53 REGION_SUFFIX="脑区" + L384 f"{region_label}{REGION_SUFFIX}"）
        # 不用 entity_type=="brainregion"——避免与 get_all_region_members 不一致
        for node_id in snapshot.nodes():
            # 跳过脑区节点本身（只用 endswith("脑区") 判断）
            if isinstance(node_id, str) and node_id.endswith("脑区"):
                continue

            # 防御性：node_id 必须是 str，否则 .lower() 会失败
            if not isinstance(node_id, str):
                continue

            # 统计该实体的 _region:contains 归属边数
            contains_edges = []
            for neighbor_id, edge_data in snapshot[node_id].items():
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                kw_lower = kw.lower() if isinstance(kw, str) else ""
                # 跳过 _session: 前缀边
                if kw_lower.startswith("_session:"):
                    continue
                # 只数 _region:contains 归属边（keywords="包含"）
                if kw_lower != "包含":
                    continue
                # 防御性校验：另一端必须是脑区节点（与 get_all_region_members 一致：endswith("脑区")）
                if not (isinstance(neighbor_id, str) and neighbor_id.endswith("脑区")):
                    continue
                contains_edges.append(edge_data)

            # 只剩 1 条归属边 + 已到保底值
            if len(contains_edges) == 1:
                w = contains_edges[0].get("weight", 1.0)
                try:
                    w = float(w)
                except (TypeError, ValueError):
                    continue
                if w <= floor_weight:
                    result.add(node_id.lower())

        return result

    except Exception as e:
        logger.debug("find_entities_with_single_floor_edge failed: %s", e)
        return set()


def remove_region_edges(region_name: str, edge_type: str) -> int:
    """Remove edges of a specific type from a brain region node.

    Directly operates on the internal NetworkX graph under write lock.

    Args:
        region_name: Brain region entity name
        edge_type: Edge keywords to match (case-insensitive)

    Returns:
        Number of edges removed.

    Note: Assumes nx.Graph (not MultiGraph). LightRAG uses nx.Graph,
    add_edge is upsert semantics — no parallel edges.
    """
    removed = 0
    try:
        rag = get_lightrag()
        if rag is None:
            return 0
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return 0
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None:
            return 0
        region_key = region_name.lower() if isinstance(region_name, str) else region_name
        with graph_write_lock():
            if region_key not in nx_graph:
                return 0
            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                if kw.lower() == edge_type.lower():
                    nx_graph.remove_edge(region_key, neighbor_id)
                    removed += 1
    except Exception as e:
        logger.debug("remove_region_edges failed for %s: %s", region_name, e)
    return removed


def remove_region_stale_edges(
    region_name: str, edge_type: str, keep_members: set[str]
) -> int:
    """Remove edges of a specific type from a brain region, except those
    connecting to members in keep_members.

    Directly operates on the internal NetworkX graph under write lock.
    Used for atomic drift updates: inject new edges first, then remove
    stale edges — avoiding the zero-member window.

    Args:
        region_name: Brain region entity name
        edge_type: Edge keywords to match (case-insensitive)
        keep_members: Set of member entity names whose edges to preserve.
                      Names are compared case-insensitively.

    Returns:
        Number of edges removed.
    """
    removed = 0
    try:
        rag = get_lightrag()
        if rag is None:
            return 0
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return 0
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        if nx_graph is None:
            return 0
        region_key = region_name.lower() if isinstance(region_name, str) else region_name
        keep_lower = {m.lower() for m in keep_members}
        with graph_write_lock():
            if region_key not in nx_graph:
                return 0
            for neighbor_id in list(nx_graph.neighbors(region_key)):
                edge_data = nx_graph.get_edge_data(region_key, neighbor_id)
                if edge_data is None:
                    continue
                kw = edge_data.get("keywords") or edge_data.get("type", "")
                if kw.lower() == edge_type.lower():
                    if neighbor_id not in keep_lower:
                        nx_graph.remove_edge(region_key, neighbor_id)
                        removed += 1
    except Exception as e:
        logger.debug("remove_region_stale_edges failed for %s: %s", region_name, e)
    return removed


def _ensure_loop() -> AbstractEventLoop:
    """Ensure the daemon event loop is running (thread-safe)."""
    global _loop, _loop_thread

    # Fast path: already running
    if _loop is not None and _loop.is_running():
        return _loop

    with _loop_lock:
        # Double-check after acquiring lock
        if _loop is not None and _loop.is_running():
            return _loop

        _loop_ready.clear()

        def _run_loop():
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            _loop_ready.set()  # Signal that _loop is assigned
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="lightrag-loop")
        _loop_thread.start()

        # Wait for loop to be ready (Event is set after _loop assignment)
        if not _loop_ready.wait(timeout=5.0):
            raise RuntimeError("LightRAG event loop failed to start")

        assert _loop is not None  # set by _run_loop after loop creation
        return _loop


def call_async(coro, timeout: int = 120):
    """Run an async coroutine in the LightRAG event loop (blocking).

    Usage:
        result = call_async(rag.aquery("hello"))
        result = call_async(rag.ainsert(content), timeout=600)  # 10 min for large docs
    """
    import concurrent.futures as _cf

    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except _cf.TimeoutError:
        future.cancel()
        raise
    except asyncio.CancelledError:
        future.cancel()
        raise
    except Exception:
        future.cancel()
        raise


# Track pending fire-and-forget futures for graceful shutdown.
_pending_futures: list = []
_pending_lock = threading.Lock()


def fire_and_forget(coro, context: str = ""):
    """Submit an async coroutine to the LightRAG event loop without waiting.

    The coroutine runs in the background and any exception is logged.
    Use for long-running operations (e.g. entity extraction pipeline)
    where the caller should not block.

    Args:
        coro: The async coroutine to submit.
        context: Optional context string for error logging (e.g. track_id, file name).

    Usage:
        fire_and_forget(rag.apipeline_process_enqueue_documents(), context="track-123")
    """
    loop = _ensure_loop()

    # Capture future ref so _wrapped can remove only its own entry.
    future_ref: list = [None]

    async def _wrapped():
        try:
            await coro
        except asyncio.CancelledError:
            ctx = f" context={context}" if context else ""
            logger.debug(f"[fire_and_forget] coroutine cancelled:{ctx}")
        except Exception as e:
            ctx = f" context={context}" if context else ""
            logger.error(f"[fire_and_forget] coroutine failed:{ctx} error={e}")
        finally:
            with _pending_lock:
                f = future_ref[0]
                if f is not None and f in _pending_futures:
                    _pending_futures.remove(f)

    future = asyncio.run_coroutine_threadsafe(_wrapped(), loop)
    future_ref[0] = future
    with _pending_lock:
        _pending_futures.append(future)


def shutdown_pending_futures(timeout: float = 10.0):
    """Wait for pending fire-and-forget futures to complete, then cancel remaining.

    Called during application shutdown to prevent documents stuck in PENDING state.
    Uses a total deadline across all futures, not per-future timeout.
    """
    import concurrent.futures
    import time

    with _pending_lock:
        futures = list(_pending_futures)

    if not futures:
        return

    logger.info(f"[fire_and_forget] shutdown: waiting for {len(futures)} pending futures")

    deadline = time.monotonic() + timeout
    for future in futures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            future.cancel()
            continue
        try:
            future.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            logger.info("[fire_and_forget] shutdown: future timed out, cancelling")
            future.cancel()
        except Exception:
            future.cancel()
        except BaseException:  # KeyboardInterrupt, SystemExit — re-raise
            future.cancel()
            raise

    # Remove only the futures we managed (waited/cancelled), not any
    # that were added to _pending_futures after our snapshot was taken.
    with _pending_lock:
        for f in futures:
            if f in _pending_futures:
                _pending_futures.remove(f)

    logger.info("[fire_and_forget] shutdown: all futures resolved")


def shutdown_lightrag_loop(timeout: float = 10.0):
    """Stop the LightRAG event loop gracefully.

    First cancels all pending fire-and-forget futures, then stops the loop.
    Called during application shutdown.
    """
    global _loop, _loop_thread

    # Step 1: Cancel pending fire-and-forget futures
    shutdown_pending_futures(timeout=timeout)

    # Step 2: Stop the event loop
    with _loop_lock:
        loop = _loop
        thread = _loop_thread

    if loop is None or not loop.is_running():
        return

    logger.info("[lightrag-loop] Stopping event loop...")

    # Submit loop.stop() from a different thread
    loop.call_soon_threadsafe(loop.stop)

    # Wait for the daemon thread to finish
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
        if thread.is_alive():
            logger.warning("[lightrag-loop] Loop thread did not stop within timeout")
        else:
            logger.info("[lightrag-loop] Loop thread stopped")

    with _loop_lock:
        _loop = None
        _loop_thread = None


# ============== LightRAG Instance ==============

_rag_instance = None
_rag_lock = threading.Lock()

# Init failure tracking: timestamp-based retry gate instead of permanent sentinel.
# After init fails, _init_failed_at records the time. get_lightrag() will
# return None until _INIT_RETRY_SECONDS have elapsed, then retry.
_init_failed_at: Optional[float] = None
_init_error: dict | None = None
_integrity_result: dict | None = None  # Phase 1 一致性检测结果，供 get_lightrag_status 暴露
_INIT_RETRY_SECONDS: float = 60.0

# repair 期间标志：避免 get_lightrag 报 critical 日志，避免 SkillSync 后台轮询误报。
# run_repair_on_user_request 设/清（try/finally 保证异常路径清除）。
# 期间 get_lightrag 静默返回 None，不报 critical 日志。
_repairing: bool = False

# Signaling event: set when LightRAG initializes successfully.
# Other threads call wait_lightrag_ready() instead of polling get_lightrag().
_lightrag_ready = threading.Event()


def _clear_sync_state_if_storage_empty(storage_dir: Path) -> None:
    """Clear sync state caches when lightrag_storage is freshly created/empty.

    When users delete lightrag_storage and restart, the graph starts empty.
    But skill_sync_state.json and last_region_sync.json may still exist,
    causing SkillSync/RegionSync to think everything is already synced
    and skip re-injection. This function detects the empty-graph condition
    and deletes those stale cache files, then notifies the sync services
    to reload their in-memory state.
    """
    entities_file = storage_dir / "kv_store_full_entities.json"
    if not entities_file.exists():
        # Fresh storage — no entities yet, clear all sync state caches
        state_files = [
            Path.home() / ".niu" / "skill_sync_state.json",
            Path.home() / ".niu" / "last_region_sync.json",
        ]
        cleared = False
        for state_file in state_files:
            if state_file.exists():
                try:
                    state_file.unlink()
                    logger.info(f"Cleared stale sync state: {state_file}")
                    cleared = True
                except OSError as e:
                    logger.warning(f"Failed to clear sync state {state_file}: {e}")

        # Notify SkillSync to reload state from disk (now empty)
        if cleared:
            try:
                from agent.injector.sync import get_skill_sync
                skill_sync = get_skill_sync(auto_start=False)
                skill_sync._last_scan = skill_sync._load_state()
                skill_sync._last_notes_scan = skill_sync._load_notes_state()
                logger.info("[LightRAG] SkillSync state reloaded after clearing stale cache")
            except Exception as e:
                logger.warning(f"[LightRAG] Failed to notify SkillSync: {e}")


def _make_local_embedding_func():
    """Create a direct local embedding callable for LightRAG.

    Bypasses the HTTP proxy entirely — calls model.encode() directly.
    Returns numpy ndarray (not list) because LightRAG's EmbeddingFunc
    validates result.size which requires a numpy array.
    Same pattern as the reranker (direct Python callable, zero overhead).
    """
    from niu_api.internal.embedding import get_model

    async def _embed(texts: list[str]):
        model = get_model()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: model.encode(texts, convert_to_numpy=True, show_progress_bar=False))

    return _embed


def _create_lightrag_instance():
    """Create a LightRAG instance with our proxy and local models.

    This is called lazily on first access. LightRAG must be installed
    (pip install lightrag-hku).
    """
    try:
        from lightrag.lightrag import LightRAG
    except ImportError:
        raise ImportError(
            "LightRAG is not installed. Run: pip install lightrag-hku"
        )

    config = _get_lightrag_config()
    embedding_dim = _get_embedding_dim_for_lightrag()

    # Ensure storage directory exists
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Build LLM function using LiteLLMSession (direct call, no proxy).
    # LightRAG calls llm_model_func(prompt, system_prompt=..., **kwargs).
    # LiteLLMSession is cached and reused across calls.
    llm_model_func = _build_llm_model_func()

    # 升级后首次启动后台探测 response_format 档位（不阻塞启动）
    _trigger_background_probe_if_needed()

    # Build embedding function (direct local call, no proxy)
    from niu_api.internal.embedding import get_embedding_max_seq_length
    max_seq_len = get_embedding_max_seq_length()

    embedding_func = EmbeddingFunc(
        embedding_dim=embedding_dim,
        max_token_size=max_seq_len,
        func=_make_local_embedding_func(),
    )

    # Build reranker callable (direct, no proxy)
    from niu_api.internal.reranker import make_lightrag_reranker_callable
    reranker_func = make_lightrag_reranker_callable()

    # Create LightRAG instance
    # Custom entity_types: constrain LLM extraction to these categories.
    # If none match, LLM classifies as "Other" (LightRAG prompt convention).
    # This ensures frontend category buttons match actual graph data.
    CUSTOM_ENTITY_TYPES = [
        "person", "organization", "technology", "concept",
        "location", "event", "document", "photo", "video",
        "note", "chat", "skill", "tool", "knowledge",
        "interactionhabit", "episodicevent", "brainregion", "other",
    ]

    # Read tunable params from preferences.json (lightrag section)
    config = _get_lightrag_config()
    chunk_token_size = config.get("chunk_token_size", 1200)
    chunk_overlap_token_size = config.get("chunk_overlap_token_size", 50)
    llm_model_max_async = config.get("llm_model_max_async", 4)
    entity_extract_max_gleaning = config.get("max_gleaning", 1)

    rag_params = dict(
        working_dir=str(STORAGE_DIR),
        llm_model_func=llm_model_func,
        llm_model_name="proxy-model",
        embedding_func=embedding_func,
        chunk_overlap_token_size=chunk_overlap_token_size,
        chunk_token_size=chunk_token_size,
        llm_model_max_async=llm_model_max_async,
        addon_params={
            "entity_types": CUSTOM_ENTITY_TYPES,
            "language": "Chinese",
            "entity_extract_max_gleaning": entity_extract_max_gleaning,
        },
    )

    logger.info(
        "LightRAG params: chunk_size=%d, chunk_overlap=%d, max_async=%d, max_gleaning=%d",
        chunk_token_size, chunk_overlap_token_size, llm_model_max_async, entity_extract_max_gleaning,
    )

    # Add reranker if configured (lightrag-hku 1.4.15 uses rerank_model_func,
    # not enable_rerank — reranking is implicitly enabled when func is provided)
    if reranker_func is not None:
        rag_params["rerank_model_func"] = reranker_func
        logger.info("LightRAG reranker enabled")
    else:
        logger.info("LightRAG reranker disabled")

    rag = LightRAG(**rag_params)
    # lightrag-hku 1.4.15 requires explicit storage initialization
    call_async(rag.initialize_storages(), timeout=300)
    # If lightrag_storage is freshly created (empty graph), clear sync state caches
    # so that SkillSync/LightRAGSync/RegionSync will re-inject everything
    _clear_sync_state_if_storage_empty(STORAGE_DIR)
    return rag


# We need EmbeddingFunc from lightrag for constructing the embedding function wrapper.
# EmbeddingFunc lives in lightrag.utils in lightrag-hku 1.4.x. LightRAG is a hard
# dependency of this project — placeholder removed so type inference matches.
from lightrag.utils import EmbeddingFunc


def get_lightrag():
    """Get the LightRAG instance (lazy-init on first call).

    三级启动门控（基于 _integrity_result 的 critical/major/minor 计数）：
    - A 级（critical > 0 或 unrecoverable）：拒绝初始化，返回 None
    - B 级（major > 0）：拒绝初始化，返回 None（需用户修复）
    - C 级（仅 minor > 0）：允许初始化，日志警告降级
    - 无 error：正常初始化

    repair 期间（_repairing=True）静默返回 None，不报 critical 日志，
    避免 SkillSync 后台轮询误报。

    Returns None if LightRAG is not installed, init failed recently
    (_INIT_RETRY_SECONDS cooldown), or repair in progress.
    After init failure, waits _INIT_RETRY_SECONDS before retrying so
    the system does not permanently lock up.
    """
    global _rag_instance, _init_failed_at

    # repair 期间静默返回 None（不报 critical 日志，避免 SkillSync 误报）
    if _repairing:
        return None

    # Fast path: already initialized
    if _rag_instance is not None:
        return _rag_instance

    # 三级门控：基于 _integrity_result 的 severity 计数判定
    if _integrity_result is not None:
        critical = _integrity_result.get("critical_errors", 0)
        major = _integrity_result.get("major_errors", 0)
        minor = _integrity_result.get("minor_errors", 0)

        if critical > 0:
            logger.warning(
                f"[LightRAG] 核心数据损坏（{critical} critical errors），拒绝初始化"
            )
            _init_failed_at = time.monotonic()
            return None
        if major > 0:
            logger.warning(
                f"[LightRAG] 数据不一致（{major} major errors），拒绝初始化。请通过修复功能恢复数据。"
            )
            _init_failed_at = time.monotonic()
            return None
        if minor > 0:
            logger.warning(
                f"[LightRAG] 数据有轻微问题（{minor} minor errors），降级启动"
            )
            # 不返回 None，继续初始化（C 级降级）

    # Retry gate: if init failed recently, return None until cooldown expires
    if _init_failed_at is not None:
        elapsed = time.monotonic() - _init_failed_at
        if elapsed < _INIT_RETRY_SECONDS:
            return None
        # Cooldown expired — clear the flag and retry below
        logger.info(
            f"LightRAG init retry cooldown expired ({elapsed:.0f}s), retrying..."
        )
        _init_failed_at = None

    with _rag_lock:
        # Double-check after acquiring lock
        if _rag_instance is not None:
            return _rag_instance

        # repair 期间再次检查（lock 内 double-check）
        if _repairing:
            return None

        # 三级门控 lock 内再次检查（防并发窗口期）
        if _integrity_result is not None:
            critical = _integrity_result.get("critical_errors", 0)
            major = _integrity_result.get("major_errors", 0)
            if critical > 0 or major > 0:
                if _init_failed_at is None:
                    _init_failed_at = time.monotonic()
                return None

        if _init_failed_at is not None:
            elapsed = time.monotonic() - _init_failed_at
            if elapsed < _INIT_RETRY_SECONDS:
                return None
            _init_failed_at = None

        try:
            logger.info("Initializing LightRAG instance...")
            _rag_instance = _create_lightrag_instance()
            logger.info("LightRAG instance ready")
            # Signal readiness to other threads
            _lightrag_ready.set()
        except ImportError as e:
            logger.warning(f"LightRAG not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            _init_failed_at = time.monotonic()
            return None

    return _rag_instance


def wait_lightrag_ready(timeout: float) -> bool:
    """Block until LightRAG is initialized, or until timeout expires.

    Uses threading.Event.wait() internally — no polling, no deadlock.
    Other threads (SkillSync, LightRAGSync, RegionSync) should call
    this instead of polling get_lightrag() in a loop.

    Args:
        timeout: Max seconds to wait. If 0, returns immediately.

    Returns:
        True if LightRAG is ready, False if timeout expired.
    """
    return _lightrag_ready.wait(timeout=timeout)


async def ensure_lightrag():
    """Async version of get_lightrag() for use in async contexts."""
    return get_lightrag()


def is_lightrag_available() -> bool:
    """Check if LightRAG is available (installed and initialized)."""
    try:
        import lightrag  # pyright: ignore[reportUnusedImport]
        return True
    except ImportError:
        return False


def run_resilience_phase1() -> dict:
    """Phase 1（LightRAG eager init 之前）：只做一致性检测。

    v6 修正：不做 cleanup / full_backup（备份是用户自己的事）。
    检测到损坏不自动修复，由 rfd 原生弹窗让用户选'退出'或'尝试修复'。

    Returns:
        {"check_ok": bool, "need_repair": bool, "check_result": dict}
    """
    global _integrity_result
    from niu_api.internal.lightrag_integrity import check_all

    # 只做检测，不动任何文件
    try:
        check_result = check_all()
    except Exception as e:
        logger.warning(f"[LightRAG] 一致性检测失败（不影响启动）: {e}")
        check_result = {"ok": True, "critical_errors": 0, "major_errors": 0, "minor_errors": 0, "error": str(e)}

    _integrity_result = check_result

    critical = check_result.get("critical_errors", 0)
    major = check_result.get("major_errors", 0)
    minor = check_result.get("minor_errors", 0)
    total = critical + major + minor
    logger.info(
        f"[LightRAG] Phase 1 完成: check_ok={check_result.get('ok')}, "
        f"critical={critical}, major={major}, minor={minor}, total={total}"
    )
    return {
        "check_ok": check_result.get("ok", True),
        "need_repair": not check_result.get("ok", True),
        "check_result": check_result,
    }


def should_signal_scheduler_ready(phase1_result: dict) -> bool:
    """Phase 1 后是否通知 scheduler 系统就绪。

    损坏时不通知，让 scheduler 180s 超时强行扫描的漏洞被堵住
    （配合 scheduler.cancel_delayed_start 让超时后 _delayed_start 线程
    直接 return，不强行 start）。

    用户决策退出或修复后，scheduler 跟随程序整体退出，不需要 ready signal。

    Returns:
        True 表示应该调 signal_scheduler_ready()（正常启动）
        False 表示跳过（LightRAG 损坏）
    """
    return not phase1_result.get("need_repair", False)


def should_start_db_monitor(phase1_result: dict) -> bool:
    """Phase 1 后是否启动 db_monitor task。

    损坏时不启动，避免 db_monitor 路由消息到 ChatQueue → runner.chat 报错
    （ChatQueue worker 已 pause，但 db_monitor 入队后只堆积在队列里，
    程序退出时丢失——可接受，损坏期间不应处理 IM 消息）。

    Returns:
        True 表示应该启动 db_monitor（正常启动）
        False 表示跳过（LightRAG 损坏）
    """
    return not phase1_result.get("need_repair", False)


def pause_chatqueue_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时 pause ChatQueue，让 worker 不消费消息。

    用户决策期间 IM/scheduler 入队的消息只堆积在队列里，不触发 runner.chat。
    程序退出时 ChatQueue 跟随整体 shutdown（stop_chat_queue cancel worker task），
    不需要 resume。

    异常处理：pause 失败只 log warning，不抛异常（不阻塞 lifespan 继续走
    Phase 1 后的 gate 流程，最坏情况是 ChatQueue 仍消费，但 scheduler
    被 cancel + db_monitor 未启动 + signal 未发，已堵住 90% 的触发路径）。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.chat_queue import get_chat_queue
            q = get_chat_queue()
            q.pause()
            logger.info("[LightRAG] ChatQueue paused due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to pause ChatQueue: {e}")


def cancel_scheduler_delayed_start_if_corrupt(phase1_result: dict) -> None:
    """Phase 1 检测到损坏时取消 scheduler 的 delayed start。

    补 P1 漏洞：scheduler.start_delayed 的 _ready_event.wait(180) 180s 超时后
    会强行 start（scheduler.py L103-106），即使不调 signal_scheduler_ready，
    scheduler 线程也会在 180s 后启动 + 阻塞 120 秒（_CALLBACK_TIMEOUT）。
    虽然此期间 ChatQueue 被 pause 阻塞不会触发 runner.chat，但 scheduler
    线程跑起来后 180s+120s 才结束，期间用户决策/退出流程会被拖延。

    调 scheduler.cancel_delayed_start() 设 _delayed_start_cancelled=True，
    _delayed_start 线程 180s 超时后检查到 flag 直接 return。

    异常处理：cancel 失败只 log warning，不阻塞 lifespan。
    """
    if phase1_result.get("need_repair", False):
        try:
            from niu_api.internal.scheduler.service import get_scheduler
            sched = get_scheduler()
            if sched is not None:
                sched.cancel_delayed_start()
                logger.info("[LightRAG] Scheduler delayed start cancelled due to LightRAG corruption")
        except Exception as e:
            logger.warning(f"[LightRAG] Failed to cancel scheduler delayed start: {e}")


def run_repair_on_user_request() -> dict:
    """用户在弹窗点'尝试修复'后调用（通过 /api/kg/lightrag/repair 触发）。

    v9：repair_all 内部走 storage.upsert 接口（Task 3-9 重写），
        run_repair_on_user_request 入口保持 v8 行为（停 RegionSync + 不调 get_lightrag/apipeline）。

    修复流程：
        1. 先停 RegionSync（get_region_sync().stop_background_sync_blocking）避免后台写
        2. 设 _repairing=True（让其他线程的 get_lightrag 返回 None，作为信号灯兜底）
        3. 调 repair_all
        4. reset_init_state + 重跑 check_all 更新 _integrity_result
        5. 不调 get_lightrag/apipeline（让下次用户请求自然触发）
        6. 判定 repaired（基于 repair_all._unrecoverable）

    RegionSync 停止策略（v9 第 2 轮审查修复 问题 4 / I4）：
        - 改用 `stop_background_sync_blocking`（join timeout=60，超时抛 RuntimeError）
        - 原 `stop_background_sync` 只 join 5 秒，in-flight sync 任务可能继续写 GraphML
          （见 lightrag-graphml-written-by-regionsync.md 根因）
        - `start_background_sync` 仍是 RegionSync 实例方法（region_sync.py:602）
        - `get_region_sync` 在 region_sync.py:690，返回 RegionSync 单例（不存在则创建）
        - RegionSync 内部调 `get_lightrag()`，但 lightrag_manager.get_lightrag() 在
          `_repairing=True` 时返回 None，所以即使 stop_background_sync_blocking 失败，
          `_repairing=True` 信号灯也能让 RegionSync 的 get_lightrag 拿不到实例，不会写真相源

    Returns:
        {
            "repaired": bool,
            "check_ok": bool,
            "critical_errors": int,
            "major_errors": int,
            "minor_errors": int,
            "repair_result": dict,
            "check_result": dict,
            "_unrecoverable": bool,
        }
    """
    global _integrity_result, _rag_instance, _repairing
    from niu_api.internal.lightrag_repair import repair_all
    from niu_api.internal.lightrag_integrity import check_all

    logger.warning("[LightRAG] 用户选择'尝试修复'，启动 repair_all（v9 storage 接口）")

    # 1. 先停 RegionSync（避免后台写）
    #    v9：用 stop_background_sync_blocking（join timeout=60，超时抛 RuntimeError）
    #    原 stop_background_sync 只 join 5 秒，in-flight sync 任务可能继续写 GraphML
    #    正确调用：get_region_sync() 拿单例，再调实例方法
    try:
        from agent.injector.region_sync import get_region_sync

        rs = get_region_sync()
        if rs is not None:
            # v9 第 2 轮审查修复（问题 4 / I4）：
            # 用 stop_background_sync_blocking 替代 stop_background_sync
            # （join timeout=60，覆盖单次 sync 30+ 秒场景，超时抛 RuntimeError）。
            # 原 stop_background_sync 只 join 5 秒，in-flight sync 任务可能继续写 GraphML
            # （见 lightrag-graphml-written-by-regionsync.md 根因）。
            rs.stop_background_sync_blocking()
            logger.info(
                "[LightRAG] RegionSync 已停止（通过 stop_background_sync_blocking，线程已确认退出）"
            )
        else:
            logger.info("[LightRAG] RegionSync 单例为 None（未启动），跳过停止")
    except Exception as e:  # noqa: BLE001
        # stop 失败不阻塞 repair：_repairing=True 信号灯会让 RegionSync 内部的
        # get_lightrag() 返回 None（lightrag_manager.py:925/973 检查 _repairing），
        # 自然不会写真相源
        logger.warning(
            f"[LightRAG] 停 RegionSync 失败（继续 repair，靠 _repairing 信号灯兜底）: {e}"
        )

    _repairing = True
    try:
        # 2. repair 期间置 _rag_instance = None（避免新 ingest 请求并发写文件竞争）
        _rag_instance = None

        # 3. 调 repair_all（v8：不备份，直接删 9 派生 + 重建）
        repair_result = repair_all()

        # 4. 检查 unrecoverable（顶层标记或单个 result 字段）
        has_unrecoverable = bool(repair_result.get("_unrecoverable", False)) or any(
            isinstance(v, dict) and v.get("unrecoverable")
            for v in repair_result.values()
            if isinstance(v, dict)
        )

        # 5. reset + 重跑 check_all
        reset_init_state()
        try:
            check_result = check_all()
            _integrity_result = check_result
        except Exception as e:
            logger.warning(f"[LightRAG] 修复后 check_all 失败: {e}")
            check_result = _integrity_result or {}

        # 6. v8：不调 get_lightrag/apipeline（铁律 3）
        #    让下次用户请求自然触发 get_lightrag 初始化（从 repair 后的磁盘重建）

        # 7. 判定 repaired（基于 repair_all._unrecoverable）
        repaired = not has_unrecoverable and not repair_result.get("_unrecoverable", False)

        for vdb_name, vdb_result in repair_result.items():
            if not isinstance(vdb_result, dict):
                continue
            if vdb_result.get("status") == "error":
                repaired = False
                logger.warning(
                    f"[LightRAG] 修复失败项: {vdb_name} - {vdb_result.get('message', '')}"
                )

        critical = check_result.get("critical_errors", 0)
        major = check_result.get("major_errors", 0)
        minor = check_result.get("minor_errors", 0)

        logger.info(
            f"[LightRAG] 修复完成: repaired={repaired}, "
            f"重检: critical={critical}, major={major}, minor={minor}"
        )

        return {
            "repaired": repaired,
            "check_ok": check_result.get("ok", True),
            "critical_errors": critical,
            "major_errors": major,
            "minor_errors": minor,
            "repair_result": repair_result,
            "_unrecoverable": bool(repair_result.get("_unrecoverable", False)),
            "check_result": check_result,
        }
    except Exception as e:
        logger.error(f"[LightRAG] 修复失败: {e}")
        return {
            "repaired": False,
            "check_ok": False,
            "critical_errors": 0,
            "major_errors": 0,
            "minor_errors": 0,
            "repair_result": {"error": str(e)},
            "check_result": _integrity_result,
        }
    finally:
        # v9 修复：finally 块不重启 RegionSync
        # 之前 finally 调 rs.start_background_sync() 会在 repair 刚结束时立即重启守护线程，
        # 守护线程跑 _sync_loop → _run_sync_impl → _manage_region_nodes → create_region_nodes
        # 会写 GraphML（创建/合并脑区节点），违反铁律 2（3 真相源不可动）。
        # 守护线程 sync_interval=86400 但有"距上次同步超 21.6h 立即跑首次同步"逻辑，
        # 用户上次同步是昨天 → 重启后立即触发 sync 写 GraphML。
        # 修复方案：repair 完成后不重启 RegionSync，让用户重启程序时由正常启动流程触发。
        # _repairing 信号灯清回 False，让下次 get_lightrag 能正常初始化。
        _repairing = False
        logger.info(
            "[LightRAG] repair 完成，不重启 RegionSync（避免守护线程写真相源）。"
            "用户重启程序时由正常启动流程触发 RegionSync。"
        )


def reset_init_state() -> None:
    """重置初始化失败状态，让下次 get_lightrag 重试。"""
    global _init_failed_at, _init_error
    _init_failed_at = None
    _init_error = None


def get_lightrag_status() -> Dict[str, Any]:
    """Get LightRAG status info for diagnostics."""
    from niu_api.internal.embedding import get_current_model_info
    from niu_api.internal.reranker import get_current_reranker_info

    with _rag_lock:
        initialized = _rag_instance is not None
        init_failed = _init_failed_at is not None
        if init_failed:
            failed_at = _init_failed_at or 0.0
            retry_in = max(0, round(_INIT_RETRY_SECONDS - (time.monotonic() - failed_at), 1))
        else:
            retry_in = None
    with _loop_lock:
        loop_running = _loop is not None and _loop.is_running()

    result = {
        "installed": is_lightrag_available(),
        "initialized": initialized,
        "init_failed": init_failed,
        "init_retry_in_seconds": retry_in,
        "storage_dir": str(STORAGE_DIR),
        "llm_mode": "litellm_direct",
        "embedding": get_current_model_info(),
        "reranker": get_current_reranker_info(),
        "loop_running": loop_running,
    }
    if _integrity_result:
        critical = _integrity_result.get("critical_errors", 0)
        major = _integrity_result.get("major_errors", 0)
        minor = _integrity_result.get("minor_errors", 0)
        integrity_ok = _integrity_result.get("ok", False)
        total_errors = critical + major + minor
    else:
        # _integrity_result 为 None（首次启动 / Phase 1 未跑过）时即时跑 check_all，
        # 避免暴露空 integrity（ok=True）但实际数据已损坏的"假绿"——参考 v5.8 Task 7 修复
        try:
            from niu_api.internal.lightrag_integrity import check_all
            fresh = check_all()
            critical = fresh.get("critical_errors", 0)
            major = fresh.get("major_errors", 0)
            minor = fresh.get("minor_errors", 0)
            integrity_ok = fresh.get("ok", False)
            total_errors = critical + major + minor
        except Exception as e:
            logger.warning(f"[LightRAG] get_lightrag_status 即时 check_all 失败: {e}")
            critical = major = minor = 0
            integrity_ok = True
            total_errors = 0
    # v4: total_errors 是 critical + major + minor 之和，保留是为了向后兼容
    # Rust IntegrityStatus.total_errors（main.rs:54）读这个字段生成弹窗文案，
    # 删了会导致 Rust serde 反序列化报 "missing field"（main.rs:42 注释明确警告过）。
    # 新代码应优先读 critical_errors/major_errors/minor_errors 三级字段。
    result["integrity"] = {
        "ok": integrity_ok,
        "total_errors": total_errors,  # 兼容字段，= critical + major + minor
        "critical_errors": critical,
        "major_errors": major,
        "minor_errors": minor,
    }
    return result


# ============== Graph Change Log ==============

class GraphChangeLog:
    """In-memory change buffer for graph write operations.

    Records entity_created, edge_created, entity_deleted, entity_merged
    events so the frontend can poll /api/kg/changelog for incremental
    updates instead of re-fetching the full snapshot.

    Uses deque with maxlen to bound memory; old entries auto-evict.
    Thread-safe via internal lock.
    """

    def __init__(self, max_size: int = 2000) -> None:
        self._changes: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def record_change(self, change_type: str, data: dict) -> None:
        with self._lock:
            self._changes.append({
                "type": change_type,
                "timestamp": datetime.now().isoformat(),
                "data": data,
            })

    def get_changes(self, since: str = "", limit: int = 200) -> list[dict]:
        """Return changes after *since* timestamp (ISO 8601).

        Does NOT drain — the buffer is preserved so late-arriving polls
        can still catch up.  Old entries are auto-evicted by deque maxlen.

        If *since* is older than the earliest entry in the deque, some
        changes have been evicted and the incremental result is incomplete.
        In that case, a snapshot_refresh event is appended so the frontend
        re-fetches the full snapshot instead of relying on partial data.
        """
        with self._lock:
            if not since:
                return list(self._changes)[-limit:]
            result = [c for c in self._changes if c["timestamp"] > since]
            # Detect overflow: if since is strictly older than the earliest
            # deque entry, some changes were evicted between the frontend's
            # last poll and now, and the incremental result is incomplete.
            # Use strict < (not <=): when since equals the earliest entry's
            # timestamp, the frontend has already processed that entry
            # (syncSince was set to that timestamp), so > since correctly
            # excludes it. Only < means entries were lost before since.
            if self._changes and since < self._changes[0]["timestamp"]:
                result.append({
                    "type": "snapshot_refresh",
                    "timestamp": datetime.now().isoformat(),
                    "data": {"reason": "changelog_overflow"},
                })
            return result[-limit:]


_change_log = GraphChangeLog()


def get_change_log() -> GraphChangeLog:
    return _change_log
