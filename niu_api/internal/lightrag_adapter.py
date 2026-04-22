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


class LightRAGAdapter:
    """Query interface for LightRAG.

    Replaces vector-store search and kg-server query with unified
    LightRAG retrieval supporting multiple modes.
    """

    def _get_rag(self):
        """Get the LightRAG instance (delegates to lightrag_manager)."""
        return get_lightrag()

    def query(
        self,
        query: str,
        mode: str = "mix",
        only_need_context: bool = False,
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
            return result

        except Exception as e:
            logger.error(f"LightRAG query failed: {e}")
            return None


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
