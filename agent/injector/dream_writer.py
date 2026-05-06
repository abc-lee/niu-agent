"""
DreamWriter — Dream Evolver's brain graph write layer

Provides two separate write interfaces for the brain knowledge graph:
- Pipeline A: Semantic memory (knowledge-type, no time chains)
- Pipeline B: Episodic memory (event-type, with time chains)

Semantic memories are preferences, skills, concepts, and tool relationships.
They use associative retrieval (entity + associated_with/USED_FOR/OFTEN_WITH).

Episodic memories are error/success experiences and decision processes.
They use sequential retrieval (brain:event entities + followed_by/corrected_by chains).

M6 module: Dual-pipeline memory write layer for Dream Evolver.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ============== Constants ==============

# Namespace prefix for episodic event entities
EVENT_PREFIX = "brain:event:"

# Entity type for episodic events
EPISODIC_ENTITY_TYPE = "EpisodicEvent"

# Self entity name (anchor point for all semantic entities)
NIU_ENTITY = "brain:Niu"

# Source identifiers for injected data
DREAM_SOURCE_ID = "dream_evolver"
DREAM_FILE_PATH = "dream://evolver"

# Relation keywords for semantic pipeline
SEMANTIC_RELATION_TYPES = {"USED_FOR", "OFTEN_WITH", "associated_with"}

# Relation keywords for episodic pipeline
CHAIN_RELATION_FOLLOWED = "followed_by"
CHAIN_RELATION_CORRECTED = "corrected_by"
INVOLVES_RELATION = "involves"

# Mapping from entity_type to brain:Niu relation keyword
_NIU_RELATION_MAP = {
    "Person": "remembers",
    "Skill": "skilled_in",
    "Concept": "knows_about",
    "Tool": "uses",
}


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

        Creates entity + brain:Niu → entity relation (prefers/skilled_in/remembers).
        No time chain needed — semantic entities use associative retrieval.

        Args:
            name: Entity name (e.g., "Python", "数据分析").
            entity_type: Entity type (e.g., "Person", "Concept", "Skill").
            description: Entity description.

        Returns:
            Dict with status and details.
        """
        # Step 1: Inject the entity
        entity_result = self._ingester.inject_custom_kg(
            entities=[{
                "entity_name": name,
                "entity_type": entity_type,
                "description": description,
            }],
            relationships=[],
            chunks=[{
                "content": description,
                "source_id": DREAM_SOURCE_ID,
                "file_path": DREAM_FILE_PATH,
            }],
            source_id=DREAM_SOURCE_ID,
        )

        if isinstance(entity_result, dict) and entity_result.get("status") == "error":
            logger.warning(
                "注入语义实体失败: %s — %s",
                name,
                entity_result.get("message", "unknown"),
            )
            return entity_result

        # Step 2: Create brain:Niu → entity relation
        niu_relation = self._determine_niu_relation(entity_type)
        niu_result = self._ingester.inject_custom_kg(
            entities=[],
            relationships=[
                {
                    "src_id": NIU_ENTITY,
                    "tgt_id": name,
                    "keywords": niu_relation,
                    "description": f"brain:Niu {niu_relation} {name}",
                    "weight": 1.0,
                    "source_id": DREAM_SOURCE_ID,
                    "file_path": DREAM_FILE_PATH,
                }
            ],
            chunks=[],
            source_id=DREAM_SOURCE_ID,
        )

        logger.info(
            "写入语义实体: %s (type=%s, niu_relation=%s)",
            name,
            entity_type,
            niu_relation,
        )

        return {
            "status": "ok",
            "entity": entity_result,
            "niu_relation": niu_result,
            "name": name,
            "entity_type": entity_type,
            "niu_relation_keyword": niu_relation,
        }

    def write_semantic_relation(
        self,
        src: str,
        tgt: str,
        relation_type: str,
        description: str = "",
    ) -> dict:
        """Write semantic relation (knowledge-type).

        Directly calls inject_custom_kg with the relationship.
        No time chain needed — semantic relations are associative.

        Args:
            src: Source entity name.
            tgt: Target entity name.
            relation_type: Relation type (USED_FOR, OFTEN_WITH, associated_with).
            description: Optional relation description.

        Returns:
            Dict with status and details.
        """
        result = self._ingester.inject_custom_kg(
            entities=[],
            relationships=[
                {
                    "src_id": src,
                    "tgt_id": tgt,
                    "keywords": relation_type,
                    "description": description or f"{src} {relation_type} {tgt}",
                    "weight": 1.0,
                    "source_id": DREAM_SOURCE_ID,
                    "file_path": DREAM_FILE_PATH,
                }
            ],
            chunks=[],
            source_id=DREAM_SOURCE_ID,
        )

        logger.info(
            "写入语义关系: %s ──%s──→ %s",
            src,
            relation_type,
            tgt,
        )

        return result

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

        Creates brain:event:{name} entity with:
        - entity_type = "EpisodicEvent"

        If prev_event_name provided:
        - If is_correction: inject_relation(prev → current, corrected_by)
        - Else: inject_relation(prev → current, followed_by)

        If related_entities provided:
        - inject_relation(event → entity, involves) for each

        Args:
            event_name: Event name (used as brain:event:{event_name}).
            description: Event description.
            experience_type: "error" or "success".
            related_entities: Optional list of related entity names.
            prev_event_name: Optional previous event name for time chain.
            is_correction: Whether this event corrects the previous one.
            session_id: Optional session ID.

        Returns:
            Dict with status and details.
        """
        full_event_name = f"{EVENT_PREFIX}{event_name}"

        # Step 1: Inject the event entity
        entity_result = self._ingester.inject_custom_kg(
            entities=[{
                "entity_name": full_event_name,
                "entity_type": EPISODIC_ENTITY_TYPE,
                "description": description,
            }],
            relationships=[],
            chunks=[{
                "content": description,
                "source_id": DREAM_SOURCE_ID,
                "file_path": DREAM_FILE_PATH,
            }],
            source_id=DREAM_SOURCE_ID,
        )

        if isinstance(entity_result, dict) and entity_result.get("status") == "error":
            logger.warning(
                "注入事件实体失败: %s — %s",
                full_event_name,
                entity_result.get("message", "unknown"),
            )
            return entity_result

        # Step 2: Time chain relation (if prev_event provided)
        chain_result = None
        if prev_event_name is not None:
            prev_full_name = f"{EVENT_PREFIX}{prev_event_name}"
            chain_keyword = (
                CHAIN_RELATION_CORRECTED if is_correction else CHAIN_RELATION_FOLLOWED
            )
            chain_result = self._ingester.inject_custom_kg(
                entities=[],
                relationships=[
                    {
                        "src_id": prev_full_name,
                        "tgt_id": full_event_name,
                        "keywords": chain_keyword,
                        "description": f"{prev_full_name} {chain_keyword} {full_event_name}",
                        "weight": 1.0,
                        "source_id": DREAM_SOURCE_ID,
                        "file_path": DREAM_FILE_PATH,
                    }
                ],
                chunks=[],
                source_id=DREAM_SOURCE_ID,
            )
            logger.info(
                "事件链: %s ──%s──→ %s",
                prev_event_name,
                chain_keyword,
                event_name,
            )

        # Step 3: involves relations (if related_entities provided)
        involves_results: list[dict] = []
        if related_entities:
            involves_rels = []
            for entity_name in related_entities:
                involves_rels.append(
                    {
                        "src_id": full_event_name,
                        "tgt_id": entity_name,
                        "keywords": INVOLVES_RELATION,
                        "description": f"{full_event_name} involves {entity_name}",
                        "weight": 0.8,
                        "source_id": DREAM_SOURCE_ID,
                        "file_path": DREAM_FILE_PATH,
                    }
                )

            if involves_rels:
                involves_results.append(
                    self._ingester.inject_custom_kg(
                        entities=[],
                        relationships=involves_rels,
                        chunks=[],
                        source_id=DREAM_SOURCE_ID,
                    )
                )
                logger.info(
                    "事件关联: %s involves %s",
                    event_name,
                    ", ".join(related_entities),
                )

        logger.info(
            "写入事件: %s (type=%s, experience=%s)",
            event_name,
            EPISODIC_ENTITY_TYPE,
            experience_type,
        )

        return {
            "status": "ok",
            "entity": entity_result,
            "chain": chain_result,
            "involves": involves_results,
            "event_name": full_event_name,
            "experience_type": experience_type,
        }

    # ============== Helpers ==============

    def _determine_niu_relation(self, entity_type: str) -> str:
        """Determine brain:Niu → entity relation type based on entity_type.

        Args:
            entity_type: The entity type string.

        Returns:
            Relation keyword for the brain:Niu → entity relation.
            Person → "remembers"
            Skill → "skilled_in"
            Concept → "knows_about"
            Tool → "uses"
            Default → "remembers"
        """
        return _NIU_RELATION_MAP.get(entity_type, "remembers")
