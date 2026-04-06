"""
Niu Embedding Module - Internal

Core embedding functions for vector search, consolidated into niu_api.
No HTTP overhead - direct function calls.

Original: mcp-servers/embedding-service/src/niu_embedding_service/__init__.py
"""

import os
from pathlib import Path

import numpy as np
from loguru import logger

# ============== Model Loading ==============

_model = None


def get_models_dir() -> Path:
    """Get models directory path."""
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])
    # 项目根目录/models
    return Path(__file__).parent.parent.parent.parent / "models"


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
    """Get or load the embedding model. Local first, GPU priority."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer

            models_dir = get_models_dir()

            # Detect device
            device = get_device()

            # Prefer multilingual model (better for Chinese)
            multilingual_model = models_dir / "paraphrase-multilingual-MiniLM-L12-v2"
            legacy_model = models_dir / "all-MiniLM-L6-v2"

            if multilingual_model.exists():
                logger.info(f"Loading multilingual embedding model from: {multilingual_model}")
                _model = SentenceTransformer(str(multilingual_model))
                logger.info("Embedding model loaded: paraphrase-multilingual-MiniLM-L12-v2")
            elif legacy_model.exists():
                logger.info(f"Loading legacy embedding model from: {legacy_model}")
                _model = SentenceTransformer(str(legacy_model))
                logger.info("Embedding model loaded: all-MiniLM-L6-v2")
            else:
                logger.info("No local model found, downloading multilingual model...")
                os.environ.pop("HF_HUB_OFFLINE", None)
                _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
                _model.save(str(multilingual_model))
                logger.info(f"Model saved to: {multilingual_model}")

            # Move model to target device
            _model = _model.to(device)
            logger.info(f"Model running on: {device.upper()}")

        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise

    return _model


# ============== Core Functions ==============


def encode(text: str) -> list[float]:
    """Encode a single text to vector."""
    model = get_model()
    embedding = model.encode(text, convert_to_numpy=True)
    return embedding.tolist()


def batch_encode(texts: list[str]) -> list[list[float]]:
    """Encode multiple texts to vectors."""
    model = get_model()
    embeddings = model.encode(texts, convert_to_numpy=True)
    return embeddings.tolist()


def similarity(text1: str, text2: str) -> float:
    """Calculate similarity between two texts."""
    model = get_model()
    embeddings = model.encode([text1, text2], convert_to_numpy=True)
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
    return _model is not None


def preload():
    """Preload the model (call at startup)."""
    logger.info("Preloading embedding model...")
    model = get_model()
    # Force load weights by encoding a dummy text
    model.encode("init", convert_to_numpy=True)
    logger.info("Embedding model ready")
