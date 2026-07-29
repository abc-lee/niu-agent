"""
Brain Graph — Knowledge graph operations on LightRAG.

Core concepts:
- Niu — the "self" entity, all memory relations start from it
- Entity names use natural language (e.g., "Python", "任飞"), not colon-prefix format
"""

import re
import threading
from typing import Any

from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

# ============== Constants ==============

ENTITY_TYPES = {"Niu", "Person", "Concept", "Skill", "Event", "Project"}

MAX_NAME_LENGTH = 64


# ============== Name Normalization ==============


def normalize_name(raw_name: str) -> str:
    """Normalize a name for the brain: namespace.

    Rules:
    - Replace spaces with underscores
    - Remove special characters except underscore and hyphen
    - Capitalize first letter of each word (PascalCase within segments)
    - Truncate to MAX_NAME_LENGTH characters
    """
    if not raw_name:
        return ""

    name = raw_name.replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-]", "", name)

    # Split by underscore and hyphen, capitalize first letter of each segment
    parts = re.split(r"[_\-]+", name)
    capitalized = []
    for p in parts:
        if not p:
            continue
        # Capitalize first letter only, preserve rest (LiLei stays LiLei)
        capitalized.append(p[0].upper() + p[1:] if len(p) > 1 else p.upper())
    name = "_".join(capitalized)

    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH]

    return name


def make_entity_name(entity_type: str, name: str) -> str:
    """Generate a natural language entity name.

    Format: just the name itself (natural language).
    Special case: "Niu" for the self entity.
    No colon prefixes — LightRAG uses title case extraction which
    conflicts with colon-prefix naming, causing entity fragmentation.
    """
    if entity_type == "Niu" or not name:
        return "Niu"
    return name


# ============== Helpers ==============


# ============== BrainGraph Class ==============


class BrainGraph:
    """Memory brain graph built on LightRAG.

    Stores memories as relations from Niu to entities.
    Retrieves memories via LightRAG aquery(mode="mix").
    """

    def __init__(self):
        self._adapter = LightRAGAdapter()
        self._ingester = LightRAGIngester()

    # ============== Entity Initialization ==============

    def ensure_niu_entity(self) -> dict[str, Any]:
        """Ensure the Niu self entity exists in the graph.

        Idempotent — safe to call on every startup.
        """
        if self._adapter.has_entity("Niu"):
            return {"status": "ok", "message": "实体'Niu'已存在，跳过重复入库", "skipped": True}
        return self._ingester.inject_custom_kg(
            entities=[{
                "entity_name": "Niu",
                "entity_type": "Niu",
                "description": "Self entity — all memory relations start from here",
            }],
            relationships=[],
            chunks=[],
            source_id="brain",
        )


# ============== Singleton ==============

_brain_graph_instance: BrainGraph | None = None
_brain_graph_lock = threading.Lock()


def get_brain_graph() -> BrainGraph:
    """Get the module-level BrainGraph singleton (thread-safe)."""
    global _brain_graph_instance
    if _brain_graph_instance is None:
        with _brain_graph_lock:
            if _brain_graph_instance is None:
                _brain_graph_instance = BrainGraph()
    return _brain_graph_instance
