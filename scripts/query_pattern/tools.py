#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared tools for Query Pattern TDD Pipeline"""
import sys
from pathlib import Path

# UTF-8 wrapper for Windows (guard against double-wrapping)
if sys.platform == "win32":
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    if not isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import json
from typing import Optional
import numpy as np
from loguru import logger


def get_vector_db_path() -> str:
    """Get vector database path"""
    memory_path = Path.home() / ".niu" / "memory.json"
    if memory_path.exists():
        try:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            workspace_path = memory.get("workspace", {}).get("path")
            if workspace_path and Path(workspace_path).exists():
                return str(Path(workspace_path) / "vectors.db")
        except Exception:
            pass
    return str(Path.home() / ".niu" / "vectors.db")


def get_vector_search():
    """Get VectorSearchAdapter instance"""
    from agent.vector_search import VectorSearchAdapter
    return VectorSearchAdapter(get_vector_db_path())


def get_embedding(content: str) -> Optional[list[float]]:
    """Get L2-normalized embedding vector for content"""
    vs = get_vector_search()
    emb = vs._get_embedding(content)
    if emb is None:
        return None
    vec = np.array(emb, dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def recursive_search(content: str, min_score: float = 0.3) -> tuple[list, float]:
    """
    Execute recursive vector search, returning (results, top_score).

    The vs.search() method already handles two-stage recursion:
    - First searches for query_pattern records
    - If found (is_recursive=True), extracts refined_query and does second search
    - Returns second-stage results (excluding query_patterns)

    Returns:
        results: list of SearchResult (second-stage results)
        top_score: score of top result (0.0 if no results)
    """
    vs = get_vector_search()
    results = vs.search(content, limit=5, min_score=min_score)
    if not results:
        return [], 0.0
    return results, results[0].score


def upsert_pattern(doc_id: str, content: str, metadata: dict) -> bool:
    """Upsert a single query_pattern to vector database"""
    embedding = get_embedding(content)
    if embedding is None:
        logger.error(f"[Writer] Failed to get embedding for: {doc_id}")
        return False

    vec = np.array(embedding, dtype=np.float32)
    embedding_blob = vec.tobytes()

    conn = get_vector_search()._get_connection()
    if conn is None:
        return False

    conn.execute(
        """
        INSERT INTO documents (id, content, embedding, metadata)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content = excluded.content,
            embedding = excluded.embedding,
            metadata = excluded.metadata
        """,
        (doc_id, content, embedding_blob, json.dumps(metadata, ensure_ascii=False)),
    )
    conn.commit()
    return True


def call_llm(prompt: str, system: str = "", temperature: float = 0.9) -> str:
    """
    Call LLM for content generation via MiniMax Anthropic-compatible API.

    Uses httpx directly since litellm routes MiniMax to the wrong endpoint path
    (/v1/chat/completions instead of /v1/messages).

    Reads model config from environment variables (MINIMAX_MODEL, MINIMAX_API_KEY,
    MINIMAX_BASE_URL) or falls back to user-config.json.

    Args:
        prompt: User prompt
        system: System prompt
        temperature: Temperature parameter
    Returns:
        LLM response text (empty string on error)
    """
    import json
    import os
    import httpx

    # Resolve model config: env vars take priority
    model = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7-highspeed")
    api_key = os.environ.get("MINIMAX_API_KEY")
    api_base = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic/v1")

    # Fallback to user-config.json
    if not api_key:
        try:
            config_path = Path(__file__).parent.parent.parent / "config" / "user-config.json"
            if config_path.exists():
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                llm_cfg = cfg.get("llm", {})
                if not api_key:
                    api_key = llm_cfg.get("apiKey")
                if not api_base or api_base == "https://api.minimaxi.com/anthropic/v1":
                    api_base = llm_cfg.get("apiBase", api_base)
                if not model or model == "MiniMax-M2.7-highspeed":
                    model = llm_cfg.get("model", model)
        except Exception:
            pass

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = httpx.post(
            f"{api_base.rstrip('/')}/messages",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            },
            timeout=60.0,
        )
        if resp.status_code != 200:
            logger.error(f"[LLM] HTTP {resp.status_code}: {resp.text[:200]}")
            return ""
        data = resp.json()
        content_list = data.get("content", [])
        if isinstance(content_list, list):
            for item in content_list:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
        return ""
    except Exception as e:
        logger.error(f"[LLM] Error: {e}")
        return ""
