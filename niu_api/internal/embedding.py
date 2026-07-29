"""
Niu Embedding Module - Internal

Core embedding functions for vector search, consolidated into niu_api.
No HTTP overhead - direct function calls.

Supports pluggable embedding models via ~/.niu/preferences.json lightrag config.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

import numpy as np
from loguru import logger

# ============== Model Config ==============

# Supported models: (local_dir_name, huggingface_id, default_dim)
SUPPORTED_MODELS = {
    "bge-base-zh-v1.5": {
        "local_dir": "bge-base-zh-v1.5",
        "hf_id": "BAAI/bge-base-zh-v1.5",
        "dim": 768,
        "desc": "BAAI/bge-base-zh-v1.5 (768d, 512 tokens, Chinese optimized)",
    },
    "bge-m3": {
        "local_dir": "bge-m3",
        "hf_id": "BAAI/bge-m3",
        "dim": 1024,
        "desc": "BAAI/bge-m3 multilingual (1024d, 8192 tokens, 2.2GB)",
    },
    "minilm-l12": {
        "local_dir": "paraphrase-multilingual-MiniLM-L12-v2",
        "hf_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "desc": "paraphrase-multilingual-MiniLM-L12-v2 (384d, legacy default)",
    },
}

DEFAULT_MODEL = "bge-base-zh-v1.5"  # BAAI/bge-base-zh-v1.5 (768d, 512 tokens, ~400MB)


def _get_embedding_model_name() -> str:
    """Read embedding model name from preferences.json, fallback to default."""
    try:
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            model_name = prefs.get("lightrag", {}).get("embedding_model", "")
            if model_name and model_name in SUPPORTED_MODELS:
                return model_name
    except Exception as e:
        logger.debug(f"Failed to read embedding model from preferences: {e}")
    return DEFAULT_MODEL


def get_embedding_dim() -> int:
    """Get the dimension of the current embedding model."""
    model_name = _get_embedding_model_name()
    return SUPPORTED_MODELS[model_name]["dim"]


def get_embedding_max_seq_length() -> int:
    """Get the max_seq_length of the loaded embedding model.

    Returns the model's actual max_seq_length (e.g. 512 for bge-base-zh-v1.5).
    If model is not loaded yet, returns the default from SUPPORTED_MODELS config.
    """
    with _model_lock:
        if _model is not None:
            return _model.max_seq_length
    # Model not loaded yet — return a safe default based on model config
    model_name = _get_embedding_model_name()
    if model_name == "bge-m3":
        return 8192
    if model_name == "minilm-l12":
        return 128
    return 512  # bge-base-zh-v1.5


# ============== Model Loading ==============

_model = None
_model_name: str | None = None  # Track which model is loaded
_model_lock = threading.Lock()  # Protect _model / _model_name access


def get_models_dir() -> Path:
    """Get models directory path."""
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])
    # 项目根目录/models (niu_api/internal/embedding.py -> niu_api/internal -> niu_api -> 项目根)
    return Path(__file__).parent.parent.parent / "models"


def get_device():
    """Detect and return optimal device (GPU priority)."""
    try:
        import torch
        if torch.cuda.is_available():
            device = "cuda"
            gpu_name = torch.cuda.get_device_name(0)
            logger.info(f"GPU detected: {gpu_name}")
            return device
    except Exception as e:
        logger.debug(f"GPU detection failed: {e}")

    logger.info("No GPU available, using CPU")
    return "cpu"


def get_model():
    """Get or load the embedding model. Config-driven, local first, GPU priority."""
    global _model, _model_name

    with _model_lock:
        requested_model = _get_embedding_model_name()

        # If model already loaded and matches request, return it
        if _model is not None and _model_name == requested_model:
            return _model

        # If model changed, unload old one
        if _model is not None and _model_name != requested_model:
            logger.info(f"Switching embedding model: {_model_name} → {requested_model}")
            _model = None
            _model_name = None

        try:
            from sentence_transformers import SentenceTransformer

            models_dir = get_models_dir()
            model_info = SUPPORTED_MODELS[requested_model]
            local_path = models_dir / model_info["local_dir"]

            # Detect device
            device = get_device()

            if local_path.exists():
                logger.info(f"Loading embedding model from: {local_path}")
                _model = SentenceTransformer(str(local_path))
            else:
                logger.info(f"No local model found at {local_path}, downloading {model_info['hf_id']}...")
                os.environ.pop("HF_HUB_OFFLINE", None)
                _model = SentenceTransformer(model_info["hf_id"])
                _model.save(str(local_path))
                logger.info(f"Model saved to: {local_path}")

            # Move model to target device
            _model = _model.to(device)
            _model_name = requested_model
            logger.info(f"Embedding model: {model_info['desc']} on {device.upper()}")

        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise

        return _model


# ============== Core Functions ==============


def encode(text: str) -> list[float]:
    """Encode a single text to vector."""
    model = get_model()
    embedding = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return embedding.tolist()


def batch_encode(texts: list[str]) -> list[list[float]]:
    """Encode multiple texts to vectors."""
    model = get_model()
    embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return embeddings.tolist()


def similarity(text1: str, text2: str) -> float:
    """Calculate similarity between two texts."""
    model = get_model()
    embeddings = model.encode([text1, text2], convert_to_numpy=True, show_progress_bar=False)
    vec1, vec2 = embeddings[0], embeddings[1]

    # Cosine similarity
    dot_product = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(dot_product / (norm1 * norm2))


def similarity_vectors(vec1: list[float], vec2: list[float]) -> float:
    """Calculate similarity between two vectors."""
    v1 = np.array(vec1, dtype=np.float32)
    v2 = np.array(vec2, dtype=np.float32)

    dot_product = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(dot_product / (norm1 * norm2))


# ============== Lifecycle ==============


def is_ready() -> bool:
    """Check if model is loaded."""
    with _model_lock:
        return _model is not None


def get_current_model_info() -> dict:
    """Get current model info (for diagnostics / system management)."""
    with _model_lock:
        name = _model_name or _get_embedding_model_name()
        info = SUPPORTED_MODELS.get(name, {})
        return {
            "name": name,
            "dim": info.get("dim", 0),
            "desc": info.get("desc", ""),
            "loaded": _model is not None,
        }


def switch_model(new_model: str) -> dict:
    """Switch embedding model at runtime (for system management agent).

    Updates preferences.json and forces model reload on next get_model() call.
    Returns status dict. Caller is responsible for re-indexing vectors if dim changed.
    """
    global _model, _model_name

    if new_model not in SUPPORTED_MODELS:
        return {
            "status": "error",
            "message": f"Unknown model: {new_model}. Supported: {list(SUPPORTED_MODELS.keys())}",
        }

    with _model_lock:
        old_name = _model_name or _get_embedding_model_name()
        old_dim = SUPPORTED_MODELS[old_name]["dim"]
        new_dim = SUPPORTED_MODELS[new_model]["dim"]

        # Update preferences.json (atomic write to prevent corruption)
        try:
            prefs_path = Path.home() / ".niu" / "preferences.json"
            prefs = {}
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            prefs.setdefault("lightrag", {})["embedding_model"] = new_model
            _atomic_write_json(prefs_path, prefs)
        except Exception as e:
            return {"status": "error", "message": f"Failed to update preferences.json: {e}"}

        # Force unload so next get_model() loads the new one
        _model = None
        _model_name = None

    needs_reindex = old_dim != new_dim
    return {
        "status": "switched",
        "old_model": old_name,
        "new_model": new_model,
        "old_dim": old_dim,
        "new_dim": new_dim,
        "needs_reindex": needs_reindex,
        "message": f"Switched {old_name}→{new_model}. {'Re-index required (dim changed).' if needs_reindex else 'No re-index needed (same dim).'}",
    }


def preload():
    """Preload the model (call at startup)."""
    model_name = _get_embedding_model_name()
    dim = SUPPORTED_MODELS[model_name]["dim"]
    logger.info(f"Preloading embedding model: {model_name} ({dim}d)...")
    model = get_model()
    # Force load weights by encoding a dummy text
    model.encode("init", convert_to_numpy=True, show_progress_bar=False)
    logger.info(f"Embedding model ready: {model_name} ({dim}d)")


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
        # On Windows, need to remove target first (os.replace fails if target exists)
        if path.exists():
            path.unlink()
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
