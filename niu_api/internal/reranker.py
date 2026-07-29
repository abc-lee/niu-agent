"""
Niu Reranker Module - Internal

Lazy-loaded reranker for LightRAG and other retrieval pipelines.
Supports pluggable models via ~/.niu/preferences.json lightrag config.

Reranker improves retrieval quality by re-scoring candidate documents
against the query with a cross-encoder model (more accurate but slower
than bi-encoder embedding similarity).
"""

import json
import os
import tempfile
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


def _get_reranker_model_name() -> str:
    """Read reranker model name from preferences.json, fallback to 'none'."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            model_name = prefs.get("lightrag", {}).get("reranker_model", "")
            if model_name and model_name in SUPPORTED_RERANKERS:
                return model_name
    except Exception as e:
        logger.debug(f"Failed to read reranker model from preferences: {e}")
    return "none"


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
