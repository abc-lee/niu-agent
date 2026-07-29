"""LightRAG 修复用独立 tokenizer + chunk_config 加载器（v8-Task 2）。

设计目的：避免调 get_lightrag / get_lightrag_for_repair / apipeline_process_enqueue_documents
（铁律 3），独立加载 tokenizer 和读 chunk 配置。

API：
- get_tokenizer() -> TiktokenTokenizer | None
  用 lightrag.utils.TiktokenTokenizer（model_name="gpt-4o-mini"），单例缓存。
- get_chunk_config() -> tuple[int, int]
  读 niu_api.internal.lightrag_manager._get_lightrag_config（只读 preferences.json，
  不调 apipeline）。fallback (1200, 50)（对齐 lightrag_manager.py:853 真实默认值）。
- reset_cache() -> None
  清理 tokenizer 单例缓存（测试用）。

注意：绝不调用 get_lightrag/get_lightrag_for_repair/apipeline（铁律 3）。
"""
from __future__ import annotations

from loguru import logger

# 单例缓存（避免每次调用都重新加载 tiktoken encoding）
_tokenizer_cache: object | None = None
_tokenizer_failed: bool = False  # 标记加载失败过，避免重复打日志


def get_tokenizer() -> object | None:
    """独立加载 TiktokenTokenizer（不调 get_lightrag_for_repair，铁律 3）。

    用 lightrag.utils.TiktokenTokenizer（model_name="gpt-4o-mini"）。
    单例缓存：首次加载后缓存，后续直接返回。
    失败返回 None（标记 _tokenizer_failed，不重复尝试）。

    Returns:
        TiktokenTokenizer 实例（有 encode/decode 方法），或 None。
    """
    global _tokenizer_cache, _tokenizer_failed

    if _tokenizer_cache is not None:
        return _tokenizer_cache
    if _tokenizer_failed:
        return None

    try:
        from lightrag.utils import TiktokenTokenizer

        tokenizer = TiktokenTokenizer(model_name="gpt-4o-mini")
        _tokenizer_cache = tokenizer
        logger.info("[LightRAGRepair] 独立加载 TiktokenTokenizer(gpt-4o-mini) 成功")
        return tokenizer
    except Exception as e:  # noqa: BLE001
        _tokenizer_failed = True
        logger.error(f"[LightRAGRepair] 加载 TiktokenTokenizer 失败: {e}")
        return None


def get_chunk_config() -> tuple[int, int]:
    """读 chunk_token_size + chunk_overlap_token_size（不调 get_lightrag，铁律 3）。

    从 niu_api.internal.lightrag_manager._get_lightrag_config 读（只读 preferences.json，
    不调 apipeline）。
    fallback (1200, 50)（与 lightrag_manager.py:853 真实默认值一致）。

    Returns:
        (chunk_token_size, chunk_overlap_token_size)
    """
    try:
        from niu_api.internal.lightrag_manager import _get_lightrag_config

        config = _get_lightrag_config()
        chunk_token_size = int(config.get("chunk_token_size", 1200))
        chunk_overlap = int(config.get("chunk_overlap_token_size", 50))
        return chunk_token_size, chunk_overlap
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[LightRAGRepair] 读 chunk_config 失败，用 fallback (1200, 50): {e}"
        )
        return 1200, 50


def reset_cache() -> None:
    """清理 tokenizer 单例缓存（测试用）。

    清 _tokenizer_cache + _tokenizer_failed 标志，让下次 get_tokenizer() 重新加载。
    """
    global _tokenizer_cache, _tokenizer_failed
    _tokenizer_cache = None
    _tokenizer_failed = False
