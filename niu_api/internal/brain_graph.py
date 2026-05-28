"""
Brain Graph — Memory system on LightRAG knowledge graph.

Memories are stored as weighted relations from Niu to typed entities,
and retrieved via LightRAG query_data(mode="mix").

Core concepts:
- Niu — the "self" entity, all memory relations start from it
- Entity names use natural language (e.g., "Python", "任飞"), not colon-prefix format
- memory_type drives relation type (MEMORY_TYPE_TO_RELATION) and entity type; weight defaults to DEFAULT_WEIGHT
- Retrieval uses LightRAG aquery(mode="mix") directly
"""

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from niu_api.internal.lightrag_adapter import LightRAGAdapter, LightRAGIngester

# ============== Constants ==============

ENTITY_TYPES = {"Niu", "Person", "Concept", "Skill", "Event", "Project"}

MEMORY_TYPE_TO_RELATION: Dict[str, str] = {
    "environment": "located_at",
    "preferences": "prefers",
    "skills": "skilled_in",
    "experiences": "remembers",
    "facts": "remembers",
}

# Default weight and relation type when memory_type is not specified
DEFAULT_WEIGHT = 0.7
DEFAULT_RELATION_TYPE = "remembers"

DEFAULT_MIN_WEIGHT = 0.3
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


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or dataclass/object, with fallback.

    Returns `default` when the key is missing OR explicitly None,
    preventing None from leaking into comparisons (e.g. weight >= min_weight).
    """
    if isinstance(obj, dict):
        val = obj.get(key, default)
        return val if val is not None else default
    val = getattr(obj, key, default)
    return val if val is not None else default


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

    def ensure_niu_entity(self) -> Dict[str, Any]:
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

    # ============== Memory Storage ==============

    def store_memory(
        self,
        content: str,
        memory_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store a memory in the brain graph.

        Creates a target entity and a weighted relation from Niu to it
        in a single atomic inject_custom_kg call, with the entity description
        passed as a chunk so LLM can extract additional relationships.

        Args:
            content: The memory content to store.
            memory_type: Memory category (environment/preferences/skills/experiences/facts).
            metadata: Optional additional metadata.

        Returns:
            Dict with status and details.
        """
        # Determine relation type and weight from memory_type
        if memory_type and memory_type in MEMORY_TYPE_TO_RELATION:
            relation_type = MEMORY_TYPE_TO_RELATION[memory_type]
        else:
            relation_type = DEFAULT_RELATION_TYPE

        weight = DEFAULT_WEIGHT

        # Determine entity type from memory_type
        entity_type = self._infer_entity_type(memory_type)

        # Create target entity name
        entity_label = self._extract_entity_label(content)
        target_name = make_entity_name(entity_type, entity_label)

        # Build relation description, embedding metadata if present
        description = content[:200]
        if metadata:
            try:
                meta_str = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                # Skip metadata if too long — truncated JSON is irrecoverable
                if len(meta_str) > 200:
                    logger.debug(f"[BRAIN] metadata too long ({len(meta_str)} chars), skipping")
                else:
                    description = f"{description} [meta:{meta_str}]"
            except (TypeError, ValueError):
                pass  # Non-serializable metadata, skip

        # Build entity description with created_at timestamp only
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        entity_description = f"created_at={created_at}|{content[:200]}"

        # --- Single atomic inject_custom_kg call ---
        # Merge entity + relationship + chunk into one call so that:
        # 1. The operation is atomic — no orphan entity if relation fails
        # 2. Chunk carries the entity description text, letting LLM extract
        #    additional relationships from the description content
        result = self._ingester.inject_custom_kg(
            entities=[{
                "entity_name": target_name,
                "entity_type": entity_type,
                "description": entity_description,
            }],
            relationships=[
                {
                    "src_id": "Niu",
                    "tgt_id": target_name,
                    "keywords": relation_type,
                    "description": description,
                    "weight": weight,
                    "source_id": "brain",
                    "file_path": "brain://memory",
                }
            ],
            chunks=[{
                "content": f"{target_name}: {entity_description}",
                "source_id": "brain",
            }],
            source_id="brain",
        )

        if isinstance(result, dict) and result.get("status") == "error":
            return result

        return {
            "status": "ok",
            "entity_name": target_name,
            "entity_type": entity_type,
            "relation_type": relation_type,
            "weight": weight,
            "memory_type": memory_type,
        }

    # ============== Memory Recall ==============

    def recall_memories(
        self,
        query: str,
        top_k: int = 10,
        min_weight: float = DEFAULT_MIN_WEIGHT,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Recall memories from the brain graph.

        Uses LightRAG query_data(mode="mix") for structured retrieval
        with real weights from the knowledge graph edges.

        Args:
            keywords: If provided, skip LLM keyword extraction (program auto-call).
                      If None, let LLM extract keywords (Agent-initiated call).
        """
        try:
            result = self._adapter.query_data(
                query=query,
                mode="mix",
                top_k=top_k,
                keywords=keywords,
            )
        except Exception:
            logger.debug("[BRAIN] query_data failed, falling back to text query")
            result = None

        if result and isinstance(result, dict):
            data = result.get("data", result)
            relationships = data.get("relationships") or []
            if relationships:
                return self._extract_brain_memories_from_structured(
                    relationships, min_weight
                )

        # Fallback: text-based extraction from aquery.
        # query() always invokes LLM for text generation, so keywords bypass
        # is not applicable here. This fallback is a quality-over-speed path
        # triggered only when query_data() fails.
        result_text = self._adapter.query(
            query=query,
            mode="mix",
            only_need_context=True,
            top_k=top_k,
        )

        if not result_text:
            return []

        return self._extract_brain_memories_from_text(result_text, min_weight)

    # ============== Internal Helpers ==============

    def _infer_entity_type(self, memory_type: Optional[str]) -> str:
        """Infer entity type from memory_type."""
        type_to_entity = {
            "skills": "Skill",
            "preferences": "Concept",
            "environment": "Concept",
            "experiences": "Event",
            "facts": "Concept",
        }
        if memory_type and memory_type in type_to_entity:
            return type_to_entity[memory_type]
        return "Concept"

    def _extract_entity_label(self, content: str) -> str:
        """Extract a short entity label from content."""
        if not content:
            return "Unknown"
        # Split by Chinese sentence-enders first, then English period
        # but only when followed by space or end (avoid splitting "Python 3.12")
        first_sentence = re.split(r"[。！？；]|(?<=[a-zA-Z])\.\s|\.\s", content)[0]
        label = first_sentence.strip()[:30]
        if not label:
            label = content.strip()[:30]
        return label if label else "Unknown"

    def _extract_brain_memories_from_structured(
        self, relationships: Any, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract Niu-related memories from structured query_data relationships.

        Handles both dict and dataclass/object access patterns.
        """
        memories = []
        for rel in relationships:
            src = _get_attr(rel, "src_id", "")
            tgt = _get_attr(rel, "tgt_id", "")
            relation = _get_attr(rel, "keywords", "") or _get_attr(rel, "relation", "")
            desc = _get_attr(rel, "description", "")
            weight = _get_attr(rel, "weight", 1.0)

            # Include relations involving Niu or any entity
            is_niu_related = src == "Niu" or tgt == "Niu"
            if not is_niu_related:
                continue

            if weight >= min_weight:
                memories.append({
                    "target": tgt if tgt != "Niu" else src,
                    "relation_type": relation,
                    "description": desc,
                    "weight": weight,
                })

        return sorted(memories, key=lambda m: m.get("weight", 0), reverse=True)

    def _extract_brain_memories_from_text(
        self, text: str, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract Niu memory references from query result text."""
        memories = []

        # Match "Niu" as a standalone word (word boundary) to avoid
        # false positives like "Niurou" or other substrings containing "Niu".
        if re.search(r"\bNiu\b", text):
            weight = 0.7  # Default for recalled memories
            if weight >= min_weight:
                memories.append({
                    "target": "Niu",
                    "relation_type": "remembers",
                    "description": text.strip()[:200],
                    "weight": weight,
                })

        if not memories and text.strip():
            memories.append({
                "target": "Niu",
                "relation_type": "remembers",
                "description": text.strip()[:200],
                "weight": 0.5,
            })

        return memories


# ============== Prompt Formatting ==============


def format_memories_for_prompt(memories: List[Dict[str, Any]]) -> str:
    """Format brain graph memories for system prompt injection.

    Sorts by weight descending before formatting.
    """
    if not memories:
        return ""

    # Sort by weight descending
    sorted_memories = sorted(memories, key=lambda m: m.get("weight", 0), reverse=True)

    lines = ["### [记忆]"]
    for mem in sorted_memories:
        target = mem.get("target", "")
        relation_type = mem.get("relation_type", "")
        description = mem.get("description", "")

        display_name = target

        relation_display = {
            "prefers": "偏好",
            "skilled_in": "擅长",
            "remembers": "",
            "located_at": "位于",
            "learned_from": "从...学到",
            "participated_in": "参与",
            "related_to": "",
            "knows_about": "了解",
        }.get(relation_type, "")

        if description:
            # Strip embedded metadata from user-visible prompt
            display_desc = re.sub(r"\s*\[meta:.*?\]", "", description)
            lines.append(f"- {display_desc}")
        elif relation_display:
            lines.append(f"- 你{relation_display}{display_name}")
        else:
            lines.append(f"- {display_name}")

    return "\n".join(lines)


# ============== Singleton ==============

_brain_graph_instance: Optional[BrainGraph] = None
_brain_graph_lock = threading.Lock()


def get_brain_graph() -> BrainGraph:
    """Get the module-level BrainGraph singleton (thread-safe)."""
    global _brain_graph_instance
    if _brain_graph_instance is None:
        with _brain_graph_lock:
            if _brain_graph_instance is None:
                _brain_graph_instance = BrainGraph()
    return _brain_graph_instance
