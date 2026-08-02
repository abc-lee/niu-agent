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
from typing import Any

from loguru import logger

from niu_api.internal.lightrag_manager import call_async, get_lightrag

# Valid query modes for LightRAG
VALID_MODES = {"naive", "local", "global", "hybrid", "mix", "bypass"}

# 图查询结果最大字符数（参考 Claude Code Grep 工具上限）
# explore_node / get_graph_snapshot 在工具层截断，避免 depth=3 limit=100 返回
# 50 万字符触发单消息超限。与 disk 保底截断（30K）形成双层防护。
LIGHTRAG_GRAPH_MAX_CHARS = 20000

# LightRAG's fail_response constant substring markers.
# Used to detect the canned error text that LightRAG returns when queries
# produce no results.  These are not LLM-generated responses — they are
# hard-coded fallback strings that must never leak into system prompts.
_LIGHTRAG_ERROR_MARKERS = ("not able to provide", "[no-context]")


def _filter_result_fields(result: dict, fields: list) -> dict:
    """对 query_data 返回结果做字段裁剪，只保留指定字段。

    Args:
        result: query_data 返回的完整结果 dict
        fields: 要保留的字段名列表。None 或空列表表示不过滤。

    Returns:
        裁剪后的结果 dict（原地修改 result 中的 data 部分）
    """
    if not fields:
        return result
    field_set = set(fields)
    data = result.get("data", {})
    if not isinstance(data, dict):
        return result  # Unexpected structure, skip filtering
    # 裁剪 entities
    if "entities" in data:
        data["entities"] = [
            {k: v for k, v in ent.items() if k in field_set}
            for ent in data["entities"]
        ]
    # 裁剪 relationships
    if "relationships" in data:
        data["relationships"] = [
            {k: v for k, v in rel.items() if k in field_set}
            for rel in data["relationships"]
        ]
    # 裁剪 chunks
    if "chunks" in data:
        data["chunks"] = [
            {k: v for k, v in ch.items() if k in field_set}
            for ch in data["chunks"]
        ]
    return result


def _clean_sep(desc: str | None) -> str:
    """Clean LightRAG <SEP> separator from entity/edge descriptions.

    LightRAG merges multi-source descriptions using <SEP> as separator.
    This replaces <SEP> with a space for clean display in API responses
    and MCP tool results returned to the LLM.

    Args:
        desc: Raw description string that may contain <SEP>.

    Returns:
        Description with all <SEP> replaced by spaces. None → empty string.
    """
    if not desc:
        return ""
    return desc.replace("<SEP>", " ")


def _clean_description(desc: str | None, entity_type: str | None = None) -> str:
    """Clean description for output, with brainregion-aware formatting.

    For brainregion entities, the raw description contains brain_meta_*
    metadata separated by <SEP>. This function parses and formats the
    human-readable summary (same logic as kg_api._format_description),
    so both API and MCP consumers receive clean descriptions.

    For all other entity types (and edges), <SEP> is replaced with spaces.

    Args:
        desc: Raw description string that may contain <SEP>.
        entity_type: Entity type string. If "brainregion", applies
            brain_meta parsing before returning.

    Returns:
        Cleaned description string. None → empty string.
    """
    if not desc:
        return ""
    if entity_type and entity_type.lower() == "brainregion" and "<SEP>" in desc:
        from niu_api.internal.region_manager import _format_summary_for_display, _parse_description
        parsed = _parse_description(desc)
        return _format_summary_for_display(parsed)
    return desc.replace("<SEP>", " ")


def _clean_sep_in_query_result(result):
    """Clean <SEP> from description fields in query_data results.

    LightRAG's aquery_data() returns structured results with entities,
    relationships, and chunks — each potentially containing description
    fields with <SEP> separators from multi-source merging.
    """
    if not isinstance(result, dict):
        return result
    data = result.get("data", result)
    if isinstance(data, dict):
        for key in ("entities", "relationships"):
            items = data.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and "description" in item:
                        item["description"] = _clean_sep(item.get("description", ""))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "description" in item:
                item["description"] = _clean_sep(item.get("description", ""))
    return result


