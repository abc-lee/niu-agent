"""
LightRAG Adapter & Ingester

Provides two interfaces to the LightRAG instance:
- LightRAGAdapter: query interface (replaces vector-store search + kg-server query)
- LightRAGIngester: dual-path injection (structured via ainsert_custom_kg,
                    unstructured via ainsert)

Both delegate to lightrag_manager for the LightRAG instance and use
call_async() for the async/sync bridge.
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from niu_api.internal.lightrag_manager import call_async, get_lightrag

# Valid query modes for LightRAG
VALID_MODES = {"naive", "local", "global", "hybrid", "mix", "bypass"}

# LightRAG's fail_response constant substring markers.
# Used to detect the canned error text that LightRAG returns when queries
# produce no results.  These are not LLM-generated responses — they are
# hard-coded fallback strings that must never leak into system prompts.
_LIGHTRAG_ERROR_MARKERS = ("not able to provide", "[no-context]")


class LightRAGAdapter:
    """Query interface for LightRAG.

    Replaces vector-store search and kg-server query with unified
    LightRAG retrieval supporting multiple modes.
    """

    # Supported entity types for filtered search
    ENTITY_TYPES = {"skill", "tool", "knowledge", "person", "photo", "concept", "interaction_habit"}

    @staticmethod
    def _is_no_result(result: Optional[Dict[str, Any]]) -> bool:
        """Check whether a LightRAG query_data result is effectively empty.

        Detects all documented "no result" scenarios from aquery_data:
        - None (adapter caught an exception)
        - {} empty dict (tokenizer missing, empty keywords, etc.)
        - {"status": "failure", ...} (KG search found nothing)
        - {"status": "success", "data": {"entities": [], ...}} (bypass mode,
          all inner lists empty)

        Prioritises structural fields (status, data content) over string
        matching so the check is robust against LightRAG version changes.

        Args:
            result: Return value from query_data().

        Returns:
            True if the result contains no usable data.
        """
        if result is None:
            return True

        if not isinstance(result, dict):
            return True

        if not result:
            # Empty dict — tokenizer missing, empty keywords, etc.
            return True

        # Structural check: explicit failure status from LightRAG
        status = result.get("status")
        if status == "failure":
            return True

        # Check data payload: if all list-valued fields are empty, treat as
        # no result (covers bypass mode with empty entities/relationships/chunks)
        data = result.get("data")
        if isinstance(data, dict):
            if data:
                # Data dict exists but all list values are empty
                list_values = [v for v in data.values() if isinstance(v, list)]
                if list_values and all(not v for v in list_values):
                    return True
            else:
                # Empty data dict with success status — still no results
                return True

        return False

    @staticmethod
    def _is_error_text(text: Optional[str]) -> bool:
        """Check whether a text result is a LightRAG canned error message.

        Detects the fail_response constant that LightRAG returns via the
        aquery() string path:
            "Sorry, I'm not able to provide an answer to that question.[no-context]"

        This is NOT an LLM-generated response — it is a hard-coded fallback
        that must never leak into system prompts.

        Args:
            text: A string query result from query() / aquery().

        Returns:
            True if the text is a LightRAG error/fail response.
        """
        if not text or not isinstance(text, str):
            return False
        lower = text.lower()
        return any(marker in lower for marker in _LIGHTRAG_ERROR_MARKERS)

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    def query(
        self,
        query: str,
        mode: str = "mix",
        only_need_context: bool = True,
        top_k: Optional[int] = None,
        response_type: Optional[str] = None,
    ) -> Optional[str]:
        """Query the brain graph / knowledge base.

        Args:
            query: The search query string.
            mode: Retrieval mode (naive, local, global, hybrid, mix, bypass).
            only_need_context: If True, return context without LLM generation.
            top_k: Number of top items to retrieve.
            response_type: Response format (e.g., "Bullet Points").

        Returns:
            Query result string, or None on error.

        Raises:
            ValueError: If mode is invalid.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {sorted(VALID_MODES)}"
            )

        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, query failed")
            return None

        try:
            from lightrag import QueryParam

            param = QueryParam(mode=mode, only_need_context=only_need_context)
            if top_k is not None:
                param.top_k = top_k
            if response_type is not None:
                param.response_type = response_type

            result = call_async(rag.aquery(query, param=param))
            # aquery() type is str | AsyncIterator[str]; with only_need_context=True
            # it always returns str. Guard against non-string just in case.
            if not isinstance(result, str):
                return None
            if self._is_error_text(result):
                logger.debug("LightRAG query() returned fail_response, filtering out")
                return ""
            return result

        except Exception as e:
            logger.error(f"LightRAG query failed: {e}")
            return None

    def query_data(
        self,
        query: str,
        mode: str = "local",
        top_k: Optional[int] = None,
        keywords: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Query LightRAG returning structured data (entities + relationships).

        Uses aquery_data() instead of aquery() to get structured results with
        entity_type information, which is needed for type-based filtering.

        When keywords are provided, skips LLM keyword extraction for near-instant
        results while keeping full graph traversal. Without keywords, LLM extraction
        adds 5-30s latency.

        Args:
            query: The search query string.
            mode: Retrieval mode (default "local" for entity-focused).
            top_k: Number of top items to retrieve.
            keywords: Pre-provided search keywords to skip LLM extraction.
                For "local" mode: used as ll_keywords (entity search).
                For "global"/"hybrid"/"mix": used as both hl and ll keywords.

        Returns:
            Structured query result dict, or None on error.
        """
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {sorted(VALID_MODES)}"
            )

        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, query_data failed")
            return None

        try:
            from lightrag import QueryParam

            param = QueryParam(mode=mode)
            if top_k is not None:
                param.top_k = top_k
            if keywords:
                param.ll_keywords = keywords
                if mode in ("global", "hybrid", "mix"):
                    param.hl_keywords = keywords

            result = call_async(rag.aquery_data(query, param=param))
            return result

        except Exception as e:
            logger.error(f"LightRAG query_data failed: {e}")
            return None

    def filter_by_entity_type(
        self,
        query_result: Optional[Dict[str, Any]],
        entity_type: str,
    ) -> List[Dict[str, Any]]:
        """Filter LightRAG query results by entity_type.

        LightRAG's aquery_data() returns structured results containing entities
        with entity_type fields. This method extracts only entities matching
        the requested type.

        Args:
            query_result: Structured result from query_data(), or None.
            entity_type: Entity type to filter for (e.g., "skill", "tool").

        Returns:
            List of entity dicts matching the requested type.
        """
        if query_result is None:
            return []

        # aquery_data returns {status, message, data: {entities, relationships, chunks}}
        data = query_result.get("data", {})
        if not data:
            # Fallback: query_result might be the data dict directly
            data = query_result

        entities = data.get("entities", [])
        if not entities:
            return []

        # Normalize entity_type for case-insensitive comparison
        target_type = entity_type.lower().strip()
        filtered = []
        for entity in entities:
            et = entity.get("entity_type", "")
            if et and et.lower().strip() == target_type:
                filtered.append(entity)

        return filtered

    # ============== Multi-Category Search ==============

    # Mapping from LightRAG entity_type to the category keys used by
    # _inject_dynamic_resources() in runner.py.
    _ENTITY_TYPE_TO_CATEGORY = {
        "skill": "skill",
        "tool": "mcp_tool",
        "knowledge": "knowledge",
        "concept": "knowledge",
    }

    def search_multi_lightrag(
        self,
        query: str,
        mode: str = "local",
        top_k: int = 20,
        keywords: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Single-query multi-category search via LightRAG.

        Performs one query_data() call and groups entities by entity_type
        into the category buckets used by _inject_dynamic_resources().

        This is the primary retrieval method for dynamic resource injection,
        replacing vector_search.search_multi() as the main path.

        When keywords are provided, skips LLM keyword extraction for near-instant
        results while keeping full graph traversal. Without keywords, LLM extraction
        adds 5-30s latency.

        Args:
            query: Search query string.
            mode: LightRAG retrieval mode (default "local" for entity-focused).
            top_k: Total number of entities to retrieve.
            keywords: Pre-provided search keywords to skip LLM extraction.
                For "local" mode: used as ll_keywords (entity search).
                For "global"/"hybrid"/"mix": used as both hl and ll keywords.

        Returns:
            Dict with keys "skill", "mcp_tool", "knowledge", each mapping
            to a list of entity dicts. Empty lists on error or no results.
        """
        result: Dict[str, List[Dict[str, Any]]] = {
            "skill": [],
            "mcp_tool": [],
            "knowledge": [],
        }

        query_result = self.query_data(
            query, mode=mode, top_k=top_k, keywords=keywords,
        )
        if self._is_no_result(query_result):
            logger.debug("LightRAG search_multi_lightrag: query_data returned no results")
            return result

        # Extract entities from query_data result
        data = query_result.get("data", {})
        if not data:
            data = query_result
        entities = data.get("entities", [])
        if not entities:
            return result

        # Group by entity_type → category
        for entity in entities:
            et = entity.get("entity_type", "").lower().strip()
            category = self._ENTITY_TYPE_TO_CATEGORY.get(et)
            if category and category in result:
                result[category].append(entity)

        return result

    # ============== Semantic Search Methods ==============

    def search_skills(
        self,
        query: str,
        top_k: int = 10,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for skill entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="skill".

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of skill entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "skill")

    def search_tools(
        self,
        query: str,
        top_k: int = 10,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for tool entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="tool".

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of tool entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "tool")

    def search_knowledge(
        self,
        query: str,
        top_k: int = 10,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for knowledge and concept entities in the knowledge graph.

        Uses local mode (entity-focused) and filters by entity_type="knowledge"
        or entity_type="concept" (both are semantic knowledge).

        Args:
            query: Search query string.
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of knowledge/concept entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        knowledge_entities = self.filter_by_entity_type(result, "knowledge")
        concept_entities = self.filter_by_entity_type(result, "concept")
        return knowledge_entities + concept_entities

    def search_interaction_habits(
        self,
        query: str,
        top_k: int = 10,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for interaction_habit entities in the knowledge graph.

        Interaction habits store tool usage patterns (e.g. dialect preferences,
        success/failure counts) as LightRAG entities with entity_type="interaction_habit".

        The entity name follows the pattern "habit:{habit_type}:{target_tool}"
        and the description contains the habit content plus confidence data.

        Args:
            query: Search query string (typically tool args or context).
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of interaction_habit entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "interaction_habit")

    # ============== Graph Traversal Methods ==============

    def explore_node(self, entity_name: str, depth: int = 2, edge_types: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get neighbors of an entity in the knowledge graph.

        Uses LightRAG's built-in get_knowledge_graph() method which performs
        BFS from the given node and returns a structured subgraph.

        Returns structured data compatible with the kg-server explore_node
        output format: {center, nodes, edges, stats}.

        Args:
            entity_name: Entity name to explore from.
            depth: BFS traversal depth (1-5, default 2).
            edge_types: Optional filter — only return edges whose relation
                        is in this list. None returns all edges.

        Returns:
            Dict with center, nodes, edges, and stats keys.
            Returns empty result on error or if entity not found.
        """
        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, explore_node failed")
            return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}

        depth = max(1, min(5, depth))

        try:
            # LightRAG's get_knowledge_graph performs BFS from node_label
            # Returns KnowledgeGraph(nodes=[KnowledgeGraphNode], edges=[KnowledgeGraphEdge])
            kg = call_async(
                rag.get_knowledge_graph(entity_name, max_depth=depth)
            )

            if kg is None or (not kg.nodes and not kg.edges):
                return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}

            # Build center from the first node (should be the queried entity)
            center = None
            if kg.nodes:
                first_node = kg.nodes[0]
                center = {
                    "id": first_node.id,
                    "name": first_node.id,
                    "type": first_node.properties.get("entity_type", "UNKNOWN"),
                }

            # Convert KnowledgeGraphNode to frontend-compatible format
            nodes = []
            for node in kg.nodes:
                nodes.append({
                    "id": node.id,
                    "name": node.id,
                    "type": node.properties.get("entity_type", "UNKNOWN"),
                    "description": node.properties.get("description", ""),
                })

            # Convert KnowledgeGraphEdge to frontend-compatible format
            edges = []
            for edge in kg.edges:
                edges.append({
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.properties.get("keywords", ""),
                    "description": edge.properties.get("description", ""),
                    "weight": edge.properties.get("weight", 1.0),
                })

            # Filter edges by edge_types if specified
            if edge_types is not None:
                edge_type_set = set(edge_types)
                edges = [e for e in edges if e.get("relation") in edge_type_set]

            return {
                "center": center,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }

        except Exception as e:
            logger.error(f"LightRAG explore_node failed: {e}")
            return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}

    def timeline_query(
        self,
        query: str,
        start_entities: Optional[List[str]] = None,
        direction: str = "backward",
        max_depth: int = 2,
        top_k: int = 5,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """时间线查询：向量匹配内容 → 遍历时间链 → 按时间戳排序。

        Uses query_data() for vector search to find matching entities,
        then explore_node() to traverse time-chain relations from those entities.
        Only edges with timeline relation types are included in results.

        Args:
            query: 查询文本。
            start_entities: 直接指定起始实体名，跳过向量匹配步骤。
            direction: 排序方向 — "backward"（最近优先）或 "forward"（最早优先）。
            max_depth: 时间链遍历深度。
            top_k: 向量搜索返回的实体数。
            max_results: 返回结果最大数量。

        Returns:
            按时间戳排序的时间线结果列表。
        """
        TIMELINE_EDGE_TYPES = {"followed_by", "corrected_by", "led_to", "resolved_by"}

        # Step 1: Determine starting entities
        if start_entities:
            entity_names = start_entities
        else:
            # Vector search to find matching entities (Agent-initiated, let LLM extract keywords)
            query_result = self.query_data(
                query, mode="local", top_k=top_k,
            )

            if query_result is None:
                return []

            data = query_result.get("data", {}) if isinstance(query_result, dict) else {}
            if not data:
                data = query_result
            entities = data.get("entities", [])
            if not entities:
                return []

            entity_names = [
                e.get("entity_name", "") if isinstance(e, dict) else str(e)
                for e in entities[:top_k]
            ]

        # Step 2: Traverse time-chain relations from matched entities
        timeline_items: List[Dict[str, Any]] = []
        seen_entities: set = set()

        for entity_name in entity_names:
            if not entity_name or entity_name in seen_entities:
                continue
            seen_entities.add(entity_name)

            # Add the matched entity itself
            # Try to get entity description from explore_node(depth=0)
            entity_desc = ""
            try:
                node_data = self.explore_node(entity_name, depth=0)
                for node in node_data.get("nodes", []):
                    if node.get("id") == entity_name or node.get("name") == entity_name:
                        entity_desc = node.get("description", "")
                        break
            except Exception:
                pass

            timestamp = self._extract_timestamp(entity_desc)
            timeline_items.append({
                "entity_name": entity_name,
                "description": entity_desc,
                "timestamp": timestamp,
                "relation": "match",
            })

            # Traverse edges via explore_node, keeping only timeline edges
            try:
                node_data = self.explore_node(entity_name, depth=max_depth)
            except Exception:
                continue

            # explore_node returns edges at top level, not nested in nodes
            sub_edges = node_data.get("edges", []) if isinstance(node_data, dict) else []
            for edge in sub_edges:
                relation = edge.get("relation", "")
                if relation not in TIMELINE_EDGE_TYPES:
                    continue
                edge_desc = edge.get("description", "")
                edge_timestamp = self._extract_timestamp(edge_desc)
                target_name = edge.get("target", "")
                if target_name and target_name not in seen_entities:
                    seen_entities.add(target_name)
                    timeline_items.append({
                        "entity_name": target_name,
                        "description": edge_desc,
                        "timestamp": edge_timestamp,
                        "relation": relation,
                    })

        # Step 3: Sort by timestamp based on direction
        reverse_sort = direction == "backward"
        timeline_items.sort(
            key=lambda x: x.get("timestamp") or "",
            reverse=reverse_sort,
        )

        return timeline_items[:max_results]

    @staticmethod
    def _extract_timestamp(description: str) -> str:
        """从 brain_meta 描述前缀中提取 created_at 时间戳。

        格式: L2|created_at=2026-04-27T14:00:00|其他内容

        Args:
            description: Entity/edge description string with brain_meta prefix.

        Returns:
            Extracted timestamp string, or empty string if not found.
        """
        if not description:
            return ""
        for part in description.split("|"):
            if part.startswith("created_at="):
                return part[len("created_at="):]
        return ""

    def get_graph_snapshot(self, limit: int = 200) -> Dict[str, Any]:
        """Return all nodes and edges from LightRAG knowledge graph.

        Uses LightRAG's chunk_entity_relation_graph.get_knowledge_graph("*")
        with max_nodes limit to return the full graph for frontend visualization.
        The "*" label triggers the "return all nodes" path in LightRAG.

        Args:
            limit: Maximum number of nodes to return (default 200).

        Returns:
            Dict with nodes and edges lists for frontend visualization.
            Returns empty result on error.
        """
        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, get_graph_snapshot failed")
            return {"nodes": [], "edges": []}

        try:
            # Use get_knowledge_graph("*") which returns all nodes sorted by degree
            # This is more efficient than get_all_nodes() for large graphs
            kg = call_async(
                rag.get_knowledge_graph("*", max_depth=1, max_nodes=limit)
            )

            if kg is None:
                return {"nodes": [], "edges": []}

            nodes = []
            for node in kg.nodes:
                nodes.append({
                    "id": node.id,
                    "name": node.id,
                    "type": node.properties.get("entity_type", "UNKNOWN"),
                    "description": node.properties.get("description", ""),
                })

            edges = []
            for edge in kg.edges:
                edges.append({
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.properties.get("keywords", ""),
                    "description": edge.properties.get("description", ""),
                    "weight": edge.properties.get("weight", 1.0),
                })

            return {"nodes": nodes, "edges": edges}

        except Exception as e:
            logger.error(f"LightRAG get_graph_snapshot failed: {e}")
            return {"nodes": [], "edges": []}

    # ============== Management Methods ==============

    def delete_entity(self, entity_name: str) -> Dict[str, Any]:
        """Delete an entity and all its relations from the knowledge graph.

        Args:
            entity_name: Entity name to delete.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.adelete_by_entity(entity_name))
            return {"status": "ok", "entity_name": entity_name, "result": str(result)}
        except Exception as e:
            logger.error(f"LightRAG delete_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def document_status(self) -> Dict[str, Any]:
        """Get document processing status counts.

        Returns:
            Dict with pending, processing, processed, failed counts.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            return call_async(rag.get_processing_status())
        except Exception as e:
            logger.error(f"LightRAG document_status failed: {e}")
            return {"status": "error", "message": str(e)}

    def list_entities(
        self,
        list_type: str = "entities",
        entity_type: str = "",
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List entities or documents in the knowledge base.

        Args:
            list_type: "entities", "documents", or "labels".
            entity_type: Filter by entity type.
            limit: Max results.

        Returns:
            Dict with status and data (list of entities/documents/labels).
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            if list_type == "labels":
                data = call_async(rag.get_graph_labels())
                return {"status": "ok", "data": data}
            elif list_type == "documents":
                data = call_async(rag.get_docs_by_status("processed"))
                return {"status": "ok", "data": data}
            elif list_type == "entities":
                kg = call_async(
                    rag.get_knowledge_graph(
                        entity_type or "",
                        max_depth=1,
                        max_nodes=limit,
                    )
                )
                if kg is None:
                    return {"status": "ok", "data": []}
                nodes = []
                for node in kg.nodes:
                    node_type = node.properties.get("entity_type", "UNKNOWN")
                    if entity_type and node_type.lower() != entity_type.lower():
                        continue
                    nodes.append({
                        "id": node.id,
                        "entity_type": node_type,
                        "description": node.properties.get("description", ""),
                    })
                return {"status": "ok", "data": nodes}
            else:
                return {"status": "error", "message": f"Unknown list_type: {list_type}"}
        except Exception as e:
            logger.error(f"LightRAG list_entities failed: {e}")
            return {"status": "error", "message": str(e)}

    def merge_entities(
        self,
        source_entities: List[str],
        target_entity: str,
    ) -> Dict[str, Any]:
        """Merge multiple entities into one, consolidating all relations.

        Args:
            source_entities: Entity names to merge.
            target_entity: Name of the merged target entity.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(
                rag.amerge_entities(source_entities, target_entity)
            )
            return {"status": "ok", "target_entity": target_entity, "result": str(result)}
        except AttributeError:
            return {"status": "error", "message": "Entity merge not supported by this LightRAG version"}
        except Exception as e:
            logger.error(f"LightRAG merge_entities failed: {e}")
            return {"status": "error", "message": str(e)}


class LightRAGIngester:
    """Dual-path data injection for LightRAG.

    Structured path: inject_entity, inject_relation, inject_custom_kg
    → calls ainsert_custom_kg() for precise entity/relation insertion.

    Unstructured path: inject_document, inject_documents
    → calls ainsert() for LLM-driven entity extraction.
    """

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    # ============== Structured Path ==============

    def inject_entity(
        self,
        name: str,
        entity_type: str,
        description: str = "",
        source_id: str = "custom_kg",
        chunk_content: Optional[str] = None,
        file_path: str = "custom_kg",
    ) -> Dict[str, Any]:
        """Inject a single entity into the brain graph.

        Args:
            name: Entity name (also used as entity_id in the graph).
            entity_type: Type classification (e.g., "ProgrammingLanguage").
            description: Entity description for retrieval.
            source_id: Source document/chunk ID.
            chunk_content: Optional chunk text for vector retrieval.
            file_path: File path for citation.

        Returns:
            Dict with status and details.
        """
        chunks = []
        if chunk_content:
            chunks.append({
                "content": chunk_content,
                "source_id": source_id,
                "file_path": file_path,
            })

        entities = [{
            "entity_name": name,
            "entity_type": entity_type,
            "description": description,
            "source_id": source_id,
            "file_path": file_path,
        }]

        return self.inject_custom_kg(
            entities=entities,
            relationships=[],
            chunks=chunks,
            source_id=source_id,
        )

    def upsert_interaction_habit(
        self,
        habit_type: str,
        content: str,
        target_tool: str,
        confidence: Optional[Dict[str, Any]] = None,
        source_id: str = "custom_kg",
    ) -> Dict[str, Any]:
        """Upsert an interaction habit entity into the knowledge graph.

        Interaction habits are stored as LightRAG entities with
        entity_type="interaction_habit". The entity name follows the pattern
        "habit:{habit_type}:{target_tool}" for deterministic upsert.

        If the entity already exists, it will be updated (LightRAG upsert
        semantics: delete old + re-insert with new data).

        Args:
            habit_type: Habit type (e.g., "tool_dialect", "user_state").
            content: Habit content description.
            target_tool: The tool this habit relates to.
            confidence: Dict with success/fail counts, e.g.
                {"success_count": 3, "fail_count": 0, "last_used": "2026-04-24"}.
                Defaults to {"success_count": 0, "fail_count": 0}.
            source_id: Source document/chunk ID.

        Returns:
            Dict with status and details.
        """
        if confidence is None:
            confidence = {"success_count": 0, "fail_count": 0}

        entity_name = f"habit:{habit_type}:{target_tool}"
        description = f"{content} | confidence: {confidence}"

        chunks = [{
            "content": f"{content} target_tool={target_tool} confidence={confidence}",
            "source_id": source_id,
            "file_path": "interaction_habit",
        }]

        entities = [{
            "entity_name": entity_name,
            "entity_type": "interaction_habit",
            "description": description,
            "source_id": source_id,
            "file_path": "interaction_habit",
        }]

        return self.inject_custom_kg(
            entities=entities,
            relationships=[],
            chunks=chunks,
            source_id=source_id,
        )

    def update_habit_confidence(
        self,
        entity_name: str,
        result: str,
    ) -> Dict[str, Any]:
        """Update confidence for an interaction habit entity.

        Reads the current entity, updates success/fail counts, and re-injects
        it (LightRAG upsert). If fail_count >= 3, deletes the entity instead.

        This replaces vector_search.update_habit_confidence() which used
        direct SQLite operations on vectors.db.

        Args:
            entity_name: Entity name (e.g., "habit:tool_dialect:kg-server").
            result: "success" or "fail".

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            # Read current entity via graph traversal
            kg = call_async(rag.get_knowledge_graph(entity_name, max_depth=1, max_nodes=1))

            if kg is None or not kg.nodes:
                return {"status": "error", "message": f"Habit entity not found: {entity_name}"}

            # Find the matching node
            target_node = None
            for node in kg.nodes:
                if node.id == entity_name:
                    target_node = node
                    break

            if target_node is None:
                return {"status": "error", "message": f"Habit entity not found: {entity_name}"}

            # Parse current confidence from description
            description = target_node.properties.get("description", "")
            import json as _json
            import re as _re

            # Extract confidence dict from description (format: "... | confidence: {...}")
            confidence = {"success_count": 0, "fail_count": 0}
            conf_match = _re.search(r'confidence:\s*(\{[^}]+\})', description)
            if conf_match:
                try:
                    confidence = _json.loads(conf_match.group(1))
                except (_json.JSONDecodeError, ValueError):
                    pass

            # Extract the content part (before "| confidence:")
            content = _re.sub(r'\s*\|\s*confidence:\s*\{[^}]+\}\s*$', '', description).strip()

            # Extract entity_type and source info
            entity_type = target_node.properties.get("entity_type", "interaction_habit")

            # Update counts
            if result == "success":
                confidence["success_count"] = confidence.get("success_count", 0) + 1
            elif result == "fail":
                confidence["fail_count"] = confidence.get("fail_count", 0) + 1

            import time as _time
            confidence["last_used"] = _time.strftime("%Y-%m-%d")

            # Delete if too many failures
            if confidence.get("fail_count", 0) >= 3:
                try:
                    call_async(rag.adelete_by_entity(entity_name))
                    logger.info(f"[InteractionHabits] Deleted low-confidence habit: {entity_name}")
                except Exception as del_e:
                    logger.warning(f"[InteractionHabits] Failed to delete habit {entity_name}: {del_e}")
                return {"status": "ok", "action": "deleted", "entity_name": entity_name}

            # Re-inject with updated confidence (upsert)
            # Parse habit_type and target_tool from entity_name "habit:{type}:{tool}"
            parts = entity_name.split(":", 2)
            habit_type = parts[1] if len(parts) >= 2 else "unknown"
            target_tool = parts[2] if len(parts) >= 3 else "unknown"

            return self.upsert_interaction_habit(
                habit_type=habit_type,
                content=content,
                target_tool=target_tool,
                confidence=confidence,
            )

        except Exception as e:
            logger.error(f"LightRAG update_habit_confidence failed: {e}")
            return {"status": "error", "message": str(e)}

    def inject_entities_batch(
        self,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Batch inject multiple entities in a single ainsert_custom_kg() call.

        Each item dict should have keys matching inject_entity() params:
            name, entity_type, description, source_id, chunk_content, file_path

        This is dramatically faster than calling inject_entity() in a loop
        because each inject_entity() triggers a full LightRAG persist cycle
        (embedding + graph write + disk flush). Batch inject does one persist.

        Args:
            items: List of entity dicts.

        Returns:
            Dict with status, entities count, chunks count.
        """
        if not items:
            return {"status": "ok", "entities": 0, "chunks": 0}

        entities = []
        chunks = []

        for item in items:
            name = item.get("name")
            if not name:
                logger.warning(f"inject_entities_batch: skipping item missing 'name': {item}")
                continue
            entity_type = item.get("entity_type", "UNKNOWN")
            description = item.get("description", "")
            source_id = item.get("source_id", "custom_kg")
            file_path = item.get("file_path", "custom_kg")
            chunk_content = item.get("chunk_content")

            entities.append({
                "entity_name": name,
                "entity_type": entity_type,
                "description": description,
                "source_id": source_id,
                "file_path": file_path,
            })

            if chunk_content:
                chunks.append({
                    "content": chunk_content,
                    "source_id": source_id,
                    "file_path": file_path,
                })

        if not entities and not chunks:
            return {"status": "ok", "entities": 0, "chunks": 0}

        return self.inject_custom_kg(
            entities=entities,
            relationships=[],
            chunks=chunks,
            source_id="batch_inject",
        )

    def inject_relation(
        self,
        src_id: str,
        tgt_id: str,
        relation: str,
        description: str = "",
        source_id: str = "custom_kg",
        file_path: str = "custom_kg",
    ) -> Dict[str, Any]:
        """Inject a relation between two entities.

        Args:
            src_id: Source entity name/ID.
            tgt_id: Target entity name/ID.
            relation: Relation type (e.g., "has_framework").
            description: Relation description.
            source_id: Source document/chunk ID.
            file_path: File path for citation.

        Returns:
            Dict with status and details.
        """
        relationships = [{
            "src_id": src_id,
            "tgt_id": tgt_id,
            "keywords": relation,
            "description": description,
            "source_id": source_id,
            "file_path": file_path,
        }]

        return self.inject_custom_kg(
            entities=[],
            relationships=relationships,
            chunks=[],
            source_id=source_id,
        )

    def inject_custom_kg(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
        chunks: List[Dict[str, Any]],
        source_id: str = "custom_kg",
    ) -> Dict[str, Any]:
        """Inject structured knowledge via ainsert_custom_kg().

        This is the primary structured injection method. It builds the
        custom_kg dict that LightRAG expects and calls ainsert_custom_kg.

        Args:
            entities: List of entity dicts with keys:
                name, entity_type, description, source_id (optional), file_path (optional)
            relationships: List of relationship dicts with keys:
                src_id, tgt_id, relation, description (optional), source_id (optional), file_path (optional)
            chunks: List of chunk dicts with keys:
                content, source_id, file_path (optional)
            source_id: Default source ID for items without explicit source.

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        # Build custom_kg dict matching LightRAG's expected structure
        custom_kg: Dict[str, Any] = {
            "chunks": [],
            "entities": [],
            "relationships": [],
        }

        for chunk in chunks:
            custom_kg["chunks"].append({
                "content": chunk["content"],
                "source_id": chunk.get("source_id", source_id),
                "file_path": chunk.get("file_path", "custom_kg"),
                "chunk_order_index": chunk.get("chunk_order_index", 0),
            })

        for entity in entities:
            custom_kg["entities"].append({
                "entity_name": entity.get("entity_name") or entity.get("name", "UNKNOWN"),
                "entity_type": entity.get("entity_type", "UNKNOWN"),
                "description": entity.get("description", ""),
                "source_id": entity.get("source_id", source_id),
                "file_path": entity.get("file_path", "custom_kg"),
            })

        for rel in relationships:
            # LightRAG requires "keywords" (direct access, no .get() fallback)
            # and reads "description" (also direct access).
            # "weight" uses .get() with default 1.0.
            keywords = rel.get("keywords") or rel.get("relation", "")
            custom_kg["relationships"].append({
                "src_id": rel["src_id"],
                "tgt_id": rel["tgt_id"],
                "keywords": keywords,
                "description": rel.get("description", ""),
                "source_id": rel.get("source_id", source_id),
                "file_path": rel.get("file_path", "custom_kg"),
                "weight": rel.get("weight", 1.0),
            })

        try:
            call_async(rag.ainsert_custom_kg(custom_kg))
            return {
                "status": "ok",
                "entities": len(custom_kg["entities"]),
                "relationships": len(custom_kg["relationships"]),
                "chunks": len(custom_kg["chunks"]),
            }
        except Exception as e:
            logger.error(f"LightRAG custom_kg injection failed: {e}")
            return {"status": "error", "message": str(e)}

    # ============== Unstructured Path ==============

    def inject_document(
        self,
        content: str,
        doc_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inject an unstructured document for LLM-driven entity extraction.

        Args:
            content: Document text content.
            doc_id: Optional unique document ID for dedup.
            file_path: Optional file path for citation.

        Returns:
            Dict with status and track_id.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if doc_id is not None:
                kwargs["ids"] = doc_id
            if file_path is not None:
                kwargs["file_paths"] = file_path

            track_id = call_async(rag.ainsert(content, **kwargs))
            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            logger.error(f"LightRAG document injection failed: {e}")
            return {"status": "error", "message": str(e)}

    def inject_documents(
        self,
        documents: List[str],
        ids: Optional[List[str]] = None,
        file_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Batch inject multiple unstructured documents.

        Args:
            documents: List of document text strings.
            ids: Optional list of unique document IDs.
            file_paths: Optional list of file paths.

        Returns:
            Dict with status and track_id.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if ids is not None:
                kwargs["ids"] = ids
            if file_paths is not None:
                kwargs["file_paths"] = file_paths

            track_id = call_async(rag.ainsert(documents, **kwargs))
            return {"status": "ok", "track_id": track_id, "count": len(documents)}
        except Exception as e:
            logger.error(f"LightRAG batch document injection failed: {e}")
            return {"status": "error", "message": str(e)}
