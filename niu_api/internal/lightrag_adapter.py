"""
LightRAG Adapter & Ingester

Provides two interfaces to the LightRAG instance:
- LightRAGAdapter: query interface (replaces vector-store search + kg-server query)
- LightRAGIngester: injection via lightrag_insert (auto extraction),
                    inject_custom_kg (precise control), and inject_document (unstructured)

Both delegate to lightrag_manager for the LightRAG instance and use
call_async() for the async/sync bridge.
"""

import time

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

    # Supported entity types for filtered search (must match CUSTOM_ENTITY_TYPES in lightrag_manager.py)
    ENTITY_TYPES = {
        "Person", "Organization", "Technology", "Concept",
        "Location", "Event", "Document", "Photo", "Video",
        "Note", "Chat", "Skill", "Tool", "Knowledge",
        "InteractionHabit", "EpisodicEvent", "BrainRegion", "Other",
    }

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
        keywords: Optional[List[str]] = None,
        timeout: int = 120,
    ) -> Optional[str]:
        """Query the brain graph / knowledge base.

        Args:
            query: The search query string.
            mode: Retrieval mode (naive, local, global, hybrid, mix, bypass).
            only_need_context: If True, return context without LLM generation.
            top_k: Number of top items to retrieve.
            response_type: Response format (e.g., "Bullet Points").
            keywords: Pre-provided keywords to skip LLM extraction.
                When provided, both hl_keywords and ll_keywords are set,
                avoiding LLM calls entirely (near-instant return).
            timeout: Maximum seconds to wait for the LightRAG query result.
                Default is 120. Callers can set a shorter timeout (e.g. 2)
                to prevent deadlock when the LightRAG event loop is busy.

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
            if keywords is not None:
                param.hl_keywords = keywords
                param.ll_keywords = keywords
            if top_k is not None:
                param.top_k = top_k
            if response_type is not None:
                param.response_type = response_type

            result = call_async(rag.aquery(query, param=param), timeout=timeout)
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

            result = call_async(rag.aquery_data(query, param=param), timeout=120)
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
    # Keys use lowercase for case-insensitive matching (.lower() applied at lookup).
    _ENTITY_TYPE_TO_CATEGORY = {
        "skill": "skill",
        "tool": "knowledge",
        "knowledge": "knowledge",
        "concept": "knowledge",
        "interactionhabit": "knowledge",
        "person": "knowledge",
        "photo": "knowledge",
        "organization": "knowledge",
        "technology": "knowledge",
        "location": "knowledge",
        "event": "knowledge",
        "document": "knowledge",
        "video": "knowledge",
        "note": "knowledge",
        "chat": "knowledge",
        "episodicevent": "knowledge",
        "brainregion": "knowledge",
        "other": "other",
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
            Dict with category keys matching _ENTITY_TYPE_TO_CATEGORY values,
            each mapping to a list of entity dicts. Empty lists on error or no results.
        """
        # Initialize result dict with all category buckets from the mapping
        result: Dict[str, List[Dict[str, Any]]] = {
            cat: [] for cat in self._ENTITY_TYPE_TO_CATEGORY.values()
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
            category = self._ENTITY_TYPE_TO_CATEGORY.get(et, "knowledge")
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
        return self.filter_by_entity_type(result, "Skill")

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
        return self.filter_by_entity_type(result, "Tool")

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
        knowledge_entities = self.filter_by_entity_type(result, "Knowledge")
        concept_entities = self.filter_by_entity_type(result, "Concept")
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

        The entity name follows the pattern "{habit_type}__{target_tool}"
        (double underscore separator to avoid ambiguity when habit_type
        contains underscores, e.g. "tool_dialect__kg-server").
        and the description contains the habit content plus confidence data.

        Args:
            query: Search query string (typically tool args or context).
            top_k: Maximum number of results to retrieve.
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of interaction_habit entity dicts.
        """
        result = self.query_data(query, mode="local", top_k=top_k, keywords=keywords)
        return self.filter_by_entity_type(result, "InteractionHabit")

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
                rag.get_knowledge_graph(entity_name, max_depth=depth),
                timeout=120,
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
                    "type": first_node.properties.get("entity_type", "Other"),
                    "description": first_node.properties.get("description", ""),
                    "file_path": first_node.properties.get("file_path", ""),
                    "source_id": first_node.properties.get("source_id", ""),
                }

            # Convert KnowledgeGraphNode to frontend-compatible format
            nodes = []
            for node in kg.nodes:
                nodes.append({
                    "id": node.id,
                    "name": node.id,
                    "type": node.properties.get("entity_type", "Other"),
                    "description": node.properties.get("description", ""),
                    "file_path": node.properties.get("file_path", ""),
                    "source_id": node.properties.get("source_id", ""),
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
            except Exception as e:
                logger.debug(f"timeline_query: explore_node({entity_name}, depth=0) failed: {e}")

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
            except Exception as e:
                logger.debug(f"timeline_query: explore_node({entity_name}, depth={max_depth}) failed: {e}")
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
        """从描述中提取 created_at 时间戳。

        格式: created_at=2026-04-27T14:00:00|其他内容

        Args:
            description: Entity/edge description string.

        Returns:
            Extracted timestamp string, or empty string if not found.
        """
        if not description:
            return ""
        for part in description.split("|"):
            if part.startswith("created_at="):
                return part[len("created_at="):]
        return ""

    def has_entity(self, entity_name: str) -> bool:
        """精确查询实体是否已存在（LightRAG内部lowercase存储，此处直接lowercase匹配）。

        Returns False when LightRAG is not initialized (lenient — allows insert
        to proceed, and ainsert_custom_kg will use <SEP> append, not overwrite).
        """
        rag = self._get_rag()
        if rag is None:
            return False
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return False
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        from niu_api.internal.lightrag_manager import graph_read_lock
        with graph_read_lock():
            return nx_graph.has_node(entity_name.lower())

    def has_edge(self, src_id: str, tgt_id: str, keywords: str = None) -> bool:
        """精确查询关系是否已存在。

        If keywords is provided, also checks that the edge's keywords matches.
        Without keywords, returns True if any edge exists between src and tgt.

        Returns False when LightRAG is not initialized (lenient — allows insert
        to proceed; ainsert_custom_kg will use <SEP> append for existing edges).
        """
        rag = self._get_rag()
        if rag is None:
            return False
        graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
        if graph_obj is None:
            return False
        nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
        from niu_api.internal.lightrag_manager import graph_read_lock
        with graph_read_lock():
            src = src_id.lower()
            tgt = tgt_id.lower()
            if not nx_graph.has_edge(src, tgt):
                return False
            if keywords is None:
                return True
            edge_data = nx_graph.get_edge_data(src, tgt)
            return edge_data.get("keywords") == keywords

    def get_graph_snapshot(self, limit: int = 200) -> Dict[str, Any]:
        """Return all nodes and edges from LightRAG knowledge graph.

        Reads the NetworkX graph directly (same pattern as hub_entities,
        list_entities, etc.) instead of going through call_async. This avoids
        blocking on the LightRAG event loop when background sync operations
        (lightrag_sync, region_sync) are writing via inject_custom_kg.

        Args:
            limit: Maximum number of nodes to return (default 200).
                Nodes are sorted by degree (most-connected first).

        Returns:
            Dict with nodes and edges lists for frontend visualization.
            Returns empty result on error.
        """
        rag = self._get_rag()
        if rag is None:
            logger.warning("LightRAG not available, get_graph_snapshot failed")
            return {"nodes": [], "edges": []}

        try:
            from niu_api.internal.lightrag_manager import graph_read_lock

            graph_obj = getattr(rag, "chunk_entity_relation_graph", None)
            if graph_obj is None:
                return {"nodes": [], "edges": []}
            nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj

            # Acquire read lock to prevent concurrent graph mutation during
            # traversal. Without this, background sync (lightrag_sync, region_sync)
            # can modify the graph via inject_custom_kg while we iterate, causing
            # RuntimeError or silent data corruption.
            with graph_read_lock():
                # Shallow-copy the graph under the lock to get a consistent snapshot.
                # The copy itself is fast (<1ms for ~200 nodes). After copying,
                # we can release the lock and iterate the snapshot safely.
                try:
                    snapshot = nx_graph.copy()
                except Exception as e:
                    logger.warning(f"[KG] graph copy failed: {e}, returning empty snapshot")
                    return {"nodes": [], "edges": []}

            # Now iterate the snapshot without holding the lock (safe because
            # it's our local copy — no other thread can modify it)
            # Sort nodes by degree (most-connected first), then take top limit
            try:
                node_degrees = {n: snapshot.degree(n) for n in snapshot.nodes()}
                sorted_nodes = sorted(
                    node_degrees.keys(), key=lambda n: node_degrees[n], reverse=True
                )
                top_nodes = sorted_nodes[:limit]
                top_set = set(top_nodes)
            except RuntimeError:
                logger.warning("Graph modified during snapshot read, returning partial data")
                top_nodes = list(snapshot.nodes())[:limit]
                top_set = set(top_nodes)

            nodes = []
            for node_name in top_nodes:
                try:
                    attrs = snapshot.nodes[node_name] if snapshot.has_node(node_name) else {}
                except (RuntimeError, KeyError):
                    attrs = {}
                nodes.append({
                    "id": node_name,
                    "name": node_name,
                    "type": attrs.get("entity_type", "Other"),
                    "description": attrs.get("description", ""),
                    "file_path": attrs.get("file_path", ""),
                    "source_id": attrs.get("source_id", ""),
                })

            edges = []
            try:
                for u, v, data in snapshot.edges(data=True):
                    if u in top_set and v in top_set:
                        edges.append({
                            "source": u,
                            "target": v,
                            "relation": data.get("keywords", ""),
                            "description": data.get("description", ""),
                            "weight": data.get("weight", 1.0),
                        })
            except RuntimeError:
                logger.warning("Graph modified during snapshot edge read, returning partial edges")

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
            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            result = call_async(rag.adelete_by_entity(entity_name), timeout=300)

            # Record change for frontend changelog polling (best-effort)
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("entity_deleted", {"id": f"entity:{entity_name}"})
            except Exception as e:
                logger.debug(f"changelog record_change failed: {e}")

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
            return call_async(rag.get_processing_status(), timeout=30)
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
                data = call_async(rag.get_graph_labels(), timeout=30)
                return {"status": "ok", "data": data}
            elif list_type == "documents":
                data = call_async(rag.get_docs_by_status("processed"), timeout=30)
                return {"status": "ok", "data": data}
            elif list_type == "entities":
                if entity_type:
                    # 按 entity_type 过滤：直接遍历 NetworkX 图节点
                    # 不能用 get_knowledge_graph(entity_type)，因为那是按节点名搜索
                    from niu_api.internal.lightrag_manager import graph_read_lock

                    graph_obj = rag.chunk_entity_relation_graph
                    nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                    if nx_graph is None:
                        return {"status": "ok", "data": []}
                    nodes = []
                    with graph_read_lock():
                        snapshot = nx_graph.copy()
                    for node_id, node_data in snapshot.nodes(data=True):
                        nt = node_data.get("entity_type", "Other")
                        if nt.lower() == entity_type.lower():
                            nodes.append({
                                "id": node_id,
                                "entity_type": nt,
                                "description": node_data.get("description", ""),
                            })
                            if len(nodes) >= limit:
                                break
                    return {"status": "ok", "data": nodes}
                else:
                    # 无过滤：用 get_knowledge_graph 全图搜索
                    kg = call_async(
                        rag.get_knowledge_graph(
                            "*",
                            max_depth=1,
                            max_nodes=limit,
                        ),
                        timeout=120,
                    )
                    if kg is None:
                        return {"status": "ok", "data": []}
                    nodes = []
                    for node in kg.nodes:
                        nodes.append({
                            "id": node.id,
                            "entity_type": node.properties.get("entity_type", "Other"),
                            "description": node.properties.get("description", ""),
                        })
                    return {"status": "ok", "data": nodes}
            else:
                return {"status": "error", "message": f"Unknown list_type: {list_type}"}
        except Exception as e:
            logger.error(f"LightRAG list_entities failed: {e}")
            return {"status": "error", "message": str(e)}

    def _resolve_entity_name_case_insensitive(
        self, entity_name: str, nx_graph
    ) -> Optional[str]:
        """Resolve entity name with case-insensitive fallback.

        Tries exact match first. If that fails, searches all graph nodes
        for a case-insensitive match. Returns the canonical name from the
        graph, or None if not found at all.

        Args:
            entity_name: The entity name to resolve.
            nx_graph: NetworkX graph instance (snapshot, not live graph).

        Returns:
            Canonical entity name from graph, or None if not found.
        """
        if nx_graph.has_node(entity_name):
            return entity_name

        logger.warning(
            "Entity '%s' not found in graph, trying case-insensitive match",
            entity_name,
        )
        entity_name_lower = entity_name.lower()
        for node_name in nx_graph.nodes():
            if node_name.lower() == entity_name_lower:
                logger.info(
                    "Case-insensitive match: '%s' resolved to '%s'",
                    entity_name,
                    node_name,
                )
                return node_name

        logger.error(
            "Entity '%s' not found in graph (case-insensitive also failed)",
            entity_name,
        )
        return None

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

        # Resolve entity names with case-insensitive fallback.
        # Take a snapshot under read lock to prevent RuntimeError from
        # concurrent graph modification by background sync (lightrag_sync, region_sync).
        from niu_api.internal.lightrag_manager import graph_read_lock

        graph_obj = rag.chunk_entity_relation_graph
        if graph_obj is None:
            return {"status": "error", "message": "Knowledge graph not available"}
        with graph_read_lock():
            nx_graph = (graph_obj._graph.copy() if hasattr(graph_obj, "_graph") else graph_obj.copy())

        resolved_sources: List[str] = []
        unresolved_sources: List[str] = []
        for src in source_entities:
            resolved = self._resolve_entity_name_case_insensitive(src, nx_graph)
            if resolved is None:
                unresolved_sources.append(src)
            elif resolved != src:
                logger.info(
                    "Source entity '%s' resolved to '%s' (case-insensitive)",
                    src, resolved,
                )
                resolved_sources.append(resolved)
            else:
                resolved_sources.append(src)

        if unresolved_sources:
            return {
                "status": "error",
                "message": (
                    f"Source entities not found in graph "
                    f"(case-insensitive also failed): {unresolved_sources}"
                ),
            }

        resolved_target = self._resolve_entity_name_case_insensitive(
            target_entity, nx_graph
        )
        if resolved_target is None:
            return {
                "status": "error",
                "message": (
                    f"Target entity '{target_entity}' not found in graph "
                    f"(case-insensitive also failed)"
                ),
            }
        if resolved_target != target_entity:
            logger.info(
                "Target entity '%s' resolved to '%s' (case-insensitive)",
                target_entity, resolved_target,
            )

        try:
            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            result = call_async(
                rag.amerge_entities(resolved_sources, resolved_target),
                timeout=300,
            )

            # Record change for frontend changelog polling (best-effort)
            try:
                from niu_api.internal.lightrag_manager import get_change_log, graph_read_lock

                # Read target entity's actual attributes from the graph
                # Use resolved_target (canonical graph name) instead of the
                # raw target_entity which may differ by case.
                graph_obj = rag.chunk_entity_relation_graph
                nx_graph = graph_obj._graph if hasattr(graph_obj, "_graph") else graph_obj
                target_type = "Other"
                target_desc = ""
                if nx_graph and nx_graph.has_node(resolved_target):
                    with graph_read_lock():
                        if nx_graph.has_node(resolved_target):
                            attrs = nx_graph.nodes[resolved_target]
                            target_type = attrs.get("entity_type", "Other")
                            target_desc = attrs.get("description", "")

                get_change_log().record_change("entity_merged", {
                    "source_ids": [f"entity:{s}" for s in resolved_sources],
                    "target_id": f"entity:{resolved_target}",
                    "name": resolved_target,
                    "type": target_type,
                    "description": target_desc,
                })
            except Exception as e:
                logger.debug(f"changelog record_change failed: {e}")

            return {"status": "ok", "target_entity": resolved_target, "result": str(result)}
        except AttributeError:
            return {"status": "error", "message": "Entity merge not supported by this LightRAG version"}
        except Exception as e:
            logger.error(f"LightRAG merge_entities failed: {e}")
            return {"status": "error", "message": str(e)}


class LightRAGIngester:
    """Data injection for LightRAG.

    Recommended: lightrag_insert → calls ainsert() for automatic entity/relationship extraction and merging.

    Structured: inject_custom_kg → calls ainsert_custom_kg() for precise entity/relationship injection (no LLM extraction).
    Complementary: inject_custom_kg (precise control) and lightrag_insert (auto extraction) serve different purposes.
    Unstructured: inject_document, inject_documents → calls ainsert() for raw document ingestion.
    """

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    # ============== Structured Path ==============

    def upsert_interaction_habit(
        self,
        habit_type: str,
        content: str,
        target_tool: str,
        confidence: Optional[Dict[str, Any]] = None,
        source_id: str = "custom_kg",
    ) -> Dict[str, Any]:
        """Upsert an interaction habit entity into the knowledge graph.

        Uses lightrag_insert (ainsert) for automatic entity extraction,
        relationship discovery, and same-name entity merging.

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

        # Entity name uses double-underscore separator to avoid ambiguity
        # when habit_type itself contains underscores (e.g. "tool_dialect").
        # Format: {habit_type}__{target_tool}
        # Example: "tool_dialect__kg-server" (not "tool_dialect_kg-server")
        entity_name = f"{habit_type}__{target_tool}"
        description = f"{content}<SEP>confidence: {confidence}"

        text = f"交互习惯: {entity_name}（类型: InteractionHabit），{description}。Niu uses {entity_name}。"
        return self.lightrag_insert(content=text, file_paths=source_id if source_id != "custom_kg" else None)

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
            entity_name: Entity name (e.g., "tool_dialect_kg-server").
            result: "success" or "fail".

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            # Read current entity via graph traversal
            kg = call_async(rag.get_knowledge_graph(entity_name, max_depth=1, max_nodes=1), timeout=120)

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
            content = _re.sub(r'(?:<SEP>|\s\|\s)confidence:\s*\{[^}]+\}\s*$', '', description).strip()

            # Extract entity_type (read for potential future use, currently unused)
            entity_type = target_node.properties.get("entity_type", "InteractionHabit")  # noqa: F841

            # Update counts
            if result == "success":
                confidence["success_count"] = confidence.get("success_count", 0) + 1
            elif result == "fail":
                confidence["fail_count"] = confidence.get("fail_count", 0) + 1

            confidence["last_used"] = time.strftime("%Y-%m-%d")

            # Delete if too many failures
            if confidence.get("fail_count", 0) >= 3:
                try:
                    call_async(rag.adelete_by_entity(entity_name), timeout=300)
                    logger.info(f"[InteractionHabits] Deleted low-confidence habit: {entity_name}")
                except Exception as del_e:
                    logger.warning(f"[InteractionHabits] Failed to delete habit {entity_name}: {del_e}")
                return {"status": "ok", "action": "deleted", "entity_name": entity_name}

            # Re-inject with updated confidence (upsert)
            # Parse habit_type and target_tool from entity_name
            # Support three formats:
            #   New:     "{type}__{tool}" — double underscore (e.g. "tool_dialect__kg-server")
            #   Legacy1: "habit:{type}:{tool}" — colon-prefix (e.g. "habit:tool_dialect:kg-server")
            #   Legacy2: "{type}_{tool}" — single underscore (e.g. "tool_dialect_kg-server")
            #            (ambiguous when type contains _, kept for backward compat only)
            if entity_name.startswith("habit:"):
                # Old colon-prefix format: "habit:{type}:{tool}"
                parts = entity_name.split(":", 2)
                habit_type = parts[1] if len(parts) >= 2 else "unknown"
                target_tool = parts[2] if len(parts) >= 3 else "unknown"
            elif "__" in entity_name:
                # New double-underscore format: "{type}__{tool}"
                parts = entity_name.split("__", 1)
                habit_type = parts[0] if len(parts) >= 1 else "unknown"
                target_tool = parts[1] if len(parts) >= 2 else "unknown"
            else:
                # Legacy single-underscore format (ambiguous, best-effort)
                parts = entity_name.split("_", 1)
                habit_type = parts[0] if len(parts) >= 1 else "unknown"
                target_tool = parts[1] if len(parts) >= 2 else "unknown"

            return self.upsert_interaction_habit(
                habit_type=habit_type,
                content=content,
                target_tool=target_tool,
                confidence=confidence,
            )

        except Exception as e:
            logger.error(f"LightRAG update_habit_confidence failed: {e}")
            return {"status": "error", "message": str(e)}

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

        Complementary to lightrag_insert: use inject_custom_kg when you need
        precise control over entity names, relationship types, and graph structure
        (e.g., brain regions, photo metadata, person nodes). Use lightrag_insert
        for natural language content where LLM auto-extraction and entity merging
        are desired.

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

        try:
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

            # Auto-generate virtual chunks for source_ids not covered by existing
            # chunks. LightRAG's ainsert_custom_kg uses chunk_to_source_map to
            # translate source_id to chunk physical IDs. Without a mapping, all
            # source_ids become "UNKNOWN". Virtual chunks also improve vector recall.
            covered_source_ids: set[str] = {
                c.get("source_id", source_id) for c in custom_kg["chunks"]
            }
            for entity in entities:
                entity_source_id = entity.get("source_id", source_id)
                # Use unique source_id per entity to avoid chunk_to_source_map
                # overwriting (all entities sharing "brain" would map to the
                # same last chunk_id otherwise)
                unique_source_id = f"{entity_source_id}_{entity.get('entity_name') or entity.get('name', 'Other')}"
                if unique_source_id not in covered_source_ids:
                    entity_name = entity.get("entity_name") or entity.get("name", "Other")
                    entity_desc = entity.get("description", "")
                    custom_kg["chunks"].append({
                        "content": f"{entity_name}: {entity_desc}" if entity_desc else entity_name,
                        "source_id": unique_source_id,
                        "file_path": entity.get("file_path", "custom_kg"),
                        "chunk_order_index": 0,
                    })
                    covered_source_ids.add(unique_source_id)
                # Update entity's source_id to match the unique chunk source_id
                entity["source_id"] = unique_source_id
            for rel in relationships:
                rel_source_id = rel.get("source_id", source_id)
                if rel_source_id not in covered_source_ids:
                    src = rel.get('src_id', 'unknown')
                    tgt = rel.get('tgt_id', 'unknown')
                    keywords = rel.get('keywords', '') or rel.get('relation', '')
                    content = f"{src}->{tgt}: {keywords}" if keywords else f"{src} -> {tgt}"
                    custom_kg["chunks"].append({
                        "content": content,
                        "source_id": rel_source_id,
                        "file_path": rel.get("file_path", "custom_kg"),
                        "chunk_order_index": 0,
                    })
                    covered_source_ids.add(rel_source_id)

            for entity in entities:
                custom_kg["entities"].append({
                    "entity_name": entity.get("entity_name") or entity.get("name", "Other"),
                    "entity_type": entity.get("entity_type", "Other"),
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

            # call_async runs in LightRAG's asyncio loop (serialized there),
            # so concurrent writes are impossible. No write lock needed —
            # readers use graph_read_lock + copy() snapshot to avoid
            # RuntimeError("Graph changed during iteration").
            call_async(rag.ainsert_custom_kg(custom_kg), timeout=600)

            # Record changes for frontend changelog polling
            # (best-effort: never let changelog errors affect the write result)
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                change_log = get_change_log()
                for entity in custom_kg["entities"]:
                    change_log.record_change("entity_created", {
                        "id": f"entity:{entity['entity_name']}",
                        "name": entity["entity_name"],
                        "type": entity.get("entity_type", "Other"),
                        "description": entity.get("description", ""),
                        "file_path": entity.get("file_path", ""),
                        "source_id": entity.get("source_id", ""),
                    })
                for rel in custom_kg["relationships"]:
                    change_log.record_change("edge_created", {
                        "source": f"entity:{rel['src_id']}",
                        "target": f"entity:{rel['tgt_id']}",
                        "relation": rel.get("keywords", ""),
                        "confidence": rel.get("weight", 1.0),
                    })
            except Exception as e:
                logger.debug(f"changelog record_change failed: {e}")

            return {
                "status": "ok",
                "entities": len(custom_kg["entities"]),
                "relationships": len(custom_kg["relationships"]),
                "chunks": len(custom_kg["chunks"]),
            }
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG custom_kg injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

    def lightrag_insert(self, content: str, file_paths: Optional[str] = None) -> Dict[str, Any]:
        """通过 ainsert 入库结构化文本（LightRAG 自动提取实体/关系）。

        与 inject_custom_kg 互补：lightrag_insert 适用于自然语言内容，LightRAG
        会自动提取实体和关系、合并同名实体、建立实体之间的边。inject_custom_kg
        适用于需要精确控制实体名称、关系类型和图结构的场景（如脑区、照片元数据、人物节点）。

        Args:
            content: 结构化文本，格式化好的照片/人物/记忆描述
            file_paths: 可选的文件路径关联

        Returns:
            Dict[str, Any]: 成功时 {"status": "ok", "track_id": str}，失败时 {"status": "error", "message": str}
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}

        try:
            kwargs = {}
            if file_paths:
                kwargs["file_paths"] = file_paths
            track_id = call_async(rag.ainsert(content, **kwargs), timeout=600)

            # Record change for frontend changelog polling (best-effort)
            # LLM-extracted entities are unknown at this point,
            # frontend should re-fetch full snapshot after receiving this event.
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": track_id or "",
                    "uri": file_paths or "",
                    "title": file_paths or "lightrag_insert",
                    "source": "lightrag_insert",
                })
            except Exception as e:
                logger.debug(f"lightrag_insert changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG lightrag_insert failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

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

            track_id = call_async(rag.ainsert(content, **kwargs), timeout=600)

            # Record document insertion for frontend changelog
            # (LLM-extracted entities are unknown, so frontend should
            # re-fetch full snapshot after receiving this event)
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": doc_id or "",
                    "uri": file_path or "",
                    "title": file_path or doc_id or "",
                    "source": "inject_document",
                })
            except Exception as e:
                logger.debug(f"inject_document changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG document injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}

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

            track_id = call_async(rag.ainsert(documents, **kwargs), timeout=600)

            # Record document insertion for frontend changelog
            try:
                from niu_api.internal.lightrag_manager import get_change_log

                get_change_log().record_change("document_created", {
                    "id": ",".join(ids) if ids else "",
                    "uri": ",".join(file_paths) if file_paths else "",
                    "title": ",".join(file_paths) if file_paths else ",".join(ids) if ids else "",
                    "source": "inject_documents",
                    "count": len(documents),
                })
            except Exception as e:
                logger.debug(f"inject_documents changelog record failed: {e}")

            return {"status": "ok", "track_id": track_id, "count": len(documents)}
        except Exception as e:
            err_msg = str(e) or f"{type(e).__name__}: (no message)"
            logger.error(f"LightRAG batch document injection failed: {err_msg}", exc_info=True)
            return {"status": "error", "message": err_msg}
