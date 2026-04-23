"""
Brain Graph — Memory system on LightRAG knowledge graph.

Replaces the flat L0/L1/L2 vector-based memory system with a structured
knowledge graph where memories are relations between entities.

Core concepts:
- brain:Niu — the "self" entity, all memory relations start from it
- brain:{type}:{name} — namespaced entity names (Person, Concept, Skill, Event, Project)
- Memory levels (L0/L1/L2) map to relation types and weights
- Retrieval uses LightRAG aquery(mode="mix") directly
"""

import json
import re
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

LEVEL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "L0": {"weight": 0.3, "decay_rate": 0.05, "relation_type": "related_to"},
    "L1": {"weight": 0.7, "decay_rate": 0.01, "relation_type": "remembers"},
    "L2": {"weight": 0.9, "decay_rate": 0.002, "relation_type": "remembers"},
}

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
    """Generate a brain-namespaced entity name.

    Format: brain:{type}:{normalized_name}
    Special case: brain:Niu (no name segment for the self entity).
    """
    if entity_type == "Niu" or not name:
        return "brain:Niu"
    normalized = normalize_name(name)
    return f"brain:{entity_type}:{normalized}"


# ============== BrainGraph Class ==============


class BrainGraph:
    """Memory brain graph built on LightRAG.

    Stores memories as relations from brain:Niu to entities.
    Retrieves memories via LightRAG aquery(mode="mix").
    """

    def __init__(self):
        self._adapter = LightRAGAdapter()
        self._ingester = LightRAGIngester()

    # ============== Entity Initialization ==============

    def ensure_niu_entity(self) -> Dict[str, Any]:
        """Ensure the brain:Niu self entity exists in the graph.

        Idempotent — safe to call on every startup.
        """
        return self._ingester.inject_entity(
            name="brain:Niu",
            entity_type="Niu",
            description="Self entity — all memory relations start from here",
            source_id="brain",
            file_path="brain://Niu",
        )

    # ============== Memory Storage ==============

    def store_memory(
        self,
        content: str,
        level: str = "L0",
        memory_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store a memory in the brain graph.

        Creates:
        1. A target entity via inject_entity
        2. A weighted relation from brain:Niu to the target via inject_custom_kg

        Args:
            content: The memory content to store.
            level: Memory level — L0 (raw), L1 (summary), L2 (insight).
            memory_type: Memory category (environment/preferences/skills/experiences/facts).
            metadata: Optional additional metadata.

        Returns:
            Dict with status and details.
        """
        if level not in LEVEL_DEFAULTS:
            level = "L0"

        level_config = LEVEL_DEFAULTS[level]
        weight = level_config["weight"]

        # Determine relation type
        if memory_type and memory_type in MEMORY_TYPE_TO_RELATION:
            relation_type = MEMORY_TYPE_TO_RELATION[memory_type]
        else:
            relation_type = level_config["relation_type"]

        # Determine entity type from memory_type
        entity_type = self._infer_entity_type(memory_type, level)

        # Create target entity name
        entity_label = self._extract_entity_label(content)
        target_name = make_entity_name(entity_type, entity_label)

        # Build relation description, embedding metadata if present
        description = content[:200]
        if metadata:
            try:
                meta_str = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                # Truncate to avoid exceeding reasonable description length
                if len(meta_str) > 200:
                    meta_str = meta_str[:200] + "..."
                description = f"{description} [meta:{meta_str}]"
            except (TypeError, ValueError):
                pass  # Non-serializable metadata, skip

        # Inject target entity
        entity_result = self._ingester.inject_entity(
            name=target_name,
            entity_type=entity_type,
            description=content[:200],
            source_id="brain",
            file_path="brain://memory",
        )

        if entity_result.get("status") == "error":
            return entity_result

        # Inject weighted relation via inject_custom_kg
        # inject_relation doesn't support weight, so we use inject_custom_kg directly
        relation_result = self._ingester.inject_custom_kg(
            entities=[],
            relationships=[
                {
                    "src_id": "brain:Niu",
                    "tgt_id": target_name,
                    "relation": relation_type,
                    "description": description,
                    "weight": weight,
                    "source_id": "brain",
                    "file_path": "brain://memory",
                }
            ],
            chunks=[],
        )

        if isinstance(relation_result, dict) and relation_result.get("status") == "error":
            return relation_result

        return {
            "status": "ok",
            "level": level,
            "relation_type": relation_type,
            "target_entity": target_name,
            "weight": weight,
        }

    # ============== Memory Recall ==============

    def recall_memories(
        self,
        query: str,
        top_k: int = 10,
        min_weight: float = DEFAULT_MIN_WEIGHT,
    ) -> List[Dict[str, Any]]:
        """Recall memories from the brain graph.

        Uses LightRAG _query_data(mode="mix") for structured retrieval
        with real weights from the knowledge graph edges.
        """
        try:
            result = self._adapter._query_data(
                query=query,
                mode="mix",
                top_k=top_k,
            )
        except Exception:
            # Fallback to text-based query if _query_data fails
            result = None

        if result and isinstance(result, dict):
            data = result.get("data", result)
            relationships = data.get("relationships", [])
            if relationships:
                return self._extract_brain_memories_from_structured(
                    relationships, min_weight
                )

        # Fallback: text-based extraction from aquery
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

    def _infer_entity_type(self, memory_type: Optional[str], level: str) -> str:
        """Infer entity type from memory_type and level."""
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
        first_sentence = re.split(r"[。.！!？?；;]", content)[0]
        label = first_sentence.strip()[:30]
        if not label:
            label = content.strip()[:30]
        return label if label else "Unknown"

    def _extract_brain_memories_from_structured(
        self, relationships: Any, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract brain:Niu memories from structured _query_data relationships.

        Each relationship is expected to have src_id, tgt_id, keywords/relation,
        description, and weight attributes.
        """
        memories = []
        for rel in relationships:
            # Handle both dict and object-style access
            src = rel.get("src_id", "") if isinstance(rel, dict) else getattr(rel, "src_id", "")
            tgt = rel.get("tgt_id", "") if isinstance(rel, dict) else getattr(rel, "tgt_id", "")
            relation = (
                rel.get("keywords", "") or rel.get("relation", "")
                if isinstance(rel, dict)
                else getattr(rel, "keywords", "") or getattr(rel, "relation", "")
            )
            desc = rel.get("description", "") if isinstance(rel, dict) else getattr(rel, "description", "")
            weight = rel.get("weight", 1.0) if isinstance(rel, dict) else getattr(rel, "weight", 1.0)

            # Only include relations involving brain: namespace
            is_brain = src.startswith("brain:") or tgt.startswith("brain:")
            if not is_brain:
                continue

            if weight >= min_weight:
                memories.append({
                    "target": tgt if tgt.startswith("brain:") else src,
                    "relation_type": relation,
                    "description": desc,
                    "weight": weight,
                })

        return sorted(memories, key=lambda m: m.get("weight", 0), reverse=True)

    def _extract_brain_memories_from_text(
        self, text: str, min_weight: float
    ) -> List[Dict[str, Any]]:
        """Extract brain:Niu memory references from query result text."""
        memories = []

        pattern = r"brain:([\w-]+):([\w-]+)"
        for match in re.finditer(pattern, text):
            full_name = match.group(0)
            weight = 0.7  # Default for recalled memories

            if weight >= min_weight:
                memories.append({
                    "target": full_name,
                    "relation_type": "remembers",
                    "description": "",
                    "weight": weight,
                })

        if not memories and text.strip():
            memories.append({
                "target": "brain:Niu",
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
        if target.startswith("brain:"):
            parts = target.split(":", 2)
            display_name = parts[-1] if len(parts) > 2 else parts[-1]

        relation_display = {
            "prefers": "偏好",
            "skilled_in": "擅长",
            "remembers": "",
            "located_at": "位于",
            "learned_from": "从...学到",
            "participated_in": "参与",
            "related_to": "",
        }.get(relation_type, "")

        if description:
            lines.append(f"- {description}")
        elif relation_display:
            lines.append(f"- 你{relation_display}{display_name}")
        else:
            lines.append(f"- {display_name}")

    return "\n".join(lines)


# ============== Singleton ==============

_brain_graph_instance: Optional[BrainGraph] = None


def get_brain_graph() -> BrainGraph:
    """Get the module-level BrainGraph singleton."""
    global _brain_graph_instance
    if _brain_graph_instance is None:
        _brain_graph_instance = BrainGraph()
    return _brain_graph_instance
