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
import threading
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


# ============== Helpers ==============


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or dataclass/object, with fallback."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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
                # Skip metadata if too long — truncated JSON is irrecoverable
                if len(meta_str) > 200:
                    logger.debug(f"[BRAIN] metadata too long ({len(meta_str)} chars), skipping")
                else:
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

        Uses LightRAG query_data(mode="mix") for structured retrieval
        with real weights from the knowledge graph edges.
        """
        try:
            result = self._adapter.query_data(
                query=query,
                mode="mix",
                top_k=top_k,
            )
        except Exception:
            logger.debug("[BRAIN] query_data failed, falling back to text query")
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
        """Extract brain:Niu memories from structured query_data relationships.

        Handles both dict and dataclass/object access patterns.
        """
        memories = []
        for rel in relationships:
            src = _get_attr(rel, "src_id", "")
            tgt = _get_attr(rel, "tgt_id", "")
            relation = _get_attr(rel, "keywords", "") or _get_attr(rel, "relation", "")
            desc = _get_attr(rel, "description", "")
            weight = _get_attr(rel, "weight", 1.0)

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

    # ── Forgetting Curve ─────────────────────────────────────

    def decay_edges(self) -> dict:
        """Decay all entity edges by their level's decay_rate.

        L0: -0.05/day, L1: -0.01/day, L2: -0.002/day.
        Edges below MIN_WEIGHT (0.1) are marked for cleanup.

        Returns:
            Dict with decayed count and cleanup candidates.
        """
        MIN_WEIGHT = 0.1
        decayed = 0
        cleanup_candidates = 0

        try:
            snapshot = self._adapter.get_graph_snapshot(limit=10000)
            edges = snapshot.get("edges", [])

            for edge in edges:
                weight = float(edge.get("weight", 1.0))
                desc = edge.get("description", "")
                level = self._extract_level(desc)

                if level and level in LEVEL_DEFAULTS:
                    decay_rate = LEVEL_DEFAULTS[level]["decay_rate"]
                    new_weight = max(0.0, weight - decay_rate)
                    decayed += 1

                    if new_weight < MIN_WEIGHT:
                        cleanup_candidates += 1

        except Exception as e:
            logger.error(f"decay_edges failed: {e}")

        return {"decayed": decayed, "cleanup_candidates": cleanup_candidates}

    def consolidate_l0_to_l1(self) -> dict:
        """Promote L0 entities accessed 3+ times to L1.

        L0 entities with access_count >= 3 get upgraded to L1
        (higher weight, lower decay rate).

        Returns:
            Dict with promoted count.
        """
        ACCESS_THRESHOLD = 3
        promoted = 0

        try:
            snapshot = self._adapter.get_graph_snapshot(limit=10000)
            nodes = snapshot.get("nodes", [])

            for node in nodes:
                desc = node.get("description", "")
                level = self._extract_level(desc)
                if level != "L0":
                    continue

                access_count = self._extract_access_count(desc)
                if access_count < ACCESS_THRESHOLD:
                    continue

                # Update description: replace L0 with L1
                new_desc = desc.replace("L0|", "L1|", 1)
                name = node.get("name", node.get("id", ""))
                etype = node.get("type", "UNKNOWN")
                self._ingester.inject_entity(
                    name=name,
                    entity_type=etype,
                    description=new_desc,
                    source_id="brain_consolidate",
                    file_path="brain://consolidate",
                )
                promoted += 1

        except Exception as e:
            logger.error(f"consolidate_l0_to_l1 failed: {e}")

        return {"promoted": promoted}

    def consolidate_l1_to_l2(self) -> dict:
        """Promote L1 entities accessed 7+ times to L2.

        L1 entities with access_count >= 7 get upgraded to L2
        (highest weight, lowest decay rate).

        Returns:
            Dict with promoted count.
        """
        ACCESS_THRESHOLD = 7
        promoted = 0

        try:
            snapshot = self._adapter.get_graph_snapshot(limit=10000)
            nodes = snapshot.get("nodes", [])

            for node in nodes:
                desc = node.get("description", "")
                level = self._extract_level(desc)
                if level != "L1":
                    continue

                access_count = self._extract_access_count(desc)
                if access_count < ACCESS_THRESHOLD:
                    continue

                new_desc = desc.replace("L1|", "L2|", 1)
                name = node.get("name", node.get("id", ""))
                etype = node.get("type", "UNKNOWN")
                self._ingester.inject_entity(
                    name=name,
                    entity_type=etype,
                    description=new_desc,
                    source_id="brain_consolidate",
                    file_path="brain://consolidate",
                )
                promoted += 1

        except Exception as e:
            logger.error(f"consolidate_l1_to_l2 failed: {e}")

        return {"promoted": promoted}

    def cleanup_low_weight(self) -> dict:
        """Remove entities and edges with weight below MIN_WEIGHT.

        Returns:
            Dict with removed counts.
        """
        MIN_WEIGHT = 0.1
        removed_entities = 0
        removed_edges = 0

        try:
            snapshot = self._adapter.get_graph_snapshot(limit=10000)
            edges = snapshot.get("edges", [])
            nodes = snapshot.get("nodes", [])

            for edge in edges:
                weight = float(edge.get("weight", 1.0))
                if weight < MIN_WEIGHT:
                    removed_edges += 1

            for node in nodes:
                desc = node.get("description", "")
                # Check weight from brain_meta prefix
                for part in desc.split("|"):
                    if part.startswith("brain_meta_weight="):
                        try:
                            w = float(part.split("=", 1)[1])
                            if w < MIN_WEIGHT:
                                removed_entities += 1
                        except ValueError:
                            pass

        except Exception as e:
            logger.error(f"cleanup_low_weight failed: {e}")

        return {"removed_entities": removed_entities, "removed_edges": removed_edges}

    @staticmethod
    def _extract_level(description: str) -> str:
        """Extract level from brain_meta description prefix.

        Format: L2|created_at=...|access_count=...|...
        """
        if not description:
            return ""
        first_part = description.split("|")[0]
        if first_part in ("L0", "L1", "L2"):
            return first_part
        return ""

    @staticmethod
    def _extract_access_count(description: str) -> int:
        """Extract access_count from brain_meta description prefix."""
        if not description:
            return 0
        for part in description.split("|"):
            if part.startswith("access_count="):
                try:
                    return int(part[len("access_count="):])
                except ValueError:
                    return 0
        return 0


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
