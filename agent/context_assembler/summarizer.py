"""块摘要增强（Task 5，可选层）——裸调 lightrag_llm 副模型为归档块生成 1-2 行摘要。

形态（spec D6 + §8 拍板）：照抄社区命名脑区先例（region_manager._call_llm_for_label）
——lightrag_llm 配置（get_llm_config(use_lightrag_config=True)，用户配置原样透传）、
LiteLLMSession 一次一 call、无 Agent 外壳；输入=块原文有界（>20K 字符截断头尾保留），
输出 1-2 行摘要。成功回写 summary+summary_state='done'；任何失败保持 pending 不抛出
（loguru warning）——机械索引行是兜底主路径，摘要绝不阻塞主路径（D7 确定性优先）。

调度（D13）：仅空闲时段执行（复用 spirit 状态机 is_sleeping 现成信号——睡眠管道
运行期即用户离开的空闲期）；活跃对话期跳过本轮。配置开关 context.blockSummaryEnabled
默认 false（单路并发安全默认，机械索引行独立工作）。每轮批量上限 N=5。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from agent.context_assembler.blocks import (
    SUMMARY_DONE,
    SUMMARY_PENDING,
    PointerBlock,
    default_db_path,
    load_all,
    load_by_ids,
    upsert_blocks,
)

BATCH_LIMIT = 5            # 每轮批量上限 N=5
MAX_INPUT_CHARS = 20_000   # 块原文输入上限：超出截断头尾各半保留
SUMMARY_MAX_CHARS = 100    # 索引行尺寸不变式：摘要行 ≤100 字
_CALL_TIMEOUT_S = 60       # 单次 LLM 调用超时（先例 30s；块输入更大给足余量）

_PROMPT_TEMPLATE = (
    "为以下早期对话片段写一份中文摘要：1-2 行、不超过100字，"
    "概括用户的核心请求与最终结果要点。\n"
    "直接输出摘要文本本身，不要任何前缀、编号、引号或解释。\n\n"
    "<对话片段>\n{block_text}\n</对话片段>"
)

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 配置开关与空闲判定
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    """用户配置路径（测试 monkeypatch 注入点）。"""
    from niu_api.config import CONFIG_PATH

    return Path(CONFIG_PATH)


def read_summary_enabled() -> bool:
    """读 context.blockSummaryEnabled（默认 False——单路并发安全默认）。"""
    try:
        import json

        config = json.loads(_config_path().read_text(encoding="utf-8"))
        return bool(config.get("context", {}).get("blockSummaryEnabled", False))
    except Exception as e:
        logger.debug(f"[BlockSummarizer] 配置读取失败，按禁用处理: {e}")
        return False


def _default_idle_check() -> bool:
    """空闲判定：spirit 状态机处于 sleep（用户离开、睡眠管道时段）即空闲。

    活跃对话期（spirit 被 chat 入口置回 idle）返回 False → 跳过本轮。
    """
    from niu_api.compat import is_sleeping

    return is_sleeping()


# ---------------------------------------------------------------------------
# 裸调 LLM helper（照抄 region_manager._call_llm_for_label 先例）
# ---------------------------------------------------------------------------

def _bare_llm_call(prompt: str) -> str | None:
    """裸调 lightrag_llm 副模型一次一 call，返回回复文本；失败返回 None 不抛出。

    无重试无 Agent 外壳（一次一call 定案）；消费 LiteLLMSession.chat() 流式
    generator，线程超时机制与社区命名先例同款。
    """
    from niu_api.internal.lightrag_manager import _get_litellm_session
    from niu_api.llm_proxy import get_llm_config

    config = get_llm_config(use_lightrag_config=True)  # lightrag 段：副模型用户配置原样
    session = _get_litellm_session(config)
    gen = session.chat(messages=[{"role": "user", "content": prompt}])

    chunks: list[str] = []
    result_holder: list = [None, None]  # [mock_response, exception]

    def _consume():
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
        except StopIteration as e:
            result_holder[0] = e.value
        except Exception as e:
            result_holder[1] = e

    thread = threading.Thread(target=_consume, daemon=True)
    thread.start()
    thread.join(timeout=_CALL_TIMEOUT_S)
    if thread.is_alive():
        logger.warning(f"[BlockSummarizer] LLM 调用超时（{_CALL_TIMEOUT_S}s），放弃本块")
        return None

    mock_resp = result_holder[0]
    if mock_resp is not None and getattr(mock_resp, "stream_error", False):
        logger.warning(f"[BlockSummarizer] LLM stream error: {getattr(mock_resp, 'error_msg', '')}")
        return None
    if mock_resp is not None and getattr(mock_resp, "content", None):
        return mock_resp.content
    if result_holder[1] is not None:
        logger.warning(f"[BlockSummarizer] LLM 调用异常: {result_holder[1]}")
        return None
    text = "".join(chunks).strip()
    return text or None


# ---------------------------------------------------------------------------
# 块原文拉取（有界输入）
# ---------------------------------------------------------------------------

def _load_block_text(block: PointerBlock, messages_db_path: Path) -> str:
    """按 start/end_rowid 闭区间从 messages.db 拉块原文并格式化。

    >MAX_INPUT_CHARS 截断头尾各半保留（中间省略标记）；DB 读失败抛给调用方
    （summarize_block 兜底转 pending 保持）。
    """
    conn = sqlite3.connect(str(messages_db_path))
    try:
        rows = conn.execute(
            "SELECT role, content, tool_call_id, created_at FROM messages "
            "WHERE rowid >= ? AND rowid <= ? ORDER BY rowid ASC",
            (block.start_rowid, block.end_rowid),
        ).fetchall()
    finally:
        conn.close()

    lines = []
    for role, content, tool_call_id, created_at in rows:
        suffix = f"·{tool_call_id}" if role == "tool" and tool_call_id else ""
        prefix = f"{created_at or ''} [{role}{suffix}] "
        lines.append(prefix + str(content or ""))

    text = "\n".join(lines)
    if len(text) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        text = (
            text[:half]
            + "\n…[中间内容已省略]…\n"
            + text[-half:]
        )
    return text


def _normalize_summary(text: str | None) -> str:
    """摘要规范化：压缩空白为单行、去首尾、≤SUMMARY_MAX_CHARS（索引尺寸不变式）。"""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()[:SUMMARY_MAX_CHARS]


# ---------------------------------------------------------------------------
# 单块摘要与批量调度
# ---------------------------------------------------------------------------

def summarize_block(
    block: PointerBlock,
    db_path: Path | None = None,
    messages_db_path: Path | None = None,
) -> bool:
    """为单个 pending 块生成并回写摘要。成功 True；任何失败 False 且保持 pending。

    回写前按 id 重读最新块状态再更新摘要两字段——避免覆盖并发压实的其他字段变更。
    本函数永不抛出。
    """
    path = db_path if db_path is not None else default_db_path()
    mdb = messages_db_path if messages_db_path is not None else _default_messages_db_path()
    try:
        block_text = _load_block_text(block, mdb)
        summary = _normalize_summary(_bare_llm_call(_PROMPT_TEMPLATE.format(block_text=block_text)))
        if not summary:
            logger.warning(f"[BlockSummarizer] 块#{block.id} 摘要为空，保持 pending")
            return False

        # 回写：重读最新行，仅改 summary 两字段（并发压实/重建安全）
        fresh = load_by_ids([block.id], path)
        if not fresh:
            logger.warning(f"[BlockSummarizer] 块#{block.id} 回写时已消失（重建？），跳过")
            return False
        target = fresh[0]
        target.summary = summary
        target.summary_state = SUMMARY_DONE
        upsert_blocks([target], path)
        return True
    except Exception as e:
        logger.warning(f"[BlockSummarizer] 块#{block.id} 摘要失败（保持 pending）: {e}")
        return False


def process_pending_blocks(
    db_path: Path | None = None,
    messages_db_path: Path | None = None,
    *,
    enabled: bool | None = None,
    idle_check: Callable[[], bool] | None = None,
    batch_limit: int = BATCH_LIMIT,
) -> int:
    """批量处理 pending 块摘要（每轮上限 N=batch_limit）。返回成功摘要的块数。

    门控顺序：配置开关（关闭零调用零读盘）→ 空闲判定（活跃对话期跳过本轮）→ 批量。
    任何单块失败不影响其余块，整体永不抛出。
    """
    try:
        if enabled is None:
            enabled = read_summary_enabled()
        if not enabled:
            return 0
        idle = idle_check if idle_check is not None else _default_idle_check
        if not idle():
            logger.info("[BlockSummarizer] 非空闲时段（活跃对话期），跳过本轮")
            return 0

        path = db_path if db_path is not None else default_db_path()
        pending = [b for b in load_all(path) if b.summary_state == SUMMARY_PENDING]
        batch = pending[:batch_limit]
        done = sum(1 for b in batch if summarize_block(b, path, messages_db_path))
        if batch:
            logger.info(f"[BlockSummarizer] 本轮 {len(batch)} 块，成功 {done}")
        return done
    except Exception as e:
        logger.warning(f"[BlockSummarizer] 批量调度异常（忽略）: {e}")
        return 0


def _default_messages_db_path() -> Path:
    return Path.home() / ".niu" / "messages.db"
