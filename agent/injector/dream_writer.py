"""
DreamWriter — Dream Evolver's brain graph write layer

Provides two separate write interfaces for the brain knowledge graph:
- Pipeline A: Semantic memory (knowledge-type, no time chains)
- Pipeline B: Episodic memory (event-type, with time chains)

Semantic memories are preferences, skills, concepts, and tool relationships.
They use associative retrieval (entity + associated_with/USED_FOR/OFTEN_WITH).

Episodic memories are error/success experiences and decision processes.
They use sequential retrieval (event entities + followed_by/corrected_by chains).

M6 module: Dual-pipeline memory write layer for Dream Evolver.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Namespace prefix for episodic event entities (deprecated — events use natural language names)
EVENT_PREFIX = ""

# Entity type for episodic events
EPISODIC_ENTITY_TYPE = "EpisodicEvent"

# Relation keywords for semantic pipeline
# Format convention: "语义关系: {src} —[{relation}]→ {tgt}。"
# The —[X]→ arrow syntax gives LightRAG a clear directional cue for
# extracting src → tgt relationships with the relation type as the edge label.

# Relation keywords for episodic pipeline
CHAIN_RELATION_FOLLOWED = "followed_by"
CHAIN_RELATION_CORRECTED = "corrected_by"
INVOLVES_RELATION = "involves"

# _NIU_RELATION_MAP removed — niu only connects to brain regions


class DreamWriter:
    """Dream Evolver's brain graph write layer

    Provides two separate write interfaces:
    - Pipeline A: Semantic memory (knowledge-type, no time chains)
    - Pipeline B: Episodic memory (event-type, with time chains)

    Usage::

        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()
        writer = DreamWriter(ingester)

        # Pipeline A: Semantic
        writer.write_semantic_entity("Python", "Skill", "Programming language")
        writer.write_semantic_relation("Python", "Django", "USED_FOR", "Web development")

        # Pipeline B: Episodic
        writer.write_episodic_event(
            "tool_x_failed", "Tool X returned error", "error",
            prev_event_name="tried_tool_x", is_correction=True,
        )
    """

    def __init__(self, ingester: Any) -> None:
        """Initialize DreamWriter with a LightRAGIngester instance.

        Args:
            ingester: LightRAGIngester instance for graph writes.
        """
        self._ingester = ingester

    # ============== Pipeline A: Semantic Memory ==============

    def write_semantic_entity(
        self,
        name: str,
        entity_type: str,
        description: str,
    ) -> dict:
        """Write semantic entity (knowledge-type).

        Formats entity as structured text and inserts via lightrag_insert,
        letting LightRAG auto-extract entities/relations/merge.

        Args:
            name: Entity name (e.g., "Python", "数据分析").
            entity_type: Entity type (e.g., "Person", "Concept", "Skill").
            description: Entity description.

        Returns:
            Dict with status and details.
        """
        text = f"语义记忆: {name}（类型: {entity_type}），{description}。"

        try:
            result = self._ingester.lightrag_insert(content=text)
            if isinstance(result, dict) and result.get("status") != "ok":
                logger.warning("语义实体入库返回非ok: name=%s, result=%s", name, result)
                return result
            logger.info(
                "语义实体入库完成: %s (type=%s)",
                name,
                entity_type,
            )
            return result
        except Exception as e:
            logger.error("语义实体入库失败: %s, error=%s", name, e)
            return {"status": "error", "message": str(e)}

    def write_semantic_relation(
        self,
        src_name: str,
        tgt_name: str,
        relation: str,
        description: str = "",
    ) -> dict:
        """Write semantic relation (knowledge-type).

        Formats relation as structured text and inserts via lightrag_insert,
        letting LightRAG auto-extract and link entities.

        Args:
            src_name: Source entity name.
            tgt_name: Target entity name.
            relation: Relation type (e.g., USED_FOR, OFTEN_WITH).
            description: Optional relation description.

        Returns:
            Dict with status and details.
        """
        text = f"语义关系: {src_name} —[{relation}]→ {tgt_name}。"
        if description:
            text += f" {description}。"

        try:
            result = self._ingester.lightrag_insert(content=text)
            if isinstance(result, dict) and result.get("status") != "ok":
                logger.warning("语义关系入库返回非ok: src=%s, tgt=%s, result=%s", src_name, tgt_name, result)
                return result
            logger.info("语义关系入库完成: %s %s %s", src_name, relation, tgt_name)
            return result
        except Exception as e:
            logger.error("语义关系入库失败: %s %s %s, error=%s", src_name, relation, tgt_name, e)
            return {"status": "error", "message": str(e)}

    # ============== Pipeline B: Episodic Memory ==============

    def write_episodic_event(
        self,
        event_name: str,
        description: str,
        experience_type: str,
        related_entities: list[str] | None = None,
        prev_event_name: str | None = None,
        is_correction: bool = False,
        session_id: str | None = None,
    ) -> dict:
        """Write episodic event (event-type).

        Formats event as structured text and inserts via lightrag_insert,
        letting LightRAG auto-extract entities/relations/merge.

        Args:
            event_name: Event name (natural language, e.g., "海滩日落事件").
            description: Event description.
            experience_type: "error" or "success".
            related_entities: Optional list of related entity names.
            prev_event_name: Optional previous event name for time chain.
            is_correction: Whether this event corrects the previous one.
            session_id: Optional session ID.

        Returns:
            Dict with status and details.
        """
        valid_types = {"error", "success"}
        if experience_type not in valid_types:
            return {"status": "error", "message": f"Invalid experience_type '{experience_type}'. Must be one of: {sorted(valid_types)}"}

        text_parts = [f"情景记忆: {event_name}（类型: {experience_type}），{description}。"]

        chain_keyword: str | None = None
        if prev_event_name is not None:
            chain_keyword = CHAIN_RELATION_CORRECTED if is_correction else CHAIN_RELATION_FOLLOWED
            text_parts.append(f"{prev_event_name} {chain_keyword} {event_name}。")

        if related_entities:
            entities_str = "、".join(related_entities)
            text_parts.append(f"{event_name} involves {entities_str}。")

        if session_id:
            text_parts.append(f"来自会话 {session_id}。")

        text = " ".join(text_parts)

        try:
            result = self._ingester.lightrag_insert(content=text)

            if isinstance(result, dict) and result.get("status") != "ok":
                logger.warning("情景事件入库返回非ok: event=%s, result=%s", event_name, result)
                return result

            if chain_keyword is not None:
                logger.info(
                    "事件链: %s ──%s──→ %s",
                    prev_event_name,
                    chain_keyword,
                    event_name,
                )

            if related_entities:
                logger.info(
                    "事件关联: %s involves %s",
                    event_name,
                    ", ".join(related_entities),
                )

            logger.info(
                "情景事件入库完成: %s (type=%s, experience=%s)",
                event_name,
                EPISODIC_ENTITY_TYPE,
                experience_type,
            )
            return result
        except Exception as e:
            logger.error("情景事件入库失败: %s, error=%s", event_name, e)
            return {"status": "error", "message": str(e)}

    # ============== Helpers ==============

    # _determine_niu_relation removed — niu only connects to brain regions
