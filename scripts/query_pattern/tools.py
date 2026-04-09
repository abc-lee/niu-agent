#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared tools for Query Pattern TDD Pipeline"""
import sys
from pathlib import Path

# UTF-8 wrapper for Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
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
    Call LLM for content generation.

    Args:
        prompt: User prompt
        system: System prompt
        temperature: Temperature parameter
    Returns:
        LLM response text (empty string on error)
    """
    import litellm

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = litellm.completion(
            model="minimax/io-optimized",
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"[LLM] Error: {e}")
        return ""
