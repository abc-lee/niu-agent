"""
Niu Reranker Module - Internal

Lazy-loaded reranker for LightRAG and other retrieval pipelines.
Supports pluggable models via ~/.niu/preferences.json lightrag config.

Reranker improves retrieval quality by re-scoring candidate documents
against the query with a cross-encoder model (more accurate but slower
than bi-encoder embedding similarity).

【2026-10-11 本期停用】
用户实测结论：rerank 收益很小、GPU/内存资源耗费很大，与本期待解决目标无关，
故生产侧已不再把它接进 LightRAG（见 lightrag_manager.py 的 reranker_func = None）。
本模块的模型加载/切换/调用逻辑整条保留原样，将来要恢复只需改那一行。
若误配成不受支持的值，本模块会响亮告警并自愈写回 "none"（进程内一次），不会尝试加载；
表内的具体模型名仍会尝试加载（依赖已在 requirements 中保留）。
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from loguru import logger

# ============== Model Config ==============

SUPPORTED_RERANKERS = {
    "bge-reranker-v2-m3": {
        "local_dir": "bge-reranker-v2-m3",
        "hf_id": "BAAI/bge-reranker-v2-m3",
        "desc": "BAAI/bge-reranker-v2-m3 (recommended, multilingual)",
    },
    "bge-reranker-v2-gemma": {
        "local_dir": "bge-reranker-v2-gemma",
        "hf_id": "BAAI/bge-reranker-v2-gemma",
        "desc": "BAAI/bge-reranker-v2-gemma (larger, more accurate)",
    },
    "none": {
        "local_dir": "",
        "hf_id": "",
        "desc": "No reranker (skip reranking step)",
    },
}


# 陈旧配置值自愈：reranker_model 读到不受支持的值时，进程内最多写回一次生效值，
# 避免 get_reranker / make_lightrag_reranker_callable / 诊断查询等每次调用都写盘。
_stale_reranker_healed = False
_stale_reranker_heal_lock = threading.Lock()


def _get_reranker_model_name() -> str:
    """Read reranker model name from preferences.json.

    - 值受支持 / 键缺失或为空：原样返回，不告警、不写回（与旧行为一致）。
    - 值不受支持（有值但不在 SUPPORTED_RERANKERS）：响亮 WARNING（带路径与键名），
      原子写回生效值 "none"（进程内一次）；写回失败绝不抛——告警后继续用生效值。
    """
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            model_name = prefs.get("lightrag", {}).get("reranker_model", "")
            if model_name and model_name in SUPPORTED_RERANKERS:
                return model_name
            if model_name:  # 有值但不受支持：响亮告警 + 自愈写回
                _heal_stale_reranker_model(model_name, prefs_path, prefs)
    except Exception as e:
        logger.debug(f"Failed to read reranker model from preferences: {e}")
    return "none"


def _heal_stale_reranker_model(stale_name: str, prefs_path: Path, prefs: dict) -> None:
    """对不受支持的 reranker_model 配置值告警，并原子写回生效值（进程内一次）。"""
    global _stale_reranker_healed
    logger.warning(
        f"preferences.json ({prefs_path}) 的 lightrag.reranker_model 配置值 "
        f"{stale_name!r} 不受支持，已改用 'none'（当前支持："
        f"{', '.join(sorted(SUPPORTED_RERANKERS))}）"
    )
    with _stale_reranker_heal_lock:
        if _stale_reranker_healed:
            return  # 本进程已尝试过写回（含失败），不再重复写盘
        _stale_reranker_healed = True
    try:
        prefs.setdefault("lightrag", {})["reranker_model"] = "none"
        _atomic_write_json(prefs_path, prefs)
    except Exception as e:
        logger.warning(f"reranker_model 自愈写回失败：{e}；继续使用 'none'")


# ============== Model Loading ==============

_reranker_model = None
_reranker_name: str | None = None


def get_models_dir() -> Path:
    """Get models directory path."""
    import os
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])
    return Path(__file__).parent.parent.parent / "models"


def get_reranker():
    """Get or lazily load the reranker model. Returns None if disabled."""
    global _reranker_model, _reranker_name

    requested = _get_reranker_model_name()

    if requested == "none":
        if _reranker_model is not None:
            _reranker_model = None
            _reranker_name = None
        return None

    # If model already loaded and matches request, return it
    if _reranker_model is not None and _reranker_name == requested:
        return _reranker_model

    # If model changed, unload old one
    if _reranker_model is not None and _reranker_name != requested:
        logger.info(f"Switching reranker model: {_reranker_name} → {requested}")
        _reranker_model = None
        _reranker_name = None

    try:
        from sentence_transformers import CrossEncoder

        models_dir = get_models_dir()
        model_info = SUPPORTED_RERANKERS[requested]
        local_path = models_dir / model_info["local_dir"]

        if local_path.exists():
            logger.info(f"Loading reranker from: {local_path}")
            _reranker_model = CrossEncoder(str(local_path))
        else:
            logger.info(f"No local reranker found at {local_path}, downloading {model_info['hf_id']}...")
            import os
            os.environ.pop("HF_HUB_OFFLINE", None)
            _reranker_model = CrossEncoder(model_info["hf_id"])
            _reranker_model.save(str(local_path))
            logger.info(f"Reranker saved to: {local_path}")

        _reranker_name = requested
        logger.info(f"Reranker model: {model_info['desc']}")

    except Exception as e:
        logger.error(f"Failed to load reranker model: {e}")
        _reranker_model = None
        _reranker_name = None
        return None

    return _reranker_model


def rerank(query: str, documents: list[str], top_k: int = 5) -> list[dict]:
    """Rerank documents against a query.

    Returns list of dicts with 'index', 'text', 'score' keys,
    sorted by score descending, limited to top_k.
    """
    model = get_reranker()
    if model is None:
        # No reranker: return documents with dummy scores
        return [
            {"index": i, "text": doc, "score": 1.0 - i * 0.01}
            for i, doc in enumerate(documents[:top_k])
        ]

    # CrossEncoder.predict returns scores for each (query, doc) pair
    pairs = [[query, doc] for doc in documents]
    scores = model.predict(pairs)

    # Sort by score descending
    ranked = [
        {"index": i, "text": documents[i], "score": float(scores[i])}
        for i in range(len(documents))
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:top_k]


def make_lightrag_reranker_callable():
    """Create a reranker callable for LightRAG.

    LightRAG expects a callable that takes (query, documents) and returns
    a list of (index, score) tuples or similar structure.

    Returns None if reranker is disabled.
    """
    if _get_reranker_model_name() == "none":
        return None

    def lightrag_reranker(query: str, documents: list[str]) -> list[tuple]:
        """LightRAG-compatible reranker callable.

        Args:
            query: The search query
            documents: List of document texts to rerank

        Returns:
            List of (index, score) tuples, sorted by score descending.
        """
        model = get_reranker()
        if model is None:
            return [(i, 1.0) for i in range(len(documents))]

        pairs = [[query, doc] for doc in documents]
        scores = model.predict(pairs)

        results = [(i, float(scores[i])) for i in range(len(documents))]
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    return lightrag_reranker


def get_current_reranker_info() -> dict:
    """Get current reranker info (for diagnostics / system management)."""
    name = _reranker_name or _get_reranker_model_name()
    info = SUPPORTED_RERANKERS.get(name, {})
    return {
        "name": name,
        "desc": info.get("desc", ""),
        "loaded": _reranker_model is not None,
    }


def switch_reranker(new_model: str) -> dict:
    """Switch reranker model at runtime (for system management agent).

    Updates preferences.json and forces model reload on next get_reranker() call.
    """
    global _reranker_model, _reranker_name

    if new_model not in SUPPORTED_RERANKERS:
        return {
            "status": "error",
            "message": f"Unknown model: {new_model}. Supported: {list(SUPPORTED_RERANKERS.keys())}",
        }

    old_name = _reranker_name or _get_reranker_model_name()

    # Update preferences.json (atomic write to prevent corruption)
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        prefs = {}
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        prefs.setdefault("lightrag", {})["reranker_model"] = new_model
        _atomic_write_json(prefs_path, prefs)
    except Exception as e:
        return {"status": "error", "message": f"Failed to update preferences.json: {e}"}

    # Force unload
    _reranker_model = None
    _reranker_name = None

    return {
        "status": "switched",
        "old_model": old_name,
        "new_model": new_model,
        "message": f"Switched reranker: {old_name} → {new_model}. Will load on next use.",
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON file atomically (write to temp, then rename).

    Prevents corruption if the process crashes mid-write.
    """
    content = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp", prefix=path.name, dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if path.exists():
            path.unlink()
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