class LightRAGAdapter:
    """Query interface for LightRAG.

    Replaces vector-store search and kg-server query with unified
    LightRAG retrieval supporting multiple modes.
    """

# Supported entity types for filtered search (must match custom_entity_types in lightrag_manager.py)
    ENTITY_TYPES = {
        "Person", "Organization", "Technology", "Concept",
        "Location", "Event", "Document", "Photo", "Video",
        "Note", "Chat", "Skill", "Tool", "Knowledge",
        "interactionhabit", "EpisodicEvent", "brainregion", "other",
    }

    @staticmethod
    def _is_no_result(result: dict[str, Any] | None) -> bool:
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
    def _is_error_text(text: str | None) -> bool:
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
        top_k: int | None = None,
        response_type: str | None = None,
        keywords: list[str] | None = None,
        timeout: int = 120,
    ) -> str | None:
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
            # Clean <SEP> from context string (entity descriptions embedded by LightRAG)
            result = result.replace("<SEP>", " ")
            return result

        except Exception as e:
            logger.error(f"LightRAG query failed: {e}")
            return None

    def query_data(
        self,
        query: str,
        mode: str = "local",
        top_k: int | None = None,
        keywords: list[str] | None = None,
        filter_lambda=None,
        fields: list[str] | None = None,
    ) -> dict[str, Any] | None:
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
            filter_lambda: Optional filter function for LightRAG query.
            fields: Optional list of field names to include in the output.
                When provided, only these fields are kept in each entity/relationship/chunk.
                Common choices: ["entity_name", "entity_type"] for name-only lists.
                None (default) returns all fields (no filtering).

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
            if filter_lambda is not None:
                param.filter_lambda = filter_lambda

            result = call_async(rag.aquery_data(query, param=param), timeout=120)
            if fields:
                result = _filter_result_fields(result, fields)
            result = _clean_sep_in_query_result(result)
            return result

        except Exception as e:
            logger.error(f"LightRAG query_data failed: {e}")
            return None

    def filter_by_entity_type(
        self,
        query_result: dict[str, Any] | None,
        entity_type: str,
    ) -> list[dict[str, Any]]:
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
        "interactionhabit": "interactionhabit",
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

    def _categorize_results(self, result: dict) -> dict[str, list[dict]]:
        """Categorize query_data results into skill/knowledge/other buckets by entity_type.

        Includes same fallback logic as search_multi_lightrag for data extraction.
        """
        buckets: dict[str, list[dict]] = {cat: [] for cat in set(self._ENTITY_TYPE_TO_CATEGORY.values())}

        if not result:
            return buckets

        data = result.get("data", {})
        if not data:
            data = result
        entities = data.get("entities", [])
        if not entities:
            return buckets

        for entity in entities:
            entity_type = entity.get("entity_type", "other").lower()
            category = self._ENTITY_TYPE_TO_CATEGORY.get(entity_type, "knowledge")
            buckets[category].append(entity)
        return buckets

    def search_multi_lightrag(
        self,
        query: str,
        mode: str = "local",
        top_k: int = 20,
        keywords: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
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
        result: dict[str, list[dict[str, Any]]] = {
            cat: [] for cat in self._ENTITY_TYPE_TO_CATEGORY.values()
        }

        query_result = self.query_data(
            query, mode=mode, top_k=top_k, keywords=keywords,
        )
        if self._is_no_result(query_result):
            logger.debug("LightRAG search_multi_lightrag: query_data returned no results")
            return result

        return self._categorize_results(query_result)

    def search_within_region(
        self,
        query: str,
        region_member_names: set[str] | list[str],
        mode: str = "local",
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> dict[str, list[dict]]:
        """Search entities within specified brain region members only.

        Uses filter_lambda to restrict vector search to the given member entity names.
        This enables region-scoped semantic search (e.g., searching only within
        activated brain regions).

        Args:
            query: Search query text
            region_member_names: Set/list of entity names to restrict search to
            mode: LightRAG search mode (default: "local")
            top_k: Number of results to return
            keywords: Optional keywords to skip LLM extraction

        Returns:
            Dict with "skill", "knowledge" and "other" lists, same format as search_multi_lightrag
        """
        if not region_member_names:
            return {"skill": [], "knowledge": [], "other": []}

        member_set = {name.lower() for name in region_member_names}
        def filter_fn(data):
            return (data.get("entity_name") or "").lower() in member_set

        result = self.query_data(
            query, mode=mode, top_k=top_k, keywords=keywords,
            filter_lambda=filter_fn,
        )
        if not result:
            return {"skill": [], "knowledge": [], "other": []}

        return self._categorize_results(result)

    # ============== Semantic Search Methods ==============

    def search_skills(
        self,
        query: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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

    def search_by_file_path(
        self,
        query: str,
        file_path_contains: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities with pre-filter on file_path via filter_lambda.

        Unlike search_skills (which post-filters), this method uses
        filter_lambda to filter at the vector search stage — achieving
        true 'filter-then-top-k' semantics.

        Used for skill retrieval where file_path contains 'skill_sync'
        (SkillSync-injected skills), ensuring skills are not drowned out
        by knowledge entities in global top-k.

        Args:
            query: Search query string.
            file_path_contains: Substring to match in entity's file_path field.
            top_k: Number of top results to retrieve (after filtering).
            keywords: Pre-provided keywords to skip LLM extraction.

        Returns:
            List of entity dicts matching the file_path filter.
        """
        def filter_fn(data: dict) -> bool:
            fp = data.get("file_path", "")
            return bool(fp) and file_path_contains in fp

        result = self.query_data(
            query, mode="local", top_k=top_k,
            keywords=keywords, filter_lambda=filter_fn,
        )
        if not result:
            return []

        data = result.get("data", {})
        if not data:
            data = result
        return data.get("entities", [])

    def search_tools(
        self,
        query: str,
        top_k: int = 10,
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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
        keywords: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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

    # DEPRECATED: 截断已移至 agent_loop 统一关口，本函数保留供参考但无调用方
    def _truncate_graph_result(self, result: dict[str, Any], tool_name: str = "lightrag_get_graph") -> dict[str, Any]:
        """图查询结果保底截断到 LIGHTRAG_GRAPH_MAX_CHARS。

        explore_node 和 get_graph_snapshot 共用。用 while 循环逐步缩减 nodes
        直到序列化 <= 上限，避免按比例截断后仍超限。

        截断策略：
        - 清空 edges（占字符最多且可重新查询）
        - 保留 center + 部分 nodes 让 LLM 知道查询方向
        - stats 保留原始计数 + kept_nodes 让 LLM 知道截断比例
        """
        import json
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) <= LIGHTRAG_GRAPH_MAX_CHARS:
            return result

        logger.warning(f"{tool_name} result {len(serialized)} chars > {LIGHTRAG_GRAPH_MAX_CHARS}, truncating")
        center = result.get("center")
        nodes = list(result.get("nodes", []))
        edges = list(result.get("edges", []))
        original_nodes = len(nodes)
        original_edges = len(edges)
        stats_extra = result.get("stats", {})

        # 先处理 center 超大边界：若 center 自身 description 过大（如 nodes=[] 时
        # while 循环 keep_count=0 立即退出但仍返回 60K），提前截断 description
        if center is not None and isinstance(center, dict):
            try:
                center_serialized = json.dumps(center, ensure_ascii=False)
            except Exception:
                center_serialized = ""
            if len(center_serialized) > LIGHTRAG_GRAPH_MAX_CHARS // 2:
                # center 占了一半以上预算，截断 description 到 1/4 预算
                desc = center.get("description", "")
                if isinstance(desc, str) and len(desc) > 0:
                    desc_budget = LIGHTRAG_GRAPH_MAX_CHARS // 4
                    center = {
                        **center,
                        "description": desc[:desc_budget] + "...[center 截断]",
                    }

        # 先清空 edges（占字符最多且可重新查询），逐步缩减 nodes
        truncated_nodes = list(nodes)
        while True:
            candidate = {
                "status": "truncated",
                "message": f"[截断] {tool_name} 原始输出 {len(serialized)} 字符，已截断至 {LIGHTRAG_GRAPH_MAX_CHARS} 字符。请缩小 depth/limit 参数后重新查询。",
                "center": center,
                "nodes": truncated_nodes,
                "edges": [],
                "stats": {
                    **stats_extra,
                    "nodes": original_nodes,
                    "edges": original_edges,
                    "truncated": True,
                    "original_chars": len(serialized),
                    "kept_nodes": len(truncated_nodes),
                },
            }
            candidate_serialized = json.dumps(candidate, ensure_ascii=False)
            if len(candidate_serialized) <= LIGHTRAG_GRAPH_MAX_CHARS or len(truncated_nodes) == 0:
                return candidate
            keep_count = max(0, int(len(truncated_nodes) * 0.7))
            truncated_nodes = truncated_nodes[:keep_count]

    def explore_node(self, entity_name: str, depth: int = 2, edge_types: list[str] | None = None) -> dict[str, Any]:
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
                    "type": first_node.properties.get("entity_type", "other"),
                    "description": _clean_description(first_node.properties.get("description", ""), first_node.properties.get("entity_type", "other")),
                    "file_path": first_node.properties.get("file_path", ""),
                    "source_id": first_node.properties.get("source_id", ""),
                }

            # Convert KnowledgeGraphNode to frontend-compatible format
            nodes = []
            for node in kg.nodes:
                nodes.append({
                    "id": node.id,
                    "name": node.id,
                    "type": node.properties.get("entity_type", "other"),
                    "description": _clean_description(node.properties.get("description", ""), node.properties.get("entity_type", "other")),
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
                    "description": _clean_sep(edge.properties.get("description", "")),
                    "weight": edge.properties.get("weight", 1.0),
                })

            # Filter edges by edge_types if specified
            if edge_types is not None:
                edge_type_set = set(edge_types)
                edges = [e for e in edges if e.get("relation") in edge_type_set]

            result = {
                "center": center,
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "max_depth": depth,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API 和内部业务（region_detector/region_manager）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG explore_node failed: {e}")
            return {"center": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "max_depth": depth}}

    def timeline_query(
        self,
        query: str,
        start_entities: list[str] | None = None,
        direction: str = "backward",
        max_depth: int = 2,
        top_k: int = 5,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
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
        timeline_edge_types = {"followed_by", "corrected_by", "led_to", "resolved_by"}

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
        timeline_items: list[dict[str, Any]] = []
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
                        entity_desc = _clean_sep(node.get("description", ""))
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
                if relation not in timeline_edge_types:
                    continue
                edge_desc = _clean_sep(edge.get("description", ""))
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
        to proceed; ainsert_custom_kg will upsert/overwrite existing nodes).
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
        to proceed; ainsert_custom_kg will upsert/overwrite existing edges).
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
            return edge_data.get("keywords", "").lower() == (keywords or "").lower()

    def get_graph_snapshot(self, limit: int = 2000) -> dict[str, Any]:
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
            # When limit <= 0, return ALL nodes (no truncation)
            if limit <= 0:
                top_nodes = list(snapshot.nodes())
                top_set = set(top_nodes)
            else:
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
                    "type": attrs.get("entity_type", "other"),
                    "description": _clean_description(attrs.get("description", ""), attrs.get("entity_type", "other")),
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
                            "description": _clean_sep(data.get("description", "")),
                            "weight": data.get("weight", 1.0),
                        })
            except RuntimeError:
                logger.warning("Graph modified during snapshot edge read, returning partial edges")

            result = {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "limit": limit,
                },
            }
            # 截断由 agent_loop 统一关口处理（Agent 工具调用路径）
            # 前端 API（kg_api.py graph_snapshot 端点）直接调此方法，不被截断
            return result

        except Exception as e:
            logger.error(f"LightRAG get_graph_snapshot failed: {e}")
            return {"nodes": [], "edges": []}

    # ============== Management Methods ==============

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """Delete a document and all its related entities/relations by doc_id.

        Uses LightRAG's adelete_by_doc_id for cascading deletion: removes the
        document's chunks, entities (if fully owned), and relationships (if
        fully owned).  Partially-owned entities/relationships are rebuilt from
        remaining documents.

        Args:
            doc_id: Document ID (e.g., "note:shopping").

        Returns:
            Dict with status and details.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.adelete_by_doc_id(doc_id), timeout=300)

            # Check DeletionResult status
            if hasattr(result, "status"):
                if result.status == "not_found" or (hasattr(result, "status_code") and getattr(result, "status_code", None) == 404):
                    logger.info(f"LightRAG delete_document: doc '{doc_id}' not found, treated as ok")
                    return {"status": "ok", "doc_id": doc_id, "note": "not_found_treated_as_ok"}
                elif result.status in ("not_allowed", "fail"):
                    logger.warning(f"LightRAG delete_document failed for '{doc_id}': status={result.status}")
                    return {"status": "error", "message": f"DeletionResult status: {result.status}", "doc_id": doc_id}
                else:
                    # status == "success"
                    # Record change for frontend changelog polling (best-effort)
                    try:
                        from niu_api.internal.lightrag_manager import get_change_log
                        get_change_log().record_change("document_deleted", {"id": doc_id})
                    except Exception as e:
                        logger.debug(f"changelog record_change failed: {e}")
                    return {"status": "ok", "doc_id": doc_id, "result": str(result)}
            else:
                # No status attribute — assume success for backward compatibility
                try:
                    from niu_api.internal.lightrag_manager import get_change_log
                    get_change_log().record_change("document_deleted", {"id": doc_id})
                except Exception as e:
                    logger.debug(f"changelog record_change failed: {e}")
                return {"status": "ok", "doc_id": doc_id, "result": str(result)}
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "404" in err_str:
                logger.info(f"LightRAG delete_document: doc '{doc_id}' not found, treated as ok")
                return {"status": "ok", "doc_id": doc_id, "note": "not_found_treated_as_ok"}
            logger.error(f"LightRAG delete_document failed: {e}")
            return {"status": "error", "message": str(e)}

    def delete_entity(self, entity_name: str) -> dict[str, Any]:
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

            # Check DeletionResult status (LightRAG returns a DeletionResult object)
            if hasattr(result, "status"):
                if result.status == "not_found" or (hasattr(result, "status_code") and getattr(result, "status_code", None) == 404):
                    logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found, treated as ok")
                    return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
                elif result.status in ("not_allowed", "fail"):
                    logger.warning(f"LightRAG delete_entity failed for '{entity_name}': status={result.status}")
                    return {"status": "error", "message": f"DeletionResult status: {result.status}", "entity_name": entity_name}
                else:
                    # status == "success"
                    # Record change for frontend changelog polling (best-effort)
                    try:
                        from niu_api.internal.lightrag_manager import get_change_log

                        get_change_log().record_change("entity_deleted", {"id": entity_name})
                    except Exception as e:
                        logger.debug(f"changelog record_change failed: {e}")

                    return {"status": "ok", "entity_name": entity_name, "result": str(result)}
            elif hasattr(result, "status_code") and getattr(result, "status_code", None) == 404:
                logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found (404), treated as ok")
                return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
            else:
                # No status attribute — assume success for backward compatibility
                # Record change for frontend changelog polling (best-effort)
                try:
                    from niu_api.internal.lightrag_manager import get_change_log

                    get_change_log().record_change("entity_deleted", {"id": entity_name})
                except Exception as e:
                    logger.debug(f"changelog record_change failed: {e}")

                return {"status": "ok", "entity_name": entity_name, "result": str(result)}
        except Exception as e:
            err_str = str(e).lower()
            if "not found" in err_str or "404" in err_str:
                logger.info(f"LightRAG delete_entity: entity '{entity_name}' not found, treated as ok")
                return {"status": "ok", "entity_name": entity_name, "note": "not_found_treated_as_ok"}
            logger.error(f"LightRAG delete_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def edit_entity(self, entity_name, updated_data, allow_rename=False, allow_merge=False, timeout=300):
        """Edit an entity's properties in the knowledge graph.

        Args:
            entity_name: Entity name to edit.
            updated_data: Dict of properties to update.
            allow_rename: Whether to allow renaming the entity.
            allow_merge: Whether to allow merging with existing entities.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.aedit_entity(entity_name, updated_data, allow_rename=allow_rename, allow_merge=allow_merge), timeout)
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG edit_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def edit_relation(self, source_entity, target_entity, updated_data, timeout=300):
        """Edit a relation's properties in the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            updated_data: Dict of properties to update.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.aedit_relation(source_entity, target_entity, updated_data), timeout)
            # Clean <SEP> from description in returned data
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG edit_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def delete_relation(self, source_entity, target_entity, timeout=300):
        """Delete a relation from the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.adelete_by_relation(source_entity, target_entity), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG delete_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_entity_info(self, entity_name, include_vector_data=False, timeout=30):
        """Get detailed information about an entity.

        Args:
            entity_name: Entity name to look up.
            include_vector_data: Whether to include vector data in the result.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.get_entity_info(entity_name, include_vector_data), timeout)
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG get_entity_info failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_relation_info(self, source_entity, target_entity, include_vector_data=False, timeout=30):
        """Get detailed information about a relation.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            include_vector_data: Whether to include vector data in the result.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            result = call_async(rag.get_relation_info(source_entity, target_entity, include_vector_data), timeout)
            # Clean <SEP> from description in result
            if isinstance(result, dict):
                graph_data = result.get("graph_data", {})
                if isinstance(graph_data, dict) and "description" in graph_data:
                    graph_data["description"] = _clean_sep(graph_data.get("description", ""))
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG get_relation_info failed: {e}")
            return {"status": "error", "message": str(e)}

    def create_entity(self, entity_name, entity_type, description="", source_id="manual_creation", file_path="manual_creation", timeout=300):
        """Create a new entity in the knowledge graph.

        Args:
            entity_name: Name of the entity to create.
            entity_type: Type of the entity (e.g., "Person", "Concept").
            description: Description of the entity.
            source_id: Source chunk ID.
            file_path: File path for citation.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            entity_data = {"entity_type": entity_type, "description": description, "source_id": source_id, "file_path": file_path}
            result = call_async(rag.acreate_entity(entity_name, entity_data), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG create_entity failed: {e}")
            return {"status": "error", "message": str(e)}

    def create_relation(self, source_entity, target_entity, keywords, description="", weight=1.0, source_id="manual_creation", file_path="manual_creation", timeout=300):
        """Create a new relation in the knowledge graph.

        Args:
            source_entity: Source entity name.
            target_entity: Target entity name.
            keywords: Keywords/relation type for the edge.
            description: Description of the relation.
            weight: Weight of the relation (default 1.0).
            source_id: Source chunk ID.
            file_path: File path for citation.
            timeout: Maximum seconds to wait for the operation.

        Returns:
            Dict with status and data.
        """
        rag = self._get_rag()
        if rag is None:
            return {"status": "error", "message": "LightRAG not available"}
        try:
            relation_data = {"keywords": keywords, "description": description, "weight": weight, "source_id": source_id, "file_path": file_path}
            result = call_async(rag.acreate_relation(source_entity, target_entity, relation_data), timeout)
            return {"status": "ok", "data": result}
        except Exception as e:
            logger.error(f"LightRAG create_relation failed: {e}")
            return {"status": "error", "message": str(e)}

    def document_status(self) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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
                        nt = node_data.get("entity_type", "other")
                        if nt.lower() == entity_type.lower():
                            nodes.append({
                                "entity_name": node_id,
                                "entity_type": nt,
                                "description": _clean_description(node_data.get("description", ""), nt),
                                "source_id": node_data.get("source_id", ""),
                                "file_path": node_data.get("file_path", ""),
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
                            "entity_name": node.id,
                            "entity_type": node.properties.get("entity_type", "other"),
                            "description": _clean_description(node.properties.get("description", ""), node.properties.get("entity_type", "other")),
                            "source_id": node.properties.get("source_id", ""),
                            "file_path": node.properties.get("file_path", ""),
                        })
                    return {"status": "ok", "data": nodes}
            else:
                return {"status": "error", "message": f"Unknown list_type: {list_type}"}
        except Exception as e:
            logger.error(f"LightRAG list_entities failed: {e}")
            return {"status": "error", "message": str(e)}

    def _resolve_entity_name_case_insensitive(
        self, entity_name: str, nx_graph
    ) -> str | None:
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
        source_entities: list[str],
        target_entity: str,
        merge_strategy: dict | None = None,
        target_entity_data: dict | None = None,
    ) -> dict[str, Any]:
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

        resolved_sources: list[str] = []
        unresolved_sources: list[str] = []
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
                rag.amerge_entities(resolved_sources, resolved_target, merge_strategy=merge_strategy, target_entity_data=target_entity_data),
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
                target_type = "other"
                target_desc = ""
                if nx_graph and nx_graph.has_node(resolved_target):
                    with graph_read_lock():
                        if nx_graph.has_node(resolved_target):
                            attrs = nx_graph.nodes[resolved_target]
                            target_type = attrs.get("entity_type", "other")
                            target_desc = _clean_description(attrs.get("description", ""), target_type)

                get_change_log().record_change("entity_merged", {
                    "source_ids": resolved_sources,
                    "target_id": resolved_target,
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
        confidence: dict[str, Any] | None = None,
        source_id: str = "custom_kg",
    ) -> dict[str, Any]:
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

        text = f"交互习惯: {entity_name}（类型: interactionhabit），{description}。"
        return self.lightrag_insert(content=text, file_paths=source_id if source_id != "custom_kg" else None)

    def update_habit_confidence(
        self,
        entity_name: str,
        result: str,
    ) -> dict[str, Any]:
        """Update confidence for an interaction habit entity.

        Reads the current entity, updates success/fail counts, and re-injects
        it (LightRAG upsert). If fail_count >= 3, deletes the entity instead.

        This replaces the old vector_search.update_habit_confidence() which
        used SQLite operations on the now-removed vectors.db.

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
            entity_type = target_node.properties.get("entity_type", "interactionhabit")  # noqa: F841

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
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        source_id: str = "custom_kg",
    ) -> dict[str, Any]:
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
            custom_kg: dict[str, Any] = {
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
            # Guard: filter out illegal Niu -> non-brainregion connections.
            # Legal Niu -> brain-region anchor edges are created by the
            # region_manager independently (source_id="brain"), not via this
            # path. This guard intercepts any relationship whose src or tgt
            # is "Niu" (case-insensitive) and whose other end is not a
            # brain-region entity, preventing future code paths from
            # reintroducing the 24-rule-violating edges.
            filtered_relationships: list[dict[str, Any]] = []
            for rel in relationships:
                src = (rel.get("src_id") or "").strip()
                tgt = (rel.get("tgt_id") or "").strip()
                src_is_niu = src.lower() == "niu"
                tgt_is_niu = tgt.lower() == "niu"
                if src_is_niu or tgt_is_niu:
                    other_id = tgt if src_is_niu else src
                    other_entity = next(
                        (e for e in entities if (e.get("entity_name") or e.get("name")) == other_id),
                        None,
                    )
                    other_type = (other_entity.get("entity_type") or "").lower() if other_entity else ""
                    if other_type != "brainregion":
                        logger.warning(
                            f"[inject_custom_kg] 拦截违规 Niu 连接: {src} --[{rel.get('keywords','')}]--> {tgt} "
                            f"(对端 entity_type={other_type or '未知'}, 非 brainregion)"
                        )
                        continue
                filtered_relationships.append(rel)
            relationships = filtered_relationships

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
                    "entity_type": entity.get("entity_type", "other"),
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
                        "id": entity['entity_name'],
                        "name": entity["entity_name"],
                        "type": entity.get("entity_type", "other"),
                        "description": entity.get("description", ""),
                        "file_path": entity.get("file_path", ""),
                        "source_id": entity.get("source_id", ""),
                    })
                for rel in custom_kg["relationships"]:
                    change_log.record_change("edge_created", {
                        "source": rel['src_id'],
                        "target": rel['tgt_id'],
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

    def lightrag_insert(self, content: str, file_paths: str | None = None) -> dict[str, Any]:
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
        doc_id: str | None = None,
        file_path: str | None = None,
    ) -> dict[str, Any]:
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
        documents: list[str],
        ids: list[str] | None = None,
        file_paths: list[str] | None = None,
    ) -> dict[str, Any]:
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
